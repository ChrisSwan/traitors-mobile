---
id: traitors-mobile-metrics
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

# Module: Metrics (traitors-mobile-metrics)

## Purpose
Batch runner + statistics + report writing. Runs the ≥10-game baseline batch through the real orchestrator, then aggregates the three required metrics — traitor catch rate, average exchanges before resolution, formal-accusation usage (spec §9.1) — and persists a machine-readable report plus a human-readable summary. Satisfies spec §2.6 and §8 ("Aggregate metrics report across the batch").

## Depends on
- `traitors-mobile-orchestrator`: `run_game`, `GameResult`, `GameAbortedError`, `GameConfig`, `write_game_outputs`-produced result records.
- `traitors-mobile-scenario`: `build_scenario` / `default_scenario` (per-game scenario).
- `traitors-mobile-llm-backend`: backend construction types (used only to build the backend the players share).

## Constraints (non-goals)
- No gameplay logic here — Metrics never constructs actions, transcripts, or votes; it only runs games and aggregates their result records.
- Aborted games are **excluded** from all metrics and listed separately (`games_aborted` with reasons) — an aborted game must never dilute the catch rate denominator silently.
- No direct LLM calls in this module (it orchestrates the orchestrator).
- Determinism: given the same seeds and the same backend, the report is reproducible.

## External dependencies
- Python 3.11 stdlib only (`json`, `statistics`, `pathlib`, `random` for seed generation). No new packages.
- **Verified:** stdlib by construction on Python 3.11.15.

## Interface

### `run_batch(batch_config: dict, game_factory: callable) -> list[GameResult]`
- Behavior: runs `num_games` games sequentially (baseline ≥ 10, from config). For game `i`: seed = `config.seed + i` when a base seed is given, else a fresh `random` seed (logged per game so runs stay auditable). `game_factory(seed, game_id)` returns `(scenario, players, game_config)`; the caller (Integration) builds the real objects so Metrics stays decoupled from wiring. Calls `run_game` per game inside try/except `GameAbortedError` — aborted games are collected separately and returned as aborted result records (status aborted), not raised.
- Raises: `ConfigError` when `num_games < 1`; propagates unexpected errors (a genuine bug should fail loudly, not be silently counted).
- Side effects: each completed game persists its transcript + result via the orchestrator (files under `output_dir`).

### `compute_metrics(results: list[GameResult]) -> MetricsReport`
- Behavior: pure aggregation over **completed** games only:
  - `catch_rate` = (games with `traitor_caught == True`) / (completed games), as a float in [0,1]; `None` when zero completed games.
  - `mean_exchanges` = mean of `exchange_count` over completed games (resolution = the final-vote phase; exchange count = total turns across all rounds, per tech-design §11).
  - `accusation_usage` = `{games_with_formal_accusation: int, mean_formal_accusations_per_game: float, fraction_of_games: float}`.
  - plus `games_completed`, `games_aborted` (with reasons), and the raw per-game summary rows (game_id, seed, traitor_caught, exchange_count, most_accused).
  - Returns `MetricsReport` (dict-like dataclass) with these fields.
- Raises: never.
- Side effects: none.

### `write_report(report: MetricsReport, output_dir: Path) -> tuple[Path, Path]`
- Behavior: writes `metrics_report.json` (full structured report, pretty-printed) and `metrics_report.md` (human-readable: headline numbers, per-game table, aborted-game notes) into `output_dir` (created if needed). Returns the two paths.
- Raises: `OSError` on filesystem failures.
- Side effects: the two report files.

## Reuse check
Searched existing `specs/` contracts (this repo, other `~/sdd-projects/` repos) for: `metrics`, `batch runner`, `catch rate`, `statistics`. Found: none. The `compute_metrics` aggregation is a **moderate reuse candidate** (Experiments 2–5 will compute the same three metrics on different configurations) — keep it a pure function over `GameResult` records.

## QA acceptance highlights (behavioral)
- `compute_metrics` on a synthetic list of 10 `GameResult`s (mix of caught/not-caught, one aborted) yields the exact expected catch rate and mean (e.g. 4/10 caught → 0.4, aborted excluded), and lists the aborted game in `games_aborted` with its reason.
- `compute_metrics` with zero completed games returns `catch_rate=None` (no ZeroDivisionError).
- `run_batch` with a game_factory whose games always abort → returns all-aborted results, no exception, and the subsequent report is written with `games_completed == 0`.
- `write_report` produces two real files whose JSON parses and whose `games_completed` equals the number of real `game_*.result.json` files on disk (cross-check with the filesystem).
