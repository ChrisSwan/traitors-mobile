"""
Acceptance tests for Metrics module (traitors-mobile-metrics, Module 5).

Contract: specs/contracts/metrics.md

Tests cover:
1. Contract compliance: function signatures and return types match
2. compute_metrics behavioral tests:
   - Synthetic GameResult list (10 games, mix of caught/not-caught, one aborted)
     yields exact expected catch rate and mean exchanges
   - Aborted games excluded from metrics but listed separately with reason
   - Zero completed games returns catch_rate=None (no ZeroDivisionError)
3. run_batch with all-aborting game factory returns all-aborted results
4. write_report produces two real files (JSON + markdown) with correct structure
5. Cross-validation: games_completed in report matches real game_*.result.json files
6. accusation_usage counts games with formal accusations and per-game mean
7. Error handling: ConfigError on num_games < 1

Dependencies: GameResult, GameAbortedError (from orchestrator).
All backend/scenario/player calls mocked — no real LLM calls, no network.
"""

import pytest
import json
import tempfile
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable, Tuple
from unittest.mock import Mock, patch, MagicMock

# Import the metrics module (will exist after Engineer implements it)
from traitors_mobile.metrics import (
    run_batch,
    compute_metrics,
    write_report,
    MetricsReport,
)

# Import dependencies from orchestrator
from traitors_mobile.orchestrator import GameAbortedError, GameResult, Accusation


# ============================================================================
# TEST FIXTURES AND HELPERS
# ============================================================================


@dataclass
class SyntheticGameResult:
    """Minimal GameResult for testing metrics aggregation."""

    game_id: str
    seed: int
    scenario: str = "test_scenario"
    traitor_id: str = "player_0"
    votes: List[Dict] = field(default_factory=list)
    vote_tally: Dict[str, int] = field(default_factory=dict)
    valid_votes: int = 0
    invalid_votes: List[str] = field(default_factory=list)
    traitor_caught: bool = False
    most_accused: Optional[str] = None
    tie: bool = False
    no_accusation: bool = False
    exchange_count: int = 0
    accusations: List[Accusation] = field(default_factory=list)
    status: str = "completed"  # "completed" | "aborted"
    abort_reason: Optional[str] = None

    def to_real_game_result(self) -> GameResult:
        """Convert to real GameResult for passing to metrics functions."""
        # Dynamically create a GameResult instance matching the orchestrator version
        return GameResult(
            game_id=self.game_id,
            seed=self.seed,
            scenario=self.scenario,
            traitor_id=self.traitor_id,
            votes=self.votes,
            vote_tally=self.vote_tally,
            valid_votes=self.valid_votes,
            invalid_votes=self.invalid_votes,
            traitor_caught=self.traitor_caught,
            most_accused=self.most_accused,
            tie=self.tie,
            no_accusation=self.no_accusation,
            exchange_count=self.exchange_count,
            accusations=self.accusations,
            status=self.status,
            abort_reason=self.abort_reason,
        )


def create_synthetic_results(
    num_games: int = 10,
    catch_count: int = 4,
    exchange_counts: Optional[List[int]] = None,
    include_aborted: bool = True,
    include_accusations: bool = True,
) -> List[GameResult]:
    """
    Create a synthetic list of GameResult records for testing.
    
    Args:
        num_games: total games (including any aborted)
        catch_count: number of games where traitor_caught=True
        exchange_counts: list of exchange_count values per game; if None, use default
        include_aborted: if True, last game is aborted
        include_accusations: if True, some games have formal accusations
    
    Returns:
        List of GameResult records ready for metrics computation.
    """
    if exchange_counts is None:
        base_counts = [10, 15, 12, 18, 11, 14, 13, 16, 12, 10]
        exchange_counts = []
        for i in range(num_games):
            exchange_counts.append(base_counts[i % len(base_counts)])

    results = []
    for i in range(num_games):
        # Last game is aborted if requested
        if include_aborted and i == num_games - 1:
            result = SyntheticGameResult(
                game_id=f"game_{i}",
                seed=42 + i,
                traitor_caught=False,
                exchange_count=0,
                status="aborted",
                abort_reason="BackendUnavailableError: service timeout",
            )
        else:
            # Distribute caught games among the first games
            is_caught = i < catch_count
            accusations = []
            if include_accusations and i % 2 == 0:
                # Add formal accusations to some games
                accusations = [
                    Accusation(turn=5, accuser="player_1", target="player_0", reason="suspicious"),
                    Accusation(turn=8, accuser="player_2", target="player_0", reason="lying"),
                ]

            result = SyntheticGameResult(
                game_id=f"game_{i}",
                seed=42 + i,
                traitor_caught=is_caught,
                exchange_count=exchange_counts[i],
                most_accused="player_0" if is_caught else "player_1",
                accusations=accusations,
                status="completed",
            )

        results.append(result.to_real_game_result())

    return results


@pytest.fixture
def output_dir():
    """Fixture: temporary directory for report output."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def synthetic_10_games():
    """Fixture: 10 synthetic GameResults (4 caught, 1 aborted, 5 not caught)."""
    return create_synthetic_results(
        num_games=10,
        catch_count=4,
        include_aborted=True,
        include_accusations=True,
    )


@pytest.fixture
def synthetic_all_aborted():
    """Fixture: 5 games, all aborted."""
    results = []
    for i in range(5):
        result = SyntheticGameResult(
            game_id=f"game_{i}",
            seed=42 + i,
            status="aborted",
            abort_reason="BackendUnavailableError",
        )
        results.append(result.to_real_game_result())
    return results


@pytest.fixture
def synthetic_no_accusations():
    """Fixture: 10 games, no formal accusations."""
    return create_synthetic_results(
        num_games=10,
        catch_count=4,
        include_aborted=False,
        include_accusations=False,
    )


# ============================================================================
# CONTRACT COMPLIANCE TESTS
# ============================================================================


class TestContractCompliance:
    """Verify function signatures match the contract."""

    def test_run_batch_exists_and_callable(self):
        """run_batch(batch_config: dict, game_factory: callable) -> list[GameResult] must exist."""
        assert callable(run_batch)

    def test_compute_metrics_exists_and_callable(self):
        """compute_metrics(results: list[GameResult]) -> MetricsReport must exist."""
        assert callable(compute_metrics)

    def test_write_report_exists_and_callable(self):
        """write_report(report: MetricsReport, output_dir: Path) -> tuple[Path, Path] must exist."""
        assert callable(write_report)

    def test_metrics_report_dataclass_fields(self):
        """MetricsReport must have required fields."""
        # Create a minimal report to verify structure
        report = MetricsReport(
            catch_rate=0.5,
            mean_exchanges=12.5,
            accusation_usage={
                "games_with_formal_accusation": 5,
                "mean_formal_accusations_per_game": 1.0,
                "fraction_of_games": 0.5,
            },
            games_completed=10,
            games_aborted=[],
            game_summaries=[],
        )
        assert hasattr(report, "catch_rate")
        assert hasattr(report, "mean_exchanges")
        assert hasattr(report, "accusation_usage")
        assert hasattr(report, "games_completed")
        assert hasattr(report, "games_aborted")
        assert hasattr(report, "game_summaries")


# ============================================================================
# COMPUTE_METRICS BEHAVIORAL TESTS
# ============================================================================


class TestComputeMetrics:
    """Test compute_metrics pure aggregation function."""

    def test_compute_metrics_exact_catch_rate_10_games(self, synthetic_10_games):
        """
        compute_metrics on 10 games (4 caught, 5 not caught, 1 aborted)
        yields exact expected catch_rate = 4/9 (aborted excluded).
        """
        report = compute_metrics(synthetic_10_games)

        assert isinstance(report, MetricsReport)
        # 4 caught out of 9 completed (1 aborted excluded)
        expected_catch_rate = 4 / 9
        assert abs(report.catch_rate - expected_catch_rate) < 0.001
        assert report.games_completed == 9

    def test_compute_metrics_mean_exchanges_excludes_aborted(self, synthetic_10_games):
        """
        compute_metrics excludes aborted game from mean_exchanges calculation.
        Aborted game has exchange_count=0, so mean should be calculated over 9 games only.
        """
        report = compute_metrics(synthetic_10_games)

        # Exchange counts for first 9 games: [10, 15, 12, 18, 11, 14, 13, 16, 12]
        # Mean = (10+15+12+18+11+14+13+16+12) / 9 = 121 / 9 ≈ 13.44
        expected_mean = (10 + 15 + 12 + 18 + 11 + 14 + 13 + 16 + 12) / 9
        assert abs(report.mean_exchanges - expected_mean) < 0.1

    def test_compute_metrics_aborted_games_listed_separately(self, synthetic_10_games):
        """
        compute_metrics lists aborted games in games_aborted with reason.
        """
        report = compute_metrics(synthetic_10_games)

        assert len(report.games_aborted) == 1
        aborted = report.games_aborted[0]
        assert aborted["game_id"] == "game_9"
        assert aborted["abort_reason"] == "BackendUnavailableError: service timeout"
        assert aborted["status"] == "aborted"

    def test_compute_metrics_zero_completed_games_returns_none_catch_rate(self, synthetic_all_aborted):
        """
        compute_metrics with zero completed games returns catch_rate=None
        (no ZeroDivisionError).
        """
        report = compute_metrics(synthetic_all_aborted)

        assert report.catch_rate is None
        assert report.games_completed == 0
        assert len(report.games_aborted) == 5
        # mean_exchanges should also be None or 0 when no completed games
        assert report.mean_exchanges is None or report.mean_exchanges == 0

    def test_compute_metrics_accusation_usage_counts_games_with_accusations(self, synthetic_10_games):
        """
        compute_metrics.accusation_usage correctly counts:
        - games_with_formal_accusation: number of completed games with ≥1 formal accusation
        - mean_formal_accusations_per_game: total accusations / completed games
        - fraction_of_games: (games with accusations) / (completed games)
        """
        report = compute_metrics(synthetic_10_games)

        # In synthetic_10_games, games 0, 2, 4, 6, 8 have accusations (5 games out of 9 completed)
        # Each has 2 accusations, so total = 10
        # Games with accusations = 5 (excluding aborted game 9)
        assert report.accusation_usage["games_with_formal_accusation"] == 5
        assert report.accusation_usage["mean_formal_accusations_per_game"] == (10 / 9)
        assert abs(
            report.accusation_usage["fraction_of_games"] - (5 / 9)
        ) < 0.001

    def test_compute_metrics_accusation_usage_no_accusations(self, synthetic_no_accusations):
        """
        compute_metrics with no formal accusations sets accusation_usage correctly.
        """
        report = compute_metrics(synthetic_no_accusations)

        assert report.accusation_usage["games_with_formal_accusation"] == 0
        assert report.accusation_usage["mean_formal_accusations_per_game"] == 0
        assert report.accusation_usage["fraction_of_games"] == 0

    def test_compute_metrics_game_summaries_excludes_aborted(self, synthetic_10_games):
        """
        compute_metrics.game_summaries contains one row per COMPLETED game
        with game_id, seed, traitor_caught, exchange_count, most_accused.
        """
        report = compute_metrics(synthetic_10_games)

        assert len(report.game_summaries) == 9
        # Check first summary
        first = report.game_summaries[0]
        assert first["game_id"] == "game_0"
        assert first["seed"] == 42
        assert first["traitor_caught"] is True
        assert first["exchange_count"] == 10

    def test_compute_metrics_catch_rate_one_hundred_percent(self):
        """
        compute_metrics on all-caught games yields catch_rate=1.0.
        """
        results = []
        for i in range(5):
            result = SyntheticGameResult(
                game_id=f"game_{i}",
                seed=42 + i,
                traitor_caught=True,
                exchange_count=10,
                status="completed",
            )
            results.append(result.to_real_game_result())

        report = compute_metrics(results)
        assert report.catch_rate == 1.0

    def test_compute_metrics_catch_rate_zero_percent(self):
        """
        compute_metrics on no-caught games yields catch_rate=0.0.
        """
        results = []
        for i in range(5):
            result = SyntheticGameResult(
                game_id=f"game_{i}",
                seed=42 + i,
                traitor_caught=False,
                exchange_count=10,
                status="completed",
            )
            results.append(result.to_real_game_result())

        report = compute_metrics(results)
        assert report.catch_rate == 0.0


# ============================================================================
# RUN_BATCH BEHAVIORAL TESTS
# ============================================================================


class TestRunBatch:
    """Test run_batch game orchestration."""

    def test_run_batch_with_all_aborting_factory(self, output_dir):
        """
        run_batch with a game_factory that always aborts
        returns all-aborted results, no exception.
        """

        def always_abort_factory(seed: int, game_id: str) -> Tuple:
            """Game factory that always aborts immediately."""
            raise GameAbortedError("Simulated backend failure")

        batch_config = {
            "num_games": 3,
            "seed": 42,
            "output_dir": str(output_dir),
        }

        results = run_batch(batch_config, always_abort_factory)

        assert len(results) == 3
        assert all(r.status == "aborted" for r in results)
        assert all(r.abort_reason == "Simulated backend failure" for r in results)

    def test_run_batch_config_error_on_num_games_zero(self, output_dir):
        """
        run_batch raises ConfigError when num_games < 1.
        """

        def dummy_factory(seed: int, game_id: str) -> Tuple:
            return None

        batch_config = {
            "num_games": 0,
            "seed": 42,
            "output_dir": str(output_dir),
        }

        with pytest.raises(Exception):  # ConfigError
            run_batch(batch_config, dummy_factory)

    def test_run_batch_config_error_on_negative_num_games(self, output_dir):
        """
        run_batch raises ConfigError when num_games < 0.
        """

        def dummy_factory(seed: int, game_id: str) -> Tuple:
            return None

        batch_config = {
            "num_games": -1,
            "seed": 42,
            "output_dir": str(output_dir),
        }

        with pytest.raises(Exception):  # ConfigError
            run_batch(batch_config, dummy_factory)

    def test_run_batch_mixed_completed_and_aborted(self, output_dir):
        """
        run_batch with a factory that completes some games and aborts others
        returns mixed results.
        """
        completed_ids = {0, 2}  # Games 0 and 2 complete; 1 aborts

        def mixed_factory(seed: int, game_id: str) -> Tuple:
            game_num = int(game_id.split("_")[1])
            if game_num in completed_ids:
                # Return minimal game objects (mocked)
                return (Mock(), Mock(), {"seed": seed})
            else:
                raise GameAbortedError(f"Game {game_num} intentionally aborted")

        batch_config = {
            "num_games": 3,
            "seed": 42,
            "output_dir": str(output_dir),
        }

        # Mock the orchestrator's run_game to return completed GameResults
        with patch("traitors_mobile.metrics.run_game") as mock_run_game:

            def run_game_side_effect(*args, **kwargs):
                # Check which game_id was passed; return completed or aborted
                game_id = kwargs.get("game_id") or (args[0] if args else None)
                if hasattr(game_id, "split") and int(game_id.split("_")[1]) in completed_ids:
                    return SyntheticGameResult(
                        game_id=game_id,
                        seed=42,
                        status="completed",
                    ).to_real_game_result()
                else:
                    raise GameAbortedError("Simulated failure")

            mock_run_game.side_effect = run_game_side_effect

            results = run_batch(batch_config, mixed_factory)

            # Should have 3 results total
            assert len(results) == 3
            completed = [r for r in results if r.status == "completed"]
            aborted = [r for r in results if r.status == "aborted"]
            assert len(completed) == 2
            assert len(aborted) == 1


# ============================================================================
# WRITE_REPORT BEHAVIORAL TESTS
# ============================================================================


class TestWriteReport:
    """Test write_report file generation."""

    def test_write_report_produces_two_files(self, output_dir, synthetic_10_games):
        """
        write_report produces two real files:
        metrics_report.json and metrics_report.md in output_dir.
        """
        report = compute_metrics(synthetic_10_games)
        json_path, md_path = write_report(report, output_dir)

        assert json_path.exists()
        assert md_path.exists()
        assert json_path.name == "metrics_report.json"
        assert md_path.name == "metrics_report.md"
        assert json_path.parent == output_dir
        assert md_path.parent == output_dir

    def test_write_report_json_is_valid_json(self, output_dir, synthetic_10_games):
        """
        write_report produces a valid JSON file that parses and contains
        expected top-level keys.
        """
        report = compute_metrics(synthetic_10_games)
        json_path, _ = write_report(report, output_dir)

        with open(json_path, "r") as f:
            data = json.load(f)

        assert "catch_rate" in data
        assert "mean_exchanges" in data
        assert "accusation_usage" in data
        assert "games_completed" in data
        assert "games_aborted" in data
        assert "game_summaries" in data

    def test_write_report_json_games_completed_matches_summaries(
        self, output_dir, synthetic_10_games
    ):
        """
        write_report: games_completed in JSON equals number of game_summaries.
        """
        report = compute_metrics(synthetic_10_games)
        json_path, _ = write_report(report, output_dir)

        with open(json_path, "r") as f:
            data = json.load(f)

        assert data["games_completed"] == len(data["game_summaries"])
        assert data["games_completed"] == 9  # 1 aborted

    def test_write_report_markdown_is_readable(self, output_dir, synthetic_10_games):
        """
        write_report produces a markdown file with headline numbers and per-game table.
        """
        report = compute_metrics(synthetic_10_games)
        _, md_path = write_report(report, output_dir)

        with open(md_path, "r") as f:
            content = f.read()

        # Should contain markdown structure (headers, tables, etc.)
        assert "Metrics Report" in content or "metrics" in content.lower()
        assert str(content).strip()  # Non-empty

    def test_write_report_creates_output_dir_if_missing(self):
        """
        write_report creates output_dir if it doesn't exist.
        """
        with tempfile.TemporaryDirectory() as parent:
            output_dir = Path(parent) / "does_not_exist"
            assert not output_dir.exists()

            results = create_synthetic_results(num_games=3, include_aborted=False)
            report = compute_metrics(results)
            json_path, md_path = write_report(report, output_dir)

            assert output_dir.exists()
            assert json_path.exists()
            assert md_path.exists()

    def test_write_report_json_games_aborted_with_reason(self, output_dir, synthetic_10_games):
        """
        write_report JSON includes games_aborted list with game_id and abort_reason.
        """
        report = compute_metrics(synthetic_10_games)
        json_path, _ = write_report(report, output_dir)

        with open(json_path, "r") as f:
            data = json.load(f)

        assert len(data["games_aborted"]) == 1
        aborted_game = data["games_aborted"][0]
        assert "game_id" in aborted_game
        assert "abort_reason" in aborted_game
        assert aborted_game["status"] == "aborted"

    def test_write_report_json_catch_rate_none_all_aborted(self, output_dir, synthetic_all_aborted):
        """
        write_report with all-aborted games writes catch_rate as null/None in JSON.
        """
        report = compute_metrics(synthetic_all_aborted)
        json_path, _ = write_report(report, output_dir)

        with open(json_path, "r") as f:
            data = json.load(f)

        assert data["catch_rate"] is None
        assert data["games_completed"] == 0
        assert len(data["games_aborted"]) == 5


# ============================================================================
# CROSS-VALIDATION TESTS
# ============================================================================


class TestCrossValidation:
    """Test cross-validation between report and filesystem."""

    def test_write_report_json_games_completed_matches_disk_result_files(self, output_dir):
        """
        write_report: games_completed in report matches number of game_*.result.json
        files on disk (cross-check with filesystem).
        """
        # Create some dummy game result files on disk
        completed_count = 5
        for i in range(completed_count):
            result_file = output_dir / f"game_{i}.result.json"
            result_file.write_text(json.dumps({"game_id": f"game_{i}"}))

        # Create a report with 5 completed games
        results = create_synthetic_results(num_games=5, include_aborted=False)
        report = compute_metrics(results)
        json_path, _ = write_report(report, output_dir)

        # Verify the counts match
        with open(json_path, "r") as f:
            data = json.load(f)

        result_files = list(output_dir.glob("game_*.result.json"))
        assert len(result_files) == completed_count
        assert data["games_completed"] == completed_count

    def test_write_report_catches_unplanned_disk_files(self, output_dir):
        """
        If extra game_*.result.json files are on disk, write_report
        produces a report whose games_completed still reflects the
        aggregated metric (not the disk count), but the metrics should
        be validated against the actual computed games.
        """
        # Create some dummy files
        for i in range(3):
            (output_dir / f"game_{i}.result.json").write_text("{}")

        # Create report with different count
        results = create_synthetic_results(num_games=2, include_aborted=False)
        report = compute_metrics(results)
        json_path, _ = write_report(report, output_dir)

        with open(json_path, "r") as f:
            data = json.load(f)

        # Report should reflect the 2 completed games we computed, not the 3 files
        assert data["games_completed"] == 2


# ============================================================================
# INTEGRATION AND EDGE CASES
# ============================================================================


class TestIntegrationAndEdgeCases:
    """Integration and edge-case tests."""

    def test_end_to_end_compute_and_write(self, output_dir):
        """
        End-to-end: create synthetic results → compute_metrics → write_report.
        Verify the full pipeline works.
        """
        results = create_synthetic_results(
            num_games=15,
            catch_count=7,
            include_aborted=True,
            include_accusations=True,
        )

        report = compute_metrics(results)
        json_path, md_path = write_report(report, output_dir)

        # Verify files exist and contain expected structure
        assert json_path.exists() and md_path.exists()

        with open(json_path, "r") as f:
            data = json.load(f)

        assert data["games_completed"] == 14  # 1 aborted
        assert data["catch_rate"] is not None
        assert data["mean_exchanges"] is not None

    def test_large_batch_metric_aggregation(self):
        """
        compute_metrics on a large batch (100 games) performs correctly
        without performance issues.
        """
        results = create_synthetic_results(
            num_games=100,
            catch_count=50,
            include_aborted=False,
        )

        report = compute_metrics(results)

        assert report.games_completed == 100
        assert abs(report.catch_rate - 0.5) < 0.01
        assert report.mean_exchanges > 0

    def test_single_game_in_batch(self):
        """
        compute_metrics on a single completed game works correctly.
        """
        results = create_synthetic_results(num_games=1, include_aborted=False)
        report = compute_metrics(results)

        assert report.games_completed == 1
        assert report.catch_rate in [0.0, 1.0]  # Either caught or not

    def test_many_accusations_per_game(self):
        """
        compute_metrics correctly aggregates when games have many accusations.
        """
        results = []
        for i in range(5):
            accusations = [
                Accusation(turn=j, accuser="player_1", target="player_0", reason=f"turn {j}")
                for j in range(10)  # 10 accusations per game
            ]
            result = SyntheticGameResult(
                game_id=f"game_{i}",
                seed=42 + i,
                accusations=accusations,
                status="completed",
            )
            results.append(result.to_real_game_result())

        report = compute_metrics(results)

        # All 5 games have accusations
        assert report.accusation_usage["games_with_formal_accusation"] == 5
        # Total = 50 accusations / 5 games = 10 per game
        assert report.accusation_usage["mean_formal_accusations_per_game"] == 10.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
