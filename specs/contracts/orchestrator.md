---
id: traitors-mobile-orchestrator
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

# Module: Orchestrator (traitors-mobile-orchestrator)

## Purpose
Drives one complete game: the turn schedule (6 rounds: opening → interrogation → pressure → accusation window → closing, then private final votes), speaking order (rotated per round, seed-driven), session-rule enforcement (question must be answered; no role reveal; no out-of-character chat; Traitor may lie), formal-accusation tracking, final-vote tallying, and per-game persistence of the structured transcript + result record. Satisfies spec §2.2–§2.5, §6 (turn model + session rules), and §8 outputs.

## Depends on
- `traitors-mobile-scenario`: `Scenario` (cast, crime window), `PlayerIdentity`.
- `traitors-mobile-player`: `PlayerAgent`, `Action`, `NonCompliantAction`, `Exchange`-shaped transcript items.

## Constraints (non-goals)
- No model calls here — all LLM access happens inside `PlayerAgent` (which uses `llm_backend`). The orchestrator consumes already-returned actions.
- No metrics aggregation (that is `traitors-mobile-metrics`); the orchestrator produces per-game records only.
- A failed LLM call (propagated `BackendError` from a player) aborts the game — the orchestrator must record `status: "aborted"` and **never** record a fake exchange for the failed call, and never include aborted games' partial data in anything the Metrics module would count (the result record's `status` field is the guard).
- Prompt isolation is enforced upstream (Player module); the orchestrator must not concatenate private material into transcript items — the transcript it logs contains only public exchange fields (speaker, action_type, content, target, reason), never role cards.

## External dependencies
- Python 3.11 stdlib only (`json`, `dataclasses`, `random`, `pathlib`). No new packages.
- **Verified:** stdlib by construction on Python 3.11.15.

## Interface

### Data types
- `Exchange`: `{turn: int, phase: str, speaker: str, action_type: str, content: str, target: str | None, reason: str | None, non_compliant_reason: str | None}`.
- `Accusation`: `{turn: int, accuser: str, target: str, reason: str}`.
- `GameConfig`: `{rounds_per_game: int = 6, seed: int, output_dir: Path, phase_action_mix: dict}` (phases and their allowed action mixes per spec §6).
- `GameResult`: `{game_id: str, seed: int, scenario: str, traitor_id: str, votes: list[dict], vote_tally: dict, valid_votes: int, invalid_votes: list[str], traitor_caught: bool, most_accused: str | None, tie: bool, no_accusation: bool, exchange_count: int, accusations: list[Accusation], status: "completed" | "aborted", abort_reason: str | None}`.

### `run_game(scenario: Scenario, players: dict[str, PlayerAgent], config: GameConfig, game_id: str) -> GameResult`
- Behavior: executes the full schedule. Per round `r` in 1..rounds_per_game: derive the phase from the phase_action_mix schedule; rotate speaking order (seed-driven rotation; round 1 starts with the first household in cast order); for each speaker in order, call `player.act(transcript, round_info, must_respond_to=<open question target if this player is that target>)`; append the returned `Action` (or `NonCompliantAction`) to the transcript as an `Exchange`; if the action is a `formal_accusation`, append to `accusations`; if it is a `question`, mark the target as owing a response and schedule that target as the next speaker (if the target is silent on their forced turn → re-prompt once via `act(must_respond_to=...)`; if still non-compliant, the exchange is logged as non-compliant and play continues — spec §10.2). After the last round, call `player.final_vote(transcript)` for each player privately, collect votes, and compute the outcome via `tally_votes`. Persist transcript + result via `write_game_outputs`. Return the `GameResult`.
- Raises: `GameAbortedError(reason)` — when any `BackendError` propagates from a player after backend retries are exhausted (spec §10.1). The caller (Metrics) catches this, the game is recorded as aborted (via a result record with `status: "aborted"` written by `write_game_outputs` if possible), and it is excluded from metrics. No partial-game data is counted anywhere.
- Side effects: writes `game_<game_id>.transcript.json` and `game_<game_id>.result.json` to `config.output_dir` (see write_game_outputs).

### `tally_votes(votes: list[dict], traitor_id: str) -> TallyResult`
- Behavior: pure, deterministic. `votes` = list of `{"player": <voter>, "vote": <target-name-or-"no accusation">}`. Rules (spec §10.3): only explicit single-name votes that match a cast member count toward a target; `"no accusation"` is a valid non-target vote; multi-name/garbage/unknown-name votes go to `invalid_votes` (with voter + raw text). `traitor_caught = True` iff traitor's valid vote count is **strictly greater** than every other player's. `tie = True` when the top two valid counts are equal (and non-zero); `no_accusation = True` when zero valid target votes exist. `most_accused` = the player with the highest valid count (None on all-tie/no-accusation). Returns `TallyResult{counts: dict, valid_votes: int, invalid_votes: list[str], traitor_caught: bool, tie: bool, no_accusation: bool, most_accused: str | None}`.
- Raises: never.
- Side effects: none. **Reuse candidate** (pure, game-agnostic enough to power the live Mode B app's vote phase later).

### `detect_accusations(transcript: list[Exchange]) -> list[Accusation]`
- Behavior: pure. Extracts every `formal_accusation` exchange into `{turn, accuser, target, reason}` in turn order.
- Raises: never.
- Side effects: none.

### `write_game_outputs(result: GameResult, transcript: list[Exchange], output_dir: Path) -> tuple[Path, Path]`
- Behavior: writes `game_<result.game_id>.transcript.json` (structure: `{"game_id", "seed", "scenario", "turns": [Exchange...]}`) and `game_<result.game_id>.result.json` (the full `GameResult`), pretty-printed JSON, creating `output_dir` if needed. Returns the two paths. Atomic-ish: writes to a temp file then `os.replace` so a crash mid-write never leaves a truncated JSON.
- Raises: `OSError` on filesystem failures.
- Side effects: the two files on disk.

### `default_phase_schedule(rounds_per_game: int = 6) -> dict[int, str]`
- Behavior: returns round → phase mapping for the baseline schedule: rounds 1–2 `opening`, 3–4 `interrogation`, 5 `pressure` + `accusation window`, 6 `closing`; final votes always follow round `rounds_per_game` (private, after the last round). Phase names must match the allowed action mixes the Player prompt receives.
- Raises: `ConfigError` for `rounds_per_game < 2`.
- Side effects: none.

## Reuse check
Searched existing `specs/` contracts (this repo, other `~/sdd-projects/` repos) for: `orchestrator`, `turn driver`, `tally`, `transcript`. Found: none. `tally_votes` flagged as reuse candidate above; the rest is game-specific.

## QA acceptance highlights (behavioral)
- `tally_votes` unit tests cover: clear traitor win (caught=true), traitor loses plurality (caught=false), tie between two players, all no-accusation, multi-name vote excluded from counts and listed in `invalid_votes`, and mixed valid/invalid input — all with exact expected dicts.
- `run_game` with a fully scripted MockBackend (valid actions for every turn + votes) yields a `GameResult` with `status: "completed"`, `exchange_count == rounds * players`, and transcript + result files on disk whose JSON parses and whose `turns` length matches the count.
- `run_game` where a player's MockBackend raises `BackendUnavailableError` mid-game → raises `GameAbortedError`; the transcript file written (if any) contains NO exchange for the failed call (exchange count before failure), and `status: "aborted"` is recorded.
- Question flow: a question names The Chens → the next turn is The Chens' forced response; if the mock returns garbage twice, the turn is logged `non_compliant` and play continues (no crash, schedule intact).
- Prompt isolation holds end-to-end: building all 5 players' prompts from the same transcript passes `assert_prompt_isolated` per player.
