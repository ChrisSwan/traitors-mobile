---
id: traitors-mobile-integration
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

# Module: Integration (traitors-mobile-integration) — Application Assembly

## Purpose
The Application Assembly module: the only real runnable entry point. Loads and validates configuration, wires scenario → players → orchestrator → metrics in the real data-flow order, and exposes `run-single` (one game) and `run-batch` (≥N games) subcommands with meaningful exit codes. Satisfies spec §8 (configuration inputs) and §9 (the harness runs the real baseline batch end-to-end via the real entry point). This module exists because the spec calls for a real runnable harness — six individually-correct modules with no entry point would be a failed build.

## Depends on
- **Every other module**: `traitors-mobile-scenario` (`default_scenario`, `build_scenario`), `traitors-mobile-llm-backend` (`create_backend`, `probe`), `traitors-mobile-player` (`PlayerAgent`), `traitors-mobile-orchestrator` (`run_game`, `GameConfig`, `write_game_outputs`), `traitors-mobile-metrics` (`run_batch`, `compute_metrics`, `write_report`).
- Built **LAST**, after every module it depends on has individually passed.

## Constraints (non-goals)
- No gameplay logic — Integration only wires and drives the other modules.
- Config must be JSON (stdlib `json`) — no YAML, no new dependencies.
- Must not silently swallow failures: exit codes distinguish config errors from backend failures.
- The Claude API key is read from the environment (`ANTHROPIC_API_KEY`) at runtime — never hardcoded, never written to config files (documented in the config template comment).

## External dependencies
- Python 3.11 stdlib (`json`, `argparse`, `os`, `pathlib`, `sys`). Plus the project's pinned runtime deps used transitively (anthropic==1.2.0, requests==2.32.3).
- **Verified:** argparse/json/os/pathlib present by construction on Python 3.11.15.

## Interface

### `load_config(path: str | None = None) -> AppConfig`
- Behavior: with `path` — reads and validates the JSON config file against the schema in tech-design §8 (`backend`, `run`, `scenario`, `cast` sections). Without `path` — returns the built-in default config (provider claude, model claude-haiku-4-5, 10 games, 6 rounds, output_dir `output`, baseline stolen-prize-tin scenario, baseline cast). Fills defaults for every optional key; unknown keys are ignored with a warning collected in `config.warnings`. Returns `AppConfig` (dataclass with `backend`, `run`, `scenario`, `cast` sub-configs).
- Raises: `ConfigError` (message names the offending field and the fix) for: unknown `backend.provider` (must be claude|ollama|mock), `num_games < 1`, `rounds_per_game < 2`, non-JSON file, missing required section keys.
- Side effects: none.

### `build_game_components(config: AppConfig, seed: int, game_id: str, backend: LLMBackend | None = None) -> tuple[Scenario, dict[str, PlayerAgent], GameConfig]`
- Behavior: constructs the real objects for one game — `scenario = build_scenario(defn, seed)` (defn from `default_scenario()` or `load_scenario(config.scenario.template)`), one `PlayerAgent` per cast member (own role card only), shared `backend` (created once per run via `create_backend`, passed in so the batch reuses one backend), and a `GameConfig` from `config.run`. Returns the triple the Metrics `game_factory` expects.
- Raises: propagates `ScenarioError`, `ConfigError`, `BackendError` from construction.
- Side effects: none (backend creation reads env var but makes no call).

### `main(argv: list[str] | None = None) -> int`
- Behavior: real CLI entry point, invoked as `python -m traitors_sim run-single [--config path] [--seed N]` or `python -m traitors_sim run-batch [--config path]`. `run-single`: loads config, probes the configured backend (`probe()` — Ollama result logged: reachable/unreachable), builds components, runs one game via `run_game`, prints the result summary (traitor, caught?, most accused, transcript path). `run-batch`: same setup, then `run_batch` → `compute_metrics` → `write_report`; prints the headline metrics and the report paths. Both create `output_dir`. Returns 0 on success.
- Raises: never — all errors are converted to exit codes (below). `ConfigError` → prints error to stderr, returns 1. `BackendUnavailableError` (during a real run) → stderr message, returns 2 (backend unreachable / budget exhausted; for `run-batch`, the report still gets written with whatever completed). `GameAbortedError` mid-single-game → stderr, returns 3. Unexpected exception → traceback to stderr, returns 4.
- Side effects: config/transcripts/results/reports written under `output_dir`; stdout summary; stderr diagnostics; exit code.

### `__main__` support
- Behavior: `python -m traitors_sim` routes to `main()`; `if __name__ == "__main__": sys.exit(main())` in `integration.py` (or a thin `__main__.py`).
- Raises: as `main`.
- Side effects: as `main`.

## Reuse check
Searched existing `specs/` contracts (this repo, other `~/sdd-projects/` repos) for: `application assembly`, `entry point`, `cli`. Found: none (this is the first contracted Assembly module in this repo). Not a reuse candidate itself — it is project-specific glue by design, but its `load_config` schema is the reference for Experiments 2–5 config extensions.

## QA acceptance highlights (behavioral — must invoke the REAL entry point)
- End-to-end with mock backend: `main(["run-batch", "--config", <mock-config>])` (config with `provider: "mock"`, `num_games: 3`, fixed seed) returns 0 and produces: 3 `game_*.transcript.json` + 3 `game_*.result.json` (all `status: "completed"`), `metrics_report.json` + `.md`; the report's `games_completed == 3`; catch rate ∈ {0, 1/3, 2/3, 1} and matches a manual tally of the result files. This is the mandatory end-to-end assembly test — a script that only imports modules and checks existence does NOT satisfy this contract.
- `main(["run-single", ...])` with a scripted mock returns 0 and prints the summary line; transcript path printed exists on disk.
- `main` with a broken config path / invalid JSON / `provider: "bogus"` returns 1 with an error naming the field.
- `main` where the mock backend always raises → returns 2 (or 3 for run-single) with a clear stderr message, and no fake exchange anywhere.
- **Final integration review (real, not mocked):** the reviewer runs `python -m traitors_sim run-batch` with `provider: "claude"` and `num_games >= 10` (env: `source ~/.hermes/.env`), then verifies: ≥10 completed games, real varying catch rate in (0,1), transcripts contain real multi-action-type exchanges, and spot-checked transcript quality (inference beyond own cards, contradictions, uncertainty). This is the spec §9 success-criteria gate; it is run by QA/reviewer, not embedded in the automated unit suite (which must stay mock-only and network-free).
