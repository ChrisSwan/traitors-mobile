---
id: traitors-mobile-player
type: interface-contract
project: traitors-mobile
parents: [SWA-146]
status: draft
version: 1
paperclip_issue: SWA-146
owner_role: Architect
created: 2026-09-01
updated: 2026-09-01
---

# Module: Player (traitors-mobile-player)

## Purpose
The LLM-backed player agent: builds the per-player prompt (own role card + public scenario + shared transcript + session rules + allowed action types), calls the LLM backend, and parses/validates the model's output into one of the six structured actions (statement / question / challenge / corroboration / formal accusation / final vote). Re-prompts once on format violations; never crashes the run. Satisfies spec §2.2 ("Each player acts from their private role material plus the shared conversation transcript") and §6 (structured turn actions).

## Depends on
- `traitors-mobile-llm-backend`: `LLMBackend.complete`, `LLMResponse` (used for every action generation).
- `traitors-mobile-scenario`: `RoleCard`, `PlayerIdentity`, `Scenario` (role material source).

## Constraints (non-goals)
- **Prompt isolation is a hard requirement (spec §10.4):** the prompt for player X must contain ONLY: (a) X's own role card, (b) the public scenario text, (c) the shared transcript, (d) session rules + allowed actions. Never another player's cards, never the Traitor's crime declaration/cover story (unless X is the Traitor), never the Detective hint (unless X is the Detective). Exposed as a testable helper (below).
- No transcript persistence, no game-state mutation, no vote tallying — the Orchestrator owns those.
- No fabrication into "facts": the module must never inject a model output into any facts store — a false *claim* by a player is legitimate transcript content (the Traitor may lie, spec §7.5), and the transcript preserves it as a claim. The distinction is: claims live in the transcript; there is no separate facts record to corrupt.
- No direct network calls here — all model access goes through `llm_backend`.

## External dependencies
- Same as `traitors-mobile-llm-backend` (anthropic==1.2.0, requests==2.32.3 — used transitively; the Player module itself adds **no** new dependencies beyond Python 3.11 stdlib: `dataclasses`, `re`, `json`).
- **Verified:** stdlib availability by construction on Python 3.11.15.

## Interface

### Data types
- `Action` dataclass: `{action_type: str, content: str, target: str | None, reason: str | None}` where `action_type ∈ {statement, question, challenge, corroboration, formal_accusation, final_vote}`. For question: `target` = named player (required); formal_accusation: `target` + `reason` (required); final_vote: `content` = a player name or `"no accusation"`, `target=None`. NOTE: both `target=None` and `target=""` (empty string) are valid representations of "no target" for action types that don't require one; these should be treated as equivalent in validation.
- `NonCompliantAction` dataclass: `{raw_text: str, reason: str, action_type: "non_compliant"}` — a real recorded turn type (the run continues; spec §10.2).
- `PlayerState`: `{player_id, role, role_card, scenario, backend, model_config}`.

### `build_player_prompt(state: PlayerState, transcript: list[Exchange], round_info: dict, must_respond_to: str | None = None) -> list[dict]`
- Behavior: returns a `messages` list for the backend: system prompt (role, goal, session rules from spec §6, **the public cast roster — the 5 household names, stated as the only valid targets**, allowed action types + required format, "never announce your role", "you may share or withhold your observations but never claim observations you were not given", "keep content to 1-2 sentences"), user message containing the public scenario text, the player's own private role card (observations; Traitor also crime declaration + cover story; Detective also hint), the transcript of prior exchanges (each rendered as `"<Household> (<action_type>): <content>"`, omitting private content — transcript only contains public exchanges by construction), current round/phase, whether a question is pending to this player (`must_respond_to`), and the instruction to reply with exactly one action in the specified format — repeating the valid-target roster and forbidding scenario-narrative characters (e.g. "the caretaker", "buffet_observer", "the group") as targets (SWA-180).
- Raises: `PromptError` if `state` lacks a role card or scenario.
- Side effects: none. **Isolation property:** given all players' private materials, `assert_prompt_isolated(text, materials_by_player, player_id)` (below) must return no violations for every player.

### `assert_prompt_isolated(prompt_text: str, private_materials_by_player: dict[str, str], player_id: str) -> list[str]`
- Behavior: pure helper. For every OTHER player's private material strings (each observation/goal/crime declaration/cover story/hint, normalised to lower-case), checks whether the material appears in `prompt_text` (case-insensitive, after collapsing whitespace). Returns a list of violation descriptions; empty list = isolated. Own material may appear.
- Household names are **not** private (SWA-180): the cast roster is public — every player knows who is present and the shared transcript labels every speaker by household name. A prompt may freely name other households (roster, transcript) as long as none of their card material appears.
- Raises: never.
- Side effects: none. (Used by both the Player's own prompt construction tests and QA's leakage-compliance check.)

### `class PlayerAgent`
`PlayerAgent(identity: PlayerIdentity, role_card: RoleCard, scenario: Scenario, backend: LLMBackend, model_config: dict)`

#### `act(transcript: list[Exchange], round_info: dict, must_respond_to: str | None = None) -> Action | NonCompliantAction`
- Behavior: builds the prompt, calls `backend.complete`, parses the returned text with `parse_action`, validates with `validate_action`. On validation failure: re-prompt ONCE (user message beginning "Your previous reply was invalid: <errors>. Reply with exactly one valid action." and continuing with the valid-target roster, the JSON-only/no-fences format and the 1-2 sentence conciseness rule — SWA-180) and re-parse; if still invalid, returns `NonCompliantAction(raw_text, reason=joined errors)`. Never raises on malformed model output. Propagates `BackendError` from the backend (the Orchestrator decides game abort — a failed LLM call must never become a fake exchange).
- Token budget: when `model_config` does not name a `max_tokens`, `act` calls `complete` with `PLAYER_DEFAULT_MAX_TOKENS` (8192) rather than the backend's 256-token default — 256 truncates real models' JSON replies mid-object, which reads as "could not extract an action" (SWA-180), and deepseek-v4-flash consumes 1.7k–6.5k completion tokens on real mid-game turns (verified live 2026-09-03), so budgets below ~2k also abort on the empty-content rule. `model_config.max_tokens`, `temperature`, `timeout` are honoured when present.
- Raises: `BackendError` subclasses (from backend, after its own retries); `PromptError` if prompt construction fails.
- Side effects: none (backend network calls only).

#### `final_vote(transcript: list[Exchange]) -> Action`
- Behavior: like `act` but restricted to `action_type == "final_vote"`; validates that content is exactly one cast member name or `"no accusation"`. On two failed attempts returns `NonCompliantAction` with reason. Never raises on malformed output.
- Raises: `BackendError` subclasses; `PromptError`.
- Side effects: none.

### `parse_action(raw_text: str, allowed_types: list[str], cast_names: list[str]) -> Action | ParseFailure`
- Behavior: pure. Parses the model's reply into an `Action`. Accepts both JSON (`{"action_type": "...", "content": "...", "target": "...", "reason": "..."}`) and a labelled plain-text format (e.g. `ACTION: question\nTARGET: The Chens\nTEXT: ...`) — the prompt must specify one canonical format; supporting the second format is a resilience bonus, not required. `final_vote` parses a bare name or `"no accusation"` too. Returns `ParseFailure(errors: list[str])` when the structure cannot be extracted.
- Raises: never.
- Side effects: none.

### `validate_action(action: Action, cast_names: list[str], rules: dict) -> list[str]`
- Behavior: pure. Returns a list of problems (empty = valid).

  **Target requirements per action type:**
  - `question`: REQUIRES a non-empty target in the cast (player name).
  - `formal_accusation`: REQUIRES a non-empty target in the cast AND a non-empty `reason`.
  - `statement`, `corroboration`, `alibi`, `challenge`, `final_vote`: DO NOT require a target. Both `target=None` and `target=""` (empty string) are treated as equivalent "no target provided" and valid for these types.

  **Validation checks (all types):**
  - Content must be non-empty.
  - If a target is provided and non-empty, it must be a cast member name.
  - Content must not contain role-revealing phrases ("I am the traitor", "as a traitor", "my role is", case-insensitive) — rule §4.
  - Content must not contain out-of-character chatter ("as an AI", "I'm a language model", case-insensitive) — rule §3.
  - For `final_vote`: content must be exactly a cast member name or `"no accusation"`.

- Raises: never.
- Side effects: none.

## Reuse check
Searched existing `specs/` contracts (this repo, other `~/sdd-projects/` repos) for: `player agent`, `action parser`, `structured output`, `prompt isolation`. Found: none. `parse_action`/`validate_action` are **moderate reuse candidates** (structured-action parsing could serve a live Mode B app later) — they must stay pure (no LLM, no I/O).

## QA acceptance highlights (behavioral)
- `build_player_prompt` for each of the 5 players passes `assert_prompt_isolated` against all other players' private materials (leakage: zero violations).
- `parse_action` on representative strings for all six action types yields the correct `Action` fields; on a question with no target it yields a `ParseFailure`/validation problem, never a crash.
- A scripted MockBackend returning garbage twice produces `NonCompliantAction` with the recorded reason; the caller (orchestrator test) continues — no exception escapes `act`.
- A MockBackend raising `BackendUnavailableError` propagates out of `act` (no fake exchange).
- `validate_action` flags "I am the traitor" style content and a final_vote naming two players.
- (SWA-180) `build_player_prompt` states all 5 household names and the target rule (valid targets are only those names; scenario-narrative characters like "the caretaker" are explicitly forbidden); roster-bearing prompts still pass `assert_prompt_isolated` (zero violations). A re-prompt after an invalid-target reply names the valid roster; with `model_config={}` the default `max_tokens` passed to `complete` is 8192, not 256.
