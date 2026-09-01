---
id: traitors-mobile-tech-design
type: tech-design
project: traitors-mobile
parents: [SWA-145]
status: draft
version: 1
paperclip_issue: SWA-146
owner_role: Architect
created: 2026-09-01
updated: 2026-09-01
---

# Tech Design: Mode B (The Parlour) AI Agent Simulation — Phase 1

## 1. Source and scope

This design derives from `specs/spec.md` (SWA-145, committed `b2efa8f`), which I read in full. The spec's own backend choice is **Claude API primary, local Ollama opportunistic secondary** — this design pins exactly that (official `anthropic` Python SDK for Claude; plain HTTP against Ollama's OpenAI-compatible `/v1` endpoint), with **no substitution** of the spec's named approach.

Scope: Experiment 1 only ("baseline catch rate"). Experiments 2–5, Mode A, UI, and the human playtest are out of scope per spec §11.

## 2. Verified environment (checked 2026-09-01, on this machine)

| Fact | How verified | Result |
|---|---|---|
| `python3` version | `python3 --version` | **Python 3.11.15** (`/home/chris/.hermes/hermes-agent/venv/bin/python3`, the `python3` on PATH). `/usr/bin/python3.14` also exists. `python3 -m venv` works (test venv created, 3.11.15). |
| Claude API connectivity | Sourced `ANTHROPIC_API_KEY` from `~/.hermes/.env` (key present there — value never printed), then a real `anthropic` SDK call | **`messages.create(model="claude-haiku-4-5", max_tokens=8)` returned "OK" — Claude API reachable.** (`claude-sonnet-4-5` not live-tested; `claude-haiku-4-5` is the org-standard Claude model per SDD CLAUDE.md.) |
| `ANTHROPIC_API_KEY` in agent shells | `env \| grep -i anthropic` | **NOT set in paperclip agent environments.** It lives in `~/.hermes/.env` (Hermes loads it). Implication: the app must read the key from the environment at runtime, and the Engineer's shell must source `~/.hermes/.env` (or export it) before real runs. Config documents this. |
| `anthropic` SDK | `python3 -c "import anthropic"` + `pip index versions anthropic` | 0.87.0 importable in the base env; **latest on PyPI = 1.2.0** (pin target for the project venv). |
| `requests` | import check | 2.32.3 importable (emits a benign urllib3/charset_normalizer version-mismatch warning; functional). |
| `pytest` | `pip index versions pytest` | **9.1.1** latest and installed in base env. |
| Ollama on LAN | `curl http://192.168.0.38:11434/api/tags` and `curl .../v1/models` | **Reachable right now** — both native `/api` and OpenAI-compatible `/v1` respond. 10 models available: `phi4:latest` (14.7B), `hermes-deepseek-r1-8b:latest`, `hermes-qwen3-8b:latest`, `qwen3:8b`, `hermes-orchestrator:latest`, `llama3.1:8b`, `qwen3-hermes:latest` (14.8B), `qwen3:14b`, `qwen2.5-coder:14b`, `deepseek-r1:8b`. **This is the opportunistic backend: reachable now, not guaranteed up later.** |
| `ollama` Python package | import check | **Not installed** — do not pin it. Ollama access is plain HTTP via `requests` (verified endpoint shape above). |

## 3. System overview

```
                     ┌──────────────────────────────────────────────┐
                     │                 integration.py                │  Module 6
                     │  CLI entry point · config load · wiring ·     │  (Application
                     │  run-single / run-batch · exit codes          │   Assembly)
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
        ▼                                                  │ Claude/Ollama│
 ┌──────────────┐                                          │ /Mock, retry │
 │   metrics    │◄─── batch of GameResults                  └──────────────┘
 │ Module 5     │
 │ stats, report│
 └──────────────┘
```

Flow in one sentence: `integration` loads config → `scenario` produces a concrete Scenario (cast + parameterised role cards) → the `orchestrator` drives N rounds in which each `player` agent (backed by `llm_backend`) emits a structured action into the shared transcript → the orchestrator tallies private final votes, persists per-game transcript + result JSON → `metrics` aggregates ≥N games into catch rate / exchange count / accusation-usage statistics and writes the report.

## 4. Module decomposition (7 modules incl. Application Assembly)

All six modules named in the task brief are kept, in the same names; the natural-boundary / hour-budget / local-model-context checks all pass at this granularity (each module is one coherent responsibility, ~150–300 lines, small enough for a local model to hold contract + file + tests + output together). **Module 6 (Integration) is explicitly the Application Assembly module** — the only place where the runnable entry point exists.

| # | Module (id) | Responsibility (one sentence) | Natural boundary — varies independently from… | Depends on |
|---|---|---|---|---|
| 1 | Scenario (`traitors-mobile-scenario`) | Define, load, validate scenario templates + role cards and initialise a concrete Scenario (cast assignment, card identity-substitution) | The cast/names/crime content can change without touching game logic; could be swapped for new scenarios for Experiments 2–5 | none |
| 3 | LLM Backend (`traitors-mobile-llm-backend`) | One interface to Claude (primary), Ollama (opportunistic), and a deterministic Mock; owns retries/backoff/timeouts/probe | The model provider can be swapped (Claude ↔ Ollama ↔ mock) without touching game logic | none |
| 2 | Player (`traitors-mobile-player`) | LLM-backed player agent: builds the per-player prompt from own role material + transcript, calls the backend, parses/validates the six structured action types, re-prompts once | Role-specific behaviour and prompt format can change without touching the turn driver | 3 (`llm_backend`), 1 (`scenario` role cards) |
| 4 | Orchestrator (`traitors-mobile-orchestrator`) | Drives one game: turn schedule, session rules, question-response enforcement, accusation tracking, final-vote tally, per-game transcript + result persistence | The discussion schedule / rule enforcement can change without touching the LLM or prompt format | 2, 1 |
| 5 | Metrics (`traitors-mobile-metrics`) | Batch runner + statistics + report writing (catch rate, avg exchanges, accusation usage) | Reporting format and which stats matter can change without touching gameplay | 4 (GameResult), 1, 3 (via config types) |
| 6 | Integration (`traitors-mobile-integration`) — **Application Assembly** | Main entry point (`python -m traitors_sim`): config loading/validation, wiring scenario→players→orchestrator→metrics, run-single / run-batch, exit codes | The user-facing surface can change (CLI flags, config file shape) without changing gameplay internals | 1, 2, 3, 4, 5 |

**Reuse candidates (flagged):**
- `llm_backend` — **strong reuse candidate.** A generic LLM abstraction (messages-in/text-out, retries, mock, multi-provider factory) with zero game concepts is likely to be reused by future experiments and other SDD projects. Its interface must stay clean and self-contained (no imports from other modules).
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

- **Language:** Python **3.11** (verified 3.11.15 on this machine; venv created from `python3`).
- **Claude:** `anthropic==1.2.0` (verified latest on PyPI; 0.87.0 verified importable in base env as fallback). Messages API, `messages.create`.
- **Ollama:** no SDK — `requests==2.32.3` against the OpenAI-compatible endpoint `POST http://192.168.0.38:11434/v1/chat/completions` (verified live). No `ollama` Python package (not installed).
- **Tests:** `pytest==9.1.1`.
- **Everything else: Python 3.11 stdlib only** — `json` for config/transcripts, `dataclasses` for models, `argparse` for the CLI. No Flask, no YAML, no asyncio, no DB.
- **Default Claude model:** `claude-haiku-4-5` (live-verified today). **Default Ollama model:** `qwen3:8b` (present on the LAN server, verified; org-proven for prose generation). Both overridable in config.
- **Config format:** JSON (stdlib — no extra dependency).

The spec named no libraries beyond "Claude API" and "Ollama"; this design implements exactly those (official SDK + direct HTTP), so no spec-stack deviation exists to flag.

## 7. Error handling (spec §10, mapped to modules)

1. **Backend unavailable/degraded** (spec §10.1): `llm_backend` retries with exponential backoff up to the configured budget (`max_retries`, `retry_backoff_base_seconds`), distinguishing `BackendTimeoutError`, `RateLimitError` (retryable), and `BackendUnreachableError`. When the budget is exhausted it raises `BackendUnavailableError`. The orchestrator catches it, **aborts the game** with `status: "aborted"` and reason — an aborted game is persisted but **never counted in metrics**. A failed LLM call is **never recorded as an exchange** (hard rule, testable: mock that fails N times → transcript exchange count unchanged).
2. **Malformed/non-compliant agent output** (spec §10.2): `player` parses the raw text into an `Action`; `validate_action` checks structure (question has a named target; vote names exactly one player or "no accusation"; no role-revealing text; no out-of-character chatter). On violation: **re-prompt once** with the validation error; if still invalid, the turn is logged as a `NonCompliantAction` (reason recorded) and play **continues — never crashes**. A player's false *claim* (e.g. Traitor lying) is legitimate gameplay: it is preserved in the transcript as a claim and is **never injected into any "known facts" record** (the game keeps no facts store — the transcript is the only record).
3. **Vote tally ambiguity** (spec §10.3): `tally_votes` is pure and deterministic: only explicit single-name votes count toward a target; `"no accusation"` is a valid non-target vote; multi-name/garbage votes are excluded (and reported in `invalid_votes`); the Traitor is caught **iff the Traitor has strictly more valid votes than any other player**; ties, no-accusation outcomes, and invalid votes are all reported explicitly in the result record. Covered by unit tests for every combination.
4. **Private-information leakage** (spec §10.4): prompt isolation is a hard constraint. `build_player_prompt` receives **only** the player's own role card, the public scenario text, the shared transcript, and the rules — never another player's cards, never the Traitor's sealed crime/cover story, never the Detective hint. Exposed as a testable helper: `assert_prompt_isolated(prompt_text, private_materials_by_player, player_id)` returns violations. QA's contract-compliance check must assert this per player.

## 8. Configuration (JSON)

```json
{
  "backend": {
    "provider": "claude",                     // "claude" | "ollama" | "mock"
    "model": "claude-haiku-4-5",              // claude default; ollama default "qwen3:8b"
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

`integration.load_config(path)` validates this schema, fills defaults for every optional key (so a config file may be minimal), and raises `ConfigError` naming the broken field. `provider: "mock"` selects the deterministic `MockBackend` (used by all unit tests and available for dry runs). Claude API key resolution: env var `ANTHROPIC_API_KEY` at runtime (documented: source `~/.hermes/.env` before running; Hermes/paperclip shells do not carry it by default).

## 9. Test/dev strategy

- **Per-module unit tests, all mock-backed and deterministic** (spec §7: tests that don't need a real model must use a mock backend). QA writes tests first (red), Engineer builds to green, per the standing TDD discipline. No unit test may hit the network.
- **One real-model integration check only, at the end:** the final integration review runs the real entry point `python -m traitors_sim run-batch` with `provider: "claude"` for **≥10 real games** (spec §9 success criteria) and inspects the real output. Ollama is exercised opportunistically if reachable (probe result is logged), never required.
- **Determinism:** `seed` threads through scenario init, speaking-order rotation, and any sampling — a fixed seed reproduces the same game sequence (given the same backend responses).
- **Verification posture:** QA re-runs the suite itself, checks stack compliance (anthropic/requests present, no substituted libs), asserts behavioral tests (not presence), and reads real transcript files, per the established anti-self-certification discipline.

## 10. Success criteria (what "done and correct" means)

1. **Real end-to-end batch:** the real entry point runs ≥10 games against real Claude, all complete (`status: "completed"`), with transcripts and results persisted under `output_dir`.
2. **Genuine, varying catch rate:** `metrics_report.json` reports a catch rate that is a real fraction of real tallies (value expected in (0,1), varying across runs/seeds) — **not** a stub, constant, or hardcoded value. The report's `games_completed` count must equal the number of real result files on disk.
3. **Real transcripts, reviewable quality:** transcripts contain real exchanges across multiple action types (statement/question/challenge/corroboration/formal accusation), and spot-checking shows inference beyond the player's own cards, contradictions raised, and genuine uncertainty (spec §9 green flags).
4. **Edge cases proven:** unit tests cover backend failure (game aborted, no fake exchange), malformed output (re-prompt once then non-compliant-continue), vote-tally ambiguity (ties/no-accusation/multi-name), and prompt isolation (no leakage, assertable).
5. **Contract compliance:** every module matches its `specs/contracts/*.md` interface contract (signatures, constraints, pinned deps) — checked by QA before behavioral acceptance.

## 11. Assumptions / design decisions (stated plainly)

- **Ollama via OpenAI-compatible `/v1/chat/completions`** (verified live today) rather than native `/api/chat` — one `messages`-shaped format across both backends, and the `/v1` surface is the more stable contract. If the desktop is off, `probe()` reports unreachable and the run uses Claude (or fails loudly if Claude is the requested provider — per §7.1).
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

This venv is created **once** and reused by every module build (standing rule: no per-module venvs). `.venv/` and `output/` are git-ignored. Real Claude runs additionally need `ANTHROPIC_API_KEY` exported (source `~/.hermes/.env`).

## 13. Deliverables map

- `specs/tech-design.md` — this document.
- `specs/contracts/scenario.md`, `specs/contracts/llm-backend.md`, `specs/contracts/player.md`, `specs/contracts/orchestrator.md`, `specs/contracts/metrics.md`, `specs/contracts/integration.md` — one interface contract per module (committed to the repo, mirror-copied into the vault `specs/contracts/` for cross-device visibility).
- Package layout the Engineer will build: `traitors_sim/{scenario,llm_backend,player,orchestrator,metrics,integration}.py` + `tests/test_*.py` + `config.example.json` + `requirements.txt`.
