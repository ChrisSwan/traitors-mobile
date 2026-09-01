"""
traitors-mobile-metrics (Module 5)

Batch runner + statistics + report writing. Runs the >=10-game baseline batch
through the real orchestrator, then aggregates the three required metrics --
traitor catch rate, average exchanges before resolution, formal-accusation
usage (spec sec 9.1) -- and persists a machine-readable report plus a
human-readable summary.

Contract: specs/contracts/metrics.md (SWA-146).
Dependencies: traitors-mobile-orchestrator (run_game, GameResult,
GameAbortedError, ConfigError). Python 3.11 stdlib only: json, statistics,
pathlib, random, dataclasses, typing.

Constraints honored:
- No gameplay logic here -- Metrics only runs games and aggregates result
  records; it never constructs actions, transcripts, or votes.
- Aborted games are EXCLUDED from all metrics and listed separately
  (games_aborted with reasons) -- an aborted game never dilutes the catch
  rate denominator silently.
- No direct LLM calls in this module.
- Determinism: same seeds + same backend -> reproducible report.
"""

from __future__ import annotations

import json
import random
import statistics
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from traitors_mobile.orchestrator import (
    ConfigError,
    GameAbortedError,
    GameResult,
    run_game,
)


@dataclass
class MetricsReport:
    """Aggregated statistics across a batch of games.

    catch_rate: (games with traitor_caught) / (completed games), None when
        zero completed games.
    mean_exchanges: mean exchange_count over completed games, None when zero
        completed games.
    accusation_usage: formal-accusation statistics over completed games.
    games_completed: number of completed games.
    games_aborted: per-game records for aborted games (with reasons).
    game_summaries: raw per-game summary rows for completed games.
    """

    catch_rate: Optional[float]
    mean_exchanges: Optional[float]
    accusation_usage: Dict[str, Any]
    games_completed: int
    games_aborted: List[Dict[str, Any]] = field(default_factory=list)
    game_summaries: List[Dict[str, Any]] = field(default_factory=list)


def run_batch(batch_config: dict, game_factory: Callable) -> List[GameResult]:
    """Run ``num_games`` games sequentially and return their result records.

    For game ``i``: seed = ``config["seed"] + i`` when a base seed is given,
    else a fresh random seed. ``game_factory(seed, game_id)`` returns
    ``(scenario, players, game_config)``; each game is run via ``run_game``
    inside try/except ``GameAbortedError`` -- aborted games are collected as
    aborted result records (status "aborted"), not raised.

    Raises ConfigError when num_games < 1; propagates unexpected errors.
    """
    num_games = batch_config.get("num_games", 0)
    if not isinstance(num_games, int) or isinstance(num_games, bool) or num_games < 1:
        raise ConfigError(f"num_games must be >= 1, got {num_games!r}")

    base_seed = batch_config.get("seed")
    results: List[GameResult] = []

    for i in range(num_games):
        if base_seed is not None:
            seed = int(base_seed) + i
        else:
            seed = random.randrange(0, 2**31 - 1)

        game_id = f"game_{i}"
        try:
            scenario, players, game_config = game_factory(seed, game_id)
            result = run_game(scenario, players, game_config, game_id=game_id)
            results.append(result)
        except GameAbortedError as exc:
            # Aborted games are returned as aborted records, never raised.
            results.append(
                GameResult(
                    game_id=game_id,
                    seed=seed,
                    scenario="",
                    traitor_id="",
                    votes=[],
                    vote_tally={},
                    valid_votes=0,
                    invalid_votes=[],
                    traitor_caught=False,
                    most_accused=None,
                    tie=False,
                    no_accusation=False,
                    exchange_count=0,
                    accusations=[],
                    status="aborted",
                    abort_reason=str(exc),
                )
            )

    return results


def compute_metrics(results: List[GameResult]) -> MetricsReport:
    """Aggregate metrics over the COMPLETED games only.

    Pure function; never raises; no side effects. Aborted games are excluded
    from every metric and listed separately in ``games_aborted`` with their
    abort reasons.
    """
    completed = [r for r in results if getattr(r, "status", "completed") != "aborted"]
    aborted = [r for r in results if getattr(r, "status", "aborted") == "aborted"]

    num_completed = len(completed)

    if num_completed > 0:
        caught_count = sum(1 for r in completed if r.traitor_caught)
        catch_rate = caught_count / num_completed
        mean_exchanges = statistics.mean(r.exchange_count for r in completed)

        games_with_accusation = sum(1 for r in completed if r.accusations)
        total_accusations = sum(len(r.accusations) for r in completed)
        accusation_usage = {
            "games_with_formal_accusation": games_with_accusation,
            "mean_formal_accusations_per_game": total_accusations / num_completed,
            "fraction_of_games": games_with_accusation / num_completed,
        }
    else:
        catch_rate = None
        mean_exchanges = None
        accusation_usage = {
            "games_with_formal_accusation": 0,
            "mean_formal_accusations_per_game": 0.0,
            "fraction_of_games": 0.0,
        }

    games_aborted = [
        {
            "game_id": r.game_id,
            "seed": r.seed,
            "status": "aborted",
            "abort_reason": r.abort_reason,
        }
        for r in aborted
    ]

    game_summaries = [
        {
            "game_id": r.game_id,
            "seed": r.seed,
            "traitor_caught": r.traitor_caught,
            "exchange_count": r.exchange_count,
            "most_accused": r.most_accused,
        }
        for r in completed
    ]

    return MetricsReport(
        catch_rate=catch_rate,
        mean_exchanges=mean_exchanges,
        accusation_usage=accusation_usage,
        games_completed=num_completed,
        games_aborted=games_aborted,
        game_summaries=game_summaries,
    )


def _render_markdown(report: MetricsReport) -> str:
    """Render a human-readable markdown summary of the report."""
    lines: List[str] = ["# Metrics Report", ""]
    lines.append(f"- Games completed: {report.games_completed}")
    lines.append(f"- Games aborted: {len(report.games_aborted)}")
    lines.append(f"- Catch rate: {report.catch_rate}")
    lines.append(f"- Mean exchanges: {report.mean_exchanges}")

    acc = report.accusation_usage
    lines.append("- Accusation usage:")
    lines.append(
        f"  - Games with formal accusation: {acc.get('games_with_formal_accusation', 0)}"
    )
    lines.append(
        f"  - Mean formal accusations per game: "
        f"{acc.get('mean_formal_accusations_per_game', 0.0)}"
    )
    lines.append(f"  - Fraction of games: {acc.get('fraction_of_games', 0.0)}")
    lines.append("")

    lines.append("## Per-game summaries")
    lines.append("")
    lines.append(
        "| game_id | seed | traitor_caught | exchange_count | most_accused |"
    )
    lines.append(
        "|---------|------|----------------|----------------|--------------|"
    )
    for row in report.game_summaries:
        lines.append(
            "| {game_id} | {seed} | {traitor_caught} | {exchange_count} | {most_accused} |".format(
                game_id=row.get("game_id", ""),
                seed=row.get("seed", ""),
                traitor_caught=row.get("traitor_caught", ""),
                exchange_count=row.get("exchange_count", ""),
                most_accused=str(row.get("most_accused", "")).replace("|", "\\|"),
            )
        )
    lines.append("")

    if report.games_aborted:
        lines.append("## Aborted games")
        lines.append("")
        for g in report.games_aborted:
            lines.append(
                f"- {g.get('game_id')}: {g.get('abort_reason')}"
            )
        lines.append("")

    return "\n".join(lines)


def write_report(report: MetricsReport, output_dir: Path) -> Tuple[Path, Path]:
    """Write ``metrics_report.json`` and ``metrics_report.md`` into output_dir.

    The output directory is created if needed. Returns the two file paths.
    Raises OSError on filesystem failures.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if is_dataclass(report):
        data = asdict(report)
    else:
        data = dict(report)

    json_path = output_dir / "metrics_report.json"
    json_path.write_text(json.dumps(data, indent=2, default=str))

    md_path = output_dir / "metrics_report.md"
    md_path.write_text(_render_markdown(report))

    return json_path, md_path
