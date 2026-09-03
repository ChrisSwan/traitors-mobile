---
id: traitors-mobile-tech-design
type: tech-design
project: traitors-mobile
parents: [SWA-145, SWA-176]
status: draft
version: 2
paperclip_issue: SWA-177
owner_role: Architect
created: 2026-09-01
updated: 2026-09-03
---

# Tech Design: Mode B (The Parlour) AI Agent Simulation — Phase 1

> **Revision 2 (2026-09-03, SWA-176/SWA-177): LLM backend switched to DeepSeek.**
> `specs/contracts/llm-backend.md` revised to version 2. DeepSeek (`deepseek-v4-flash`
> via `https://api.deepseek.com/v1`, OpenAI-compatible chat completions) is now the
> primary, default real-API backend. Claude is demoted to a legacy, non-default path
> (code retained; no Anthropic key provisioned — intentional, not a bug). Ollama stays
> the opportunistic/free secondary, unchanged. Default `backend.provider` in
> `config.example.json` and code defaults: `"deepseek"`.

## 1. Source and scope

This design derives from `specs/spec.md` (SWA-145, committed `b2efa8f`), which I read in full, plus the SWA-176 backend-switch directive (Chris's decision, cost rationale in `2_Areas/06_AI_Development/01_SDD/docs/API Cost Sweep - 2026-09-02.md`).

**Explicit stack deviation, stated plainly (per standing rule):** the spec's §7 wording named Claude API as "the primary, reliable backend and the default in configuration", and revision 1 of this design pinned exactly that (official `anthropic` Python SDK). SWA-176 supersedes that choice at the product level: the prototype's own code calls the raw `anthropic` SDK, which cannot use the org's Pro/Max OAuth (claude_local ACP) transport, and no new metered Anthropic key is being provisioned. Chris chose DeepSeek instead (already-provisioned account, proven working, dramatically cheaper per the cost-sweep doc). This design therefore names **DeepSeek v4-flash as primary** in place of Claude — a deliberate, reason-backed substitution driven by the parent issue, not a silent swap. Claude code is retained as a legacy non-default path. Ollama remains exactly as before (opportunistic secondary).

Scope unchanged: Experiment 1 only ("baseline catch rate"). Experiments 2–5, Mode A, UI, and the human playtest are out of scope per spec §11.

## 2. Verified environment (checked 2026-09-01 and 2026-09-03, on this machine)

| Fact | How verified | Result |
|---|---|---|
| `python3` version | `python3 --version` | **Python 3.11.15** (project `.venv`; also `/home/chris/.hermes/hermes-agent/venv/bin/python3` on PATH). |
| DeepSeek endpoint reachable | `source ~/.hermes/.env` (key loaded, never printed) then `curl -sS https://api.deepseek.com/v1/models -H "Authorization: Bearer $DEEPSEEK_API_KEY"` | **HTTP 200, 2026-09-03.** JSON list of model ids: `deepseek-v4-flash`, `deepseek-v4-pro`, `deepseek-v4-flash-vision-exp`. The endpoint `https://api.deepseek.com/v1` is real and OpenAI-compatible. |
| DeepSeek model `deepseek-v4-flash` live call | `curl -sS https://api.deepseek.com/v1/chat/completions` with `{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_tokens":256,"temperature":0.0}` | **HTTP 200, 2026-09-03.** OpenAI-compatible `chat.completion`; `choices[0].message` = `{role, content: "OK", reasoning_content: "We need to reply exactly \"OK\"..."}`; `finish_reason: "stop"`; `usage.completion_tokens_details.reasoning_tokens: 22`. **v4-flash is a reasoning model** — responses carry `reasoning_content` (CoT) alongside `content`. |
| Reasoning-budget edge case | Same call with `max_tokens: 16` | **HTTP 200 but `content: ""`, `finish_reason: "length"`** — the whole budget was consumed by reasoning, so no answer text was produced. Contract rule: empty `content` ⇒ raise (never record `""` or CoT as dialogue). |
| `DEEPSEEK_API_KEY` presence | `grep -c '^DEEPSEEK_API_KEY=' ~/.hermes/.env` | **1** (present; real value; never printed/committed). NOT set in paperclip agent shells by default — real runs must `source ~/.hermes/.env` first. |
| `ANTHROPIC_API_KEY` presence | `grep -c '^ANTHROPIC_API_KEY=' ~/.hermes/.env` | **0 on 2026-09-03** (was 1 on 2026-09-01). No Anthropic key provisioned — intentional per SWA-176. The claude provider path stays in code but will `ConfigError` until a key exists; expected, not a bug. |
| `anthropic` SDK | `.venv/bin/pip list` | **1.2.0 installed** in the project `.venv` (2026-09-03). Retained for the legacy claude path only. |
| `requests` | `.venv/bin/pip list` | **2.32.3 installed** in the project `.venv` (2026-09-03). Used for both DeepSeek and Ollama HTTP. |
| `openai` / `deepseek` / `ollama` Python packages | `.venv/bin/pip list` | **None installed** — do not pin any of them. DeepSeek's API is OpenAI-compatible over plain HTTP via `requests` (verified endpoint shape above), exactly like Ollama. |
| `pytest` | `pip index versions pytest` (2026-09-01) / `.venv/bin/pip list` | **9.1.1** latest; installed in `.venv`. |
| Ollama on LAN | `curl http://192.168.0.38:11434/api/tags` | **Reachable 2026-09-03** — responds with models incl. `phi4:latest`, `qwen3:8b`. Opportunistic: reachable now, not guaranteed up later. |
| `ollama` Python package | import check (2026-09-01) | **Not installed** — do not pin it. Ollama access is plain HTTP via `requests`. |

## 3. System overview

```
                     ┌──────────────────────────────────────────────┐
                     │                 integration.py               │  Module 6
                     │  CLI entry point · config load · wiring ·    │  (Application
                     │  run-single / run-batch · exit codes         │   Assembly)
                     └───────────────┬──────────────────────────────┘
                                     │ builds / wires
        ┌────────────────────────────┼────────────────────────────┐
        ▼                            ▼                            ▼
 ┌──────────────┐           ┌────────────────┐            ┌──────────────┐
 │   scenario   │◄─────────►│   orchestrator │◄──────────►│    player    │
 │ Module 1     │ scenario   │ Module 4       │ 5 players  │ Module 2     │
 │ templates,   │ + cast     │ turn driver,   │ (agents)   │ prompt build,│
 │ role cards,  │            │ rules, tally,  │            │ action parse │
 │ init         │            │ transcript     │            │ & validate   │
 └──────┬───────┘            └───────┬────────┘            └──────┬───────┘
        │                           │                             │
        │                    game JSON out                 ┌──────▼───────┐
        │                    (transcript, result)          │ llm_backend │
        │                                                  │ Module 3     │
        ▼                                                  │DeepSeek/Ollama
 ┌──────────────┐                                          │ /Mock (+Claude│
 │   metrics    │◄─── batch of GameResults                  │  legacy)     │
 │ Module 5     │                                          └──────────────┘
 │ stats, report│
 └──────────────┘
```

Flow in one sentence: `integration` loads config → `scenario` produces a concrete Scenario (cast + parameterised role cards) → the `orchestrator` drives N rounds in which each `player` agent (backed by `llm_backend`) emits a structured action into the shared transcript → the orchestrator tallies private final votes, persists per-game transcript + result JSON → `metrics` aggregates ≥N games into catch rate / exchange count / accusation-usage statistics and writes the report.

## 4. Module decomposition (7 modules incl. Application Assembly)

All six modules named in the task brief are kept, in the same names; the natural-boundary / hour-budget / local-model-context checks all pass at this granularity (each module is one coherent responsibility, ~150–300 lines, small enough for a local model to hold contract + file + tests + output together). **Module 6 (Integration) is explicitly the Application Assembly module** — the only place where the runnable entry point exists.

| # | Module (id) | Responsibility (one sentence) | Natural boundary — varies independently from… | Depends on |
|---|---|---|---|---|
| 1 | Scenario (`traitors-mobile-scenario`) | Define, load, validate scenario templates + role cards and initialise a concrete Scenario (cast assignment, card identity-substitution) | The cast/names/crime content can change without touching game logic; could be swapped for new scenarios for Experiments 2–5 | none |
| 3 | LLM Backend (`traitors-mobile-llm-backend`) | One interface to DeepSeek (primary, default), Ollama (opportunistic), Claude (legacy, non-default), and a deterministic Mock; owns retries/backoff/timeouts/probe | The model provider can be swapped (DeepSeek ↔ Ollama ↔ Claude ↔ mock) without touching game logic | none |
| 2 | Player (`traitors-mobile-player`) | LLM-backed player agent: builds the per-player prompt from own role material + transcript, calls the backend, parses/validates the six structured action types, re-prompts once | Role-specific behaviour and prompt format can change without touching the turn driver | 3 (`llm_backend`), 1 (`scenario` role cards) |
| 4 | Orchestrator (`traitors-mobile-orchestrator`) | Drives one game: turn schedule, session rules, question-response enforcement, accusation tracking, final-vote tally, per-game transcript + result persistence | The discussion schedule / rule enforcement can change without touching the LLM or prompt format | 2, 1 |
| 5 | Metrics (`traitors-mobile-metrics`) | Batch runner + statistics + report writing (catch rate, avg exchanges, accusation usage) | Reporting format and which stats matter can change without touching gameplay | 4 (GameResult), 1, 3 (via config types) |
| 6 | Integration (`traitors-mobile-integration`) — **Application Assembly** | Main entry point (`python -m traitors_sim`): config loading/validation, wiring scenario→players→orchestrator→metrics, run-single / run-batch, exit codes | The user-facing surface can change (CLI flags, config file shape) without changing gameplay internals | 1, 2, 3, 4, 5 |

**Reuse candidates (flagged):**
- `llm_backend` — **strong reuse candidate.** A generic LLM abstraction (messages-in/text-out, retries, mock, multi-provider factory) with zero game concepts is likely to be reused by future experiments and other SDD projects. Its interface must stay clean and self-contained (no imports from other modules). Revision 2 adds DeepSeek through the same plain-HTTP OpenAI-compatible pattern as Ollama — keeping the module's blast radius small.
- `orchestrator.tally_votes` — pure vote-tallying function, plausible future reuse by the live Mode B app. Keep it pure (no I/O).

**Build order (dependency order):** env setup → 1 Scenario → 3 LLM Backend → 2 Player → 4 Orchestrator → 5 Metrics → 6 Integration (last, per Application Assembly rule).

## 5. Data flow and file formats

1. **Config → scenario**: `integration.load_config()` reads JSON config → `scenario.build_scenario(defn, seed)` initialises a `Scenario` (players, role cards with `{placeholder}` identity references substituted, public scenario text, crime window).
2. **Scenario → player**: each `PlayerAgent` is constructed with its own identity + role card only.
3. **Player → orchestrator**: `player.act(transcript, round_info, must_respond)` returns a validated `Action` (one of the six types) or a `NonCompliantAction`.
4. **Orchestrator → transcript**: every exchange is appended to the in-memory transcript and persisted per game as `game_<game_id>.transcript.json`:
   ```json
   {"game_id": "...", "seed": 42, "scenario": "stolen-prize-tin", "turns": [
     {"turn": 1, "phase": "opening", "speaker": "The Abbotts", "action_type": "statement", "content": "...", "target": null, "reason": null},
     {"turn": 2, "phase": "opening", "speaker": "The Murphys", "action_type": "question", "content": "...", "target": "The Chens", "reason": null}
   ], "accusations": [{"turn": 14, "accuser": "The Patels", "target": "The Abbotts", "reason": "..."}], "votes": [{"player": "The Abbotts", "vote": "The Chens"}, ...], "outcome": {"traitor_caught": false, "tie": false, "no_accusation": false, "most_accused": "The Chens"}}
   ```
5. **Orchestrator → result**: `game_<game_id>.result.json` per game (traitor identity, most-accused, `traitor_caught`, vote tally, exchange count, `status: "completed" | "aborted"`).
6. **Metrics**: reads all completed GameResults → `metrics_report.json` + `metrics_report.md` (catch rate, mean exchanges, accusation usage, aborted-game list).

All outputs are structured files on disk under `output_dir` (config), never console-only (spec §8).

## 6. Pinned stack (Engineer must not substitute)

- **Language:** Python **3.11** (verified 3.11.15 in the project `.venv`).
- **DeepSeek (primary, default):** NO SDK — plain `requests==2.32.3` against the OpenAI-compatible endpoint `POST https://api.deepseek.com/v1/chat/completions` with header `Authorization: Bearer $DEEPSEEK_API_KEY` (verified live 2026-09-03). Default model **`deepseek-v4-flash`**. The `openai` and `deepseek` Python packages are NOT installed and must NOT be added.
- **Claude (legacy, non-default):** `anthropic==1.2.0` retained in `.venv` and in the code path (do not delete working code), but no longer the default and not expected to work right now — no `ANTHROPIC_API_KEY` is provisioned (intentional).
- **Ollama (opportunistic secondary):** no SDK — `requests==2.32.3` against `POST http://192.168.0.38:11434/v1/chat/completions` (verified live). No `ollama` Python package (not installed). Unchanged from revision 1.
- **Tests:** `pytest==9.1.1`.
- **Everything else: Python 3.11 stdlib only** — `json` for config/transcripts, `dataclasses` for models, `argparse` for the CLI. No Flask, no YAML, no asyncio, no DB.
- **Default model per provider:** DeepSeek `deepseek-v4-flash` (live-verified 2026-09-03). Claude (if ever explicitly selected with a key) `claude-haiku-4-5`. Ollama `qwen3:8b` (present on the LAN server, verified). All overridable in config.

The spec named Claude/Ollama (spec §7); revision 1 implemented exactly that. Revision 2 deliberately substitutes DeepSeek as primary per SWA-176 — reason given in §1 above. Ollama's role is unchanged, so no other spec-stack deviation exists.

## 7. Error handling (spec §10, mapped to modules)

1. **Backend unavailable/degraded** (spec §10.1): `llm_backend` retries with exponential backoff up to the configured budget (`max_retries`, `retry_backoff_base_seconds`), distinguishing `BackendTimeoutError`, `RateLimitError` (retryable), and `BackendUnreachableError`. When the budget is exhausted it raises `BackendUnavailableError`. The orchestrator catches any `BackendError` subclass (it catches the base class), **aborts the game** with `status: "aborted"` and reason — an aborted game is persisted but **never counted in metrics**. A failed LLM call is **never recorded as an exchange** (hard rule, testable: mock that fails N times → transcript exchange count unchanged). **New in revision 2 — DeepSeek reasoning-model edge case:** v4-flash returns `content: ""` + `finish_reason: "length"` when `max_tokens` is consumed by reasoning before an answer is produced (verified live at `max_tokens=16`). A 2xx response with empty/missing `content` raises `BackendError` immediately (non-retryable — retrying the same request with the same budget cannot help) with a message naming the empty content; `reasoning_content` is NEVER used as the response text (CoT must never enter a transcript) and `""` is never returned as a real exchange.
2. **Malformed/non-compliant agent output** (spec §10.2): `player` parses the raw text into an `Action`; `validate_action` checks structure (question has a named target; vote names exactly one player or "no accusation"; no role-revealing text; no out-of-character chatter). On violation: **re-prompt once** with the validation error; if still invalid, the turn is logged as a `NonCompliantAction` (reason recorded) and play **continues — never crashes**. A player's false *claim* (e.g. Traitor lying) is legitimate gameplay: it is preserved in the transcript as a claim and is **never injected into any "known facts" record** (the game keeps no facts store — the transcript is the only record).
3. **Vote tally ambiguity** (spec §10.3): `tally_votes` is pure and deterministic: only explicit single-name votes count toward a target; `"no accusation"` is a valid non-target vote; multi-name/garbage votes are excluded (and reported in `invalid_votes`); the Traitor is caught **iff the Traitor has strictly more valid votes than any other player**; ties, no-accusation outcomes, and invalid votes are all reported explicitly in the result record. Covered by unit tests for every combination.
4. **Private-information leakage** (spec §10.4): prompt isolation is a hard constraint. `build_player_prompt` receives **only** the player's own role card, the public scenario text, the shared transcript, and the rules — never another player's cards, never the Traitor's sealed crime/cover story, never the Detective hint. Exposed as a testable helper: `assert_prompt_isolated(prompt_text, private_materials_by_player, player_id)` returns violations. QA's contract-compliance check must assert this per player.

## 8. Configuration (JSON)

```json
{
  "backend": {
    "provider": "deepseek",                  // "deepseek" | "claude" | "ollama" | "mock" — deepseek is the default
    "model": "deepseek-v4-flash",            // deepseek default; ollama default "qwen3:8b"; claude legacy default "claude-haiku-4-5"
    "timeout_seconds": 60,
    "max_retries": 3,
    "retry_backoff_base_seconds": 2.0,
    "ollama_base_url": "http://192.168.0.38:11434"
  },
  "run": {
    "num_games": 10,
    "rounds_per_game": 6,                     // tunable constant, spec §6
    "seed": null,                             // null = time-based; int = reproducible
    "output_dir": "output"
  },
  "scenario": { "template": "stolen-prize-tin" },  // built-in baseline; see contract
  "cast": {
    "traitor": "The Abbotts", "detective": "The Murphys",
    "loyalist_a": "The Chens", "loyalist_b": "The Patels", "loyalist_c": "The Okayes"
  }
}
```

`integration.load_config(path)` validates this schema, fills defaults for every optional key (so a config file may be minimal), and raises `ConfigError` naming the broken field. `provider: "mock"` selects the deterministic `MockBackend` (used by all unit tests and available for dry runs). `config.example.json` ships `"provider": "deepseek"` / `"model": "deepseek-v4-flash"`. API key resolution: DeepSeek reads env var `DEEPSEEK_API_KEY` at runtime; the legacy claude path reads `ANTHROPIC_API_KEY` (documented: source `~/.hermes/.env` before running; Hermes/paperclip shells do not carry either key by default). The integration module's `VALID_PROVIDERS` must include `"deepseek"` (i.e. `("deepseek", "claude", "ollama", "mock")`).

## 9. Test/dev strategy

- **Per-module unit tests, all mock-backed and deterministic** (spec §7: tests that don't need a real model must use a mock backend). QA writes tests first (red), Engineer builds to green, per the standing TDD discipline. No unit test may hit the network. New DeepSeek behavioral tests mock the OpenAI-compatible HTTP response shape (a dict with `choices[0].message.content`); assert (a) text comes from `content`, never from `reasoning_content`, (b) empty `content` raises instead of returning `""`.
- **One real-model integration check only, at the end:** the final integration review runs the real entry point `python -m traitors_sim run-batch` with `provider: "deepseek"` for **≥10 real games** (spec §9 success criteria; this is SWA-176 Part 3) and inspects the real output. Ollama is exercised opportunistically if reachable (probe result is logged), never required. Claude is not expected to be exercised (no key — intentional).
- **Determinism:** `seed` threads through scenario init, speaking-order rotation, and any sampling — a fixed seed reproduces the same game sequence (given the same backend responses).
- **Verification posture:** QA re-runs the suite itself, checks stack compliance (requests present; anthropic retained but non-default; no openai/deepseek/ollama SDKs added), asserts behavioral tests (not presence), and reads real transcript files, per the established anti-self-certification discipline.

## 10. Success criteria (what "done and correct" means)

1. **Real end-to-end batch:** the real entry point runs ≥10 games against real DeepSeek (`deepseek-v4-flash`), all complete (`status: "completed"`), with transcripts and results persisted under `output_dir`. (Executed in SWA-176 Part 3; this design pins the target.)
2. **Genuine, varying catch rate:** `metrics_report.json` reports a catch rate that is a real fraction of real tallies (value expected in (0,1), varying across runs/seeds) — **not** a stub, constant, or hardcoded value. The report's `games_completed` count must equal the number of real result files on disk.
3. **Real transcripts, reviewable quality:** transcripts contain real exchanges across multiple action types (statement/question/challenge/corroboration/formal accusation), and spot-checking shows inference beyond the player's own cards, contradictions raised, and genuine uncertainty (spec §9 green flags). Transcript text must be the model's `content` — never `reasoning_content`.
4. **Edge cases proven:** unit tests cover backend failure (game aborted, no fake exchange, including the DeepSeek empty-content case), malformed output (re-prompt once then non-compliant-continue), vote-tally ambiguity (ties/no-accusation/multi-name), and prompt isolation (no leakage, assertable).
5. **Contract compliance:** every module matches its `specs/contracts/*.md` interface contract (signatures, constraints, pinned deps) — checked by QA before behavioral acceptance. llm-backend contract is version 2 (DeepSeek primary, Claude legacy, Ollama unchanged, default provider `deepseek`).

## 11. Assumptions / design decisions (stated plainly)

- **DeepSeek via its OpenAI-compatible `/v1` endpoint over plain HTTP** (verified live 2026-09-03) rather than an SDK — zero new dependencies (`requests` already pinned), and the same `messages`-shaped format already used for Ollama. If DeepSeek is unreachable, `probe()` reports it and the run fails loudly (DeepSeek is the primary provider, not opportunistic).
- **Ollama via OpenAI-compatible `/v1/chat/completions`** (verified live) rather than native `/api/chat` — one `messages`-shaped format across DeepSeek and Ollama, and the `/v1` surface is the more stable contract. If the desktop is off, `probe()` reports unreachable and the run uses DeepSeek (or fails loudly if Ollama was explicitly requested).
- **Empty `content` from the reasoning model is an error, not a silent `""` exchange** — verified that a small `max_tokens` budget is consumed by `reasoning_content` before any answer is produced. Raising on empty content keeps the "never record a failed call as a real exchange" rule intact. Callers that need long answers should pass an adequate `max_tokens` (the 16-token live test truncated; 256 produced a normal answer).
- **Config in JSON** (stdlib) instead of YAML — zero extra dependencies, matches the spec's "structured files" requirement.
- **The baseline scenario ships as built-in data** (defined fully in the Scenario contract, content from spec §4–5) rather than an external file the Engineer must invent; a configurable `template` key allows future scenarios.
- **Round schedule** (spec §6): 6 rounds = opening statements → interrogation (question/challenge/corroboration) → pressure → accusation window → closing, then private final votes. Speaking order rotates per round (seed-driven); when a Question names a target, the orchestrator schedules the target's response as the next turn in that round (enforced, re-prompt once if silent).
- **"Resolution"** for "average exchanges before resolution" = the final-vote phase; exchange count = total turns across all rounds (documented in the Metrics contract so the number is unambiguous).
- **Aborted games** are excluded from all metrics and listed separately in the report.

## 12. Environment setup (ONCE, before any module build)

In the project repo `~/sdd-projects/traitors-mobile/` (the dedicated git repo — no vault writes for code; vault gets doc mirrors only):

```bash
cd ~/sdd-projects/traitors-mobile
python3 -m venv .venv                       # python3 = 3.11.15, verified working
.venv/bin/pip install --upgrade pip
.venv/bin/pip install anthropic==1.2.0 requests==2.32.3 pytest==9.1.1
```

This venv already exists (created 2026-09-01, re-verified 2026-09-03 with all three pinned packages installed) and is reused by every module build (standing rule: no per-module venvs). **No new installs are needed for the DeepSeek switch** — DeepSeek is plain HTTP via the already-pinned `requests`; no `openai`/`deepseek` SDK. `.venv/` and `output/` are git-ignored. Real DeepSeek runs additionally need `DEEPSEEK_API_KEY` exported (source `~/.hermes/.env`).

## 13. Deliverables map

- `specs/tech-design.md` — this document (revision 2, 2026-09-03).
- `specs/contracts/scenario.md`, `specs/contracts/llm-backend.md` (revision 2: DeepSeek primary), `specs/contracts/player.md`, `specs/contracts/orchestrator.md`, `specs/contracts/metrics.md`, `specs/contracts/integration.md` — one interface contract per module (committed to the repo, mirror-copied into the vault `specs/contracts/` for cross-device visibility).
- Package layout the Engineer will build against: `traitors_mobile/{scenario,llm_backend,player,orchestrator,metrics,integration}.py` + `tests/test_*.py` + `config.example.json` (default provider `"deepseek"`, model `"deepseek-v4-flash"`) + `requirements.txt`.
- Engineer change scope for the backend switch (per SWA-176): add `DeepSeekBackend` + factory branch in `traitors_mobile/llm_backend.py` (reusing the Ollama HTTP pattern), add `"deepseek"` to `VALID_PROVIDERS` and flip the provider/model defaults in `traitors_mobile/integration.py` and `config.example.json`. Claude code path stays in place.
