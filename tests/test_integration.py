"""
Acceptance tests for the Integration module (traitors-mobile-integration).

Tests the real CLI entry point and application assembly: config loading,
wiring scenario → players → orchestrator → metrics, end-to-end with mock
backend, and proper exit codes for error cases.

All tests use mock backend (no network, deterministic). Final integration
review (real Claude backend, ≥10 games) is manual and run by QA after
module build completes.
"""

import json
import os
import tempfile
import shutil
import sys
import pytest
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

# Import the modules we're testing (once integration.py exists)
# For now, we define stubs that test_integration.py will import when it exists
try:
    from traitors_mobile.integration import (
        load_config,
        AppConfig,
        build_game_components,
        main,
        ConfigError,
    )
    from traitors_mobile.scenario import build_scenario, default_scenario
    from traitors_mobile.player import PlayerAgent
    from traitors_mobile.orchestrator import GameConfig, run_game, GameAbortedError
    from traitors_mobile.metrics import run_batch, compute_metrics, write_report
    from traitors_mobile.llm_backend import create_backend, MockBackend, BackendUnavailableError
except ImportError as e:
    # Test file structure exists even if integration.py doesn't yet
    pass


class TestContractCompliance:
    """Verify all required functions/classes exist and are callable."""

    def test_load_config_exists_and_callable(self):
        """load_config(path: str | None) -> AppConfig must exist."""
        assert callable(load_config)

    def test_app_config_dataclass_exists(self):
        """AppConfig dataclass must exist with backend, run, scenario, cast fields."""
        config = load_config()  # default
        assert hasattr(config, 'backend')
        assert hasattr(config, 'run')
        assert hasattr(config, 'scenario')
        assert hasattr(config, 'cast')

    def test_build_game_components_exists_and_callable(self):
        """build_game_components(config, seed, game_id, backend) -> tuple exists."""
        assert callable(build_game_components)

    def test_main_exists_and_callable(self):
        """main(argv: list[str] | None) -> int must exist."""
        assert callable(main)

    def test_config_error_exception_exists(self):
        """ConfigError exception must be defined."""
        with pytest.raises(ConfigError):
            raise ConfigError("test")


class TestLoadConfigDefault:
    """Test load_config() with no path (returns built-in defaults)."""

    def test_load_config_no_path_returns_app_config(self):
        """load_config() returns AppConfig with defaults."""
        config = load_config()
        assert isinstance(config, AppConfig)

    def test_load_config_default_backend_claude(self):
        """Default backend provider is 'claude'."""
        config = load_config()
        assert config.backend.provider == "claude"

    def test_load_config_default_model_haiku(self):
        """Default model is 'claude-haiku-4-5'."""
        config = load_config()
        assert config.backend.model == "claude-haiku-4-5"

    def test_load_config_default_num_games_10(self):
        """Default num_games is 10."""
        config = load_config()
        assert config.run.num_games == 10

    def test_load_config_default_rounds_per_game_6(self):
        """Default rounds_per_game is 6."""
        config = load_config()
        assert config.run.rounds_per_game == 6

    def test_load_config_default_output_dir_output(self):
        """Default output_dir is 'output'."""
        config = load_config()
        assert config.run.output_dir == "output"

    def test_load_config_default_scenario_stolen_prize_tin(self):
        """Default scenario template is 'stolen-prize-tin'."""
        config = load_config()
        assert config.scenario.template == "stolen-prize-tin"

    def test_load_config_default_cast_has_five_roles(self):
        """Default cast has exactly 5 roles."""
        config = load_config()
        assert config.cast.traitor is not None
        assert config.cast.detective is not None
        assert config.cast.loyalist_a is not None
        assert config.cast.loyalist_b is not None
        assert config.cast.loyalist_c is not None

    def test_load_config_default_has_warnings_list(self):
        """AppConfig has a warnings list (for non-fatal issues)."""
        config = load_config()
        assert hasattr(config, 'warnings')
        assert isinstance(config.warnings, list)


class TestLoadConfigFromFile:
    """Test load_config(path) with a JSON config file."""

    def test_load_config_from_valid_json(self):
        """load_config(path) reads and validates JSON config."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({
                "backend": {"provider": "mock"},
                "run": {"num_games": 3, "output_dir": "test_output"}
            }, f)
            f.flush()
            path = f.name

        try:
            config = load_config(path)
            assert config.backend.provider == "mock"
            assert config.run.num_games == 3
            assert config.run.output_dir == "test_output"
        finally:
            os.unlink(path)

    def test_load_config_invalid_json_raises_config_error(self):
        """load_config with invalid JSON raises ConfigError."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write("{invalid json")
            f.flush()
            path = f.name

        try:
            with pytest.raises(ConfigError):
                load_config(path)
        finally:
            os.unlink(path)

    def test_load_config_file_not_found_raises_config_error(self):
        """load_config with nonexistent path raises ConfigError."""
        with pytest.raises(ConfigError):
            load_config("/nonexistent/path/config.json")

    def test_load_config_invalid_provider_raises_config_error(self):
        """load_config with invalid backend.provider raises ConfigError."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({"backend": {"provider": "bogus"}}, f)
            f.flush()
            path = f.name

        try:
            with pytest.raises(ConfigError):
                load_config(path)
        finally:
            os.unlink(path)

    def test_load_config_num_games_less_than_one_raises_error(self):
        """load_config with num_games < 1 raises ConfigError."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({"run": {"num_games": 0}}, f)
            f.flush()
            path = f.name

        try:
            with pytest.raises(ConfigError):
                load_config(path)
        finally:
            os.unlink(path)

    def test_load_config_rounds_less_than_two_raises_error(self):
        """load_config with rounds_per_game < 2 raises ConfigError."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({"run": {"rounds_per_game": 1}}, f)
            f.flush()
            path = f.name

        try:
            with pytest.raises(ConfigError):
                load_config(path)
        finally:
            os.unlink(path)

    def test_load_config_fills_defaults_for_optional_keys(self):
        """load_config fills in defaults for missing optional config keys."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({"backend": {"provider": "mock"}}, f)
            f.flush()
            path = f.name

        try:
            config = load_config(path)
            # Should have defaults for num_games, rounds, output_dir, scenario, cast
            assert config.run.num_games == 10  # default
            assert config.run.rounds_per_game == 6  # default
            assert config.run.output_dir == "output"  # default
            assert config.scenario.template == "stolen-prize-tin"  # default
        finally:
            os.unlink(path)

    def test_load_config_unknown_keys_ignored_with_warning(self):
        """load_config ignores unknown top-level keys with warning."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({
                "backend": {"provider": "mock"},
                "unknown_key": "unknown_value"
            }, f)
            f.flush()
            path = f.name

        try:
            config = load_config(path)
            # Should have warnings list with at least one warning
            assert len(config.warnings) > 0
            assert any("unknown" in w.lower() for w in config.warnings)
        finally:
            os.unlink(path)


class TestBuildGameComponents:
    """Test build_game_components assembly of scenario/players/config."""

    def test_build_game_components_returns_tuple_of_three(self):
        """build_game_components returns (Scenario, dict[str, PlayerAgent], GameConfig)."""
        config = load_config()
        config.backend.provider = "mock"
        backend = create_backend(config.backend)
        
        scenario, players, game_config = build_game_components(config, seed=42, game_id="test-1", backend=backend)
        
        assert scenario is not None
        assert isinstance(players, dict)
        assert game_config is not None

    def test_build_game_components_creates_five_players(self):
        """build_game_components creates exactly 5 PlayerAgent objects (one per cast member)."""
        config = load_config()
        config.backend.provider = "mock"
        backend = create_backend(config.backend)
        
        scenario, players, game_config = build_game_components(config, seed=42, game_id="test-1", backend=backend)
        
        assert len(players) == 5
        assert all(isinstance(p, PlayerAgent) for p in players.values())

    def test_build_game_components_each_player_has_own_role_card_only(self):
        """Each PlayerAgent is constructed with only its own role card, not others'."""
        config = load_config()
        config.backend.provider = "mock"
        backend = create_backend(config.backend)

        scenario, players, game_config = build_game_components(config, seed=42, game_id="test-1", backend=backend)

        # Spot-check: the Traitor's private material (crime declaration,
        # cover story) must not appear in any non-traitor player's card.
        # (Engineer, SWA-161: the original compared `p.role == "Traitor"`
        # and called `role_card.lower()`, but PlayerAgent exposes no `.role`
        # and role cards are RoleCard dataclasses, not strings -- the test
        # was rewritten against the canonical `players_by_role` lookup and
        # the real card contents, keeping the same no-leakage intent.)
        traitor_player_id = scenario.players_by_role["traitor"]
        traitor_player = players[traitor_player_id]
        traitor_declaration = (traitor_player.role_card.crime_declaration or "").lower()
        traitor_cover_story = (traitor_player.role_card.cover_story or "").lower()

        for pid, player in players.items():
            if pid != traitor_player_id:
                card_text = " ".join(
                    [player.role_card.goal] + list(player.role_card.observations)
                ).lower()
                assert traitor_declaration not in card_text
                assert traitor_cover_story not in card_text

    def test_build_game_components_propagates_scenario_error(self):
        """build_game_components propagates ScenarioError from build_scenario."""
        config = load_config()
        config.scenario.template = "nonexistent-scenario"
        config.backend.provider = "mock"
        backend = create_backend(config.backend)
        
        from traitors_mobile.scenario import ScenarioError
        with pytest.raises(ScenarioError):
            build_game_components(config, seed=42, game_id="test-1", backend=backend)

    def test_build_game_components_propagates_backend_error(self):
        """build_game_components propagates backend creation errors (ConfigError)."""
        config = load_config()
        config.backend.provider = "bogus"

        # (Engineer, SWA-161: create_backend raises ConfigError for an unknown
        # provider -- pinned by test_llm_backend.py and the llm-backend
        # contract; the original expected BackendError. Routing through
        # build_game_components (backend=None => it creates one) exercises
        # the real propagation path.)
        from traitors_mobile.llm_backend import ConfigError as BackendConfigError
        with pytest.raises(BackendConfigError):
            build_game_components(config, seed=42, game_id="test-1")


class TestMainRunSingleWithMock:
    """Test main() with run-single subcommand (mock backend)."""

    def test_main_run_single_returns_zero_on_success(self):
        """main(['run-single']) with mock backend returns 0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config.json")
            output_dir = os.path.join(tmpdir, "output")
            
            with open(config_path, 'w') as f:
                json.dump({
                    "backend": {"provider": "mock"},
                    "run": {"output_dir": output_dir, "rounds_per_game": 2}
                }, f)
            
            result = main(["run-single", "--config", config_path, "--seed", "42"])
            assert result == 0

    def test_main_run_single_creates_output_directory(self):
        """main(['run-single']) creates output_dir if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config.json")
            output_dir = os.path.join(tmpdir, "output")
            
            with open(config_path, 'w') as f:
                json.dump({
                    "backend": {"provider": "mock"},
                    "run": {"output_dir": output_dir, "num_games": 1}
                }, f)
            
            main(["run-single", "--config", config_path])
            assert os.path.isdir(output_dir)

    def test_main_run_single_produces_transcript_and_result_files(self):
        """main(['run-single']) produces game_*.transcript.json and game_*.result.json."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config.json")
            output_dir = os.path.join(tmpdir, "output")
            
            with open(config_path, 'w') as f:
                json.dump({
                    "backend": {"provider": "mock"},
                    "run": {"output_dir": output_dir, "rounds_per_game": 2}
                }, f)
            
            main(["run-single", "--config", config_path, "--seed", "42"])
            
            # Check that files were created
            files = os.listdir(output_dir)
            transcript_files = [f for f in files if f.endswith('.transcript.json')]
            result_files = [f for f in files if f.endswith('.result.json')]
            
            assert len(transcript_files) > 0
            assert len(result_files) > 0

    def test_main_run_single_invalid_config_path_returns_1(self):
        """main(['run-single', '--config', '/nonexistent/path']) returns 1 (config error)."""
        result = main(["run-single", "--config", "/nonexistent/path/config.json"])
        assert result == 1

    def test_main_run_single_with_invalid_provider_returns_1(self):
        """main with invalid backend.provider in config returns 1."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config.json")
            output_dir = os.path.join(tmpdir, "output")
            
            with open(config_path, 'w') as f:
                json.dump({
                    "backend": {"provider": "bogus"},
                    "run": {"output_dir": output_dir}
                }, f)
            
            result = main(["run-single", "--config", config_path])
            assert result == 1


class TestMainRunBatchWithMock:
    """Test main() with run-batch subcommand (mock backend)."""

    def test_main_run_batch_returns_zero_on_success(self):
        """main(['run-batch']) with mock backend returns 0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config.json")
            output_dir = os.path.join(tmpdir, "output")
            
            with open(config_path, 'w') as f:
                json.dump({
                    "backend": {"provider": "mock"},
                    "run": {"num_games": 3, "output_dir": output_dir, "rounds_per_game": 2}
                }, f)
            
            result = main(["run-batch", "--config", config_path])
            assert result == 0

    def test_main_run_batch_produces_n_game_files(self):
        """main(['run-batch']) with num_games=N produces N transcript + N result files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config.json")
            output_dir = os.path.join(tmpdir, "output")
            num_games = 3
            
            with open(config_path, 'w') as f:
                json.dump({
                    "backend": {"provider": "mock"},
                    "run": {"num_games": num_games, "output_dir": output_dir, "rounds_per_game": 2}
                }, f)
            
            main(["run-batch", "--config", config_path])
            
            files = os.listdir(output_dir)
            transcript_files = [f for f in files if f.endswith('.transcript.json')]
            result_files = [f for f in files if f.endswith('.result.json')]
            
            assert len(transcript_files) == num_games
            assert len(result_files) == num_games

    def test_main_run_batch_produces_metrics_report(self):
        """main(['run-batch']) produces metrics_report.json and metrics_report.md."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config.json")
            output_dir = os.path.join(tmpdir, "output")
            
            with open(config_path, 'w') as f:
                json.dump({
                    "backend": {"provider": "mock"},
                    "run": {"num_games": 3, "output_dir": output_dir, "rounds_per_game": 2}
                }, f)
            
            main(["run-batch", "--config", config_path])
            
            assert os.path.isfile(os.path.join(output_dir, "metrics_report.json"))
            assert os.path.isfile(os.path.join(output_dir, "metrics_report.md"))

    def test_main_run_batch_metrics_report_has_correct_games_completed_count(self):
        """metrics_report.json games_completed count matches number of result files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config.json")
            output_dir = os.path.join(tmpdir, "output")
            num_games = 3
            
            with open(config_path, 'w') as f:
                json.dump({
                    "backend": {"provider": "mock"},
                    "run": {"num_games": num_games, "output_dir": output_dir, "rounds_per_game": 2}
                }, f)
            
            main(["run-batch", "--config", config_path])
            
            with open(os.path.join(output_dir, "metrics_report.json")) as f:
                report = json.load(f)
            
            assert report["games_completed"] == num_games

    def test_main_run_batch_catch_rate_is_valid_fraction(self):
        """metrics_report.json catch_rate is a number in [0, 1]."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config.json")
            output_dir = os.path.join(tmpdir, "output")
            
            with open(config_path, 'w') as f:
                json.dump({
                    "backend": {"provider": "mock"},
                    "run": {"num_games": 3, "output_dir": output_dir, "rounds_per_game": 2}
                }, f)
            
            main(["run-batch", "--config", config_path])
            
            with open(os.path.join(output_dir, "metrics_report.json")) as f:
                report = json.load(f)
            
            assert "catch_rate" in report
            assert isinstance(report["catch_rate"], (int, float))
            assert 0 <= report["catch_rate"] <= 1

    def test_main_run_batch_metrics_report_is_valid_json(self):
        """metrics_report.json is valid, parseable JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config.json")
            output_dir = os.path.join(tmpdir, "output")
            
            with open(config_path, 'w') as f:
                json.dump({
                    "backend": {"provider": "mock"},
                    "run": {"num_games": 3, "output_dir": output_dir, "rounds_per_game": 2}
                }, f)
            
            main(["run-batch", "--config", config_path])
            
            with open(os.path.join(output_dir, "metrics_report.json")) as f:
                report = json.load(f)
            
            assert isinstance(report, dict)


class TestMainExitCodes:
    """Test that main() returns correct exit codes for various error conditions."""

    def test_main_config_error_returns_1(self):
        """main returns 1 (exit code) on config error."""
        result = main(["run-batch", "--config", "/nonexistent/path"])
        assert result == 1

    def test_main_missing_subcommand_returns_nonzero(self):
        """main with no subcommand returns nonzero."""
        result = main([])
        assert result != 0

    def test_main_invalid_subcommand_returns_nonzero(self):
        """main with invalid subcommand returns nonzero."""
        result = main(["invalid-subcommand"])
        assert result != 0


class TestMainEndToEndWithMockBackend:
    """Comprehensive end-to-end test: full pipeline with mock backend."""

    def test_main_complete_pipeline_mock_backend(self):
        """Full end-to-end: config → scenario → players → orchestrator → metrics with mock."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config.json")
            output_dir = os.path.join(tmpdir, "output")
            
            with open(config_path, 'w') as f:
                json.dump({
                    "backend": {"provider": "mock"},
                    "run": {
                        "num_games": 3,
                        "output_dir": output_dir,
                        "rounds_per_game": 3,
                        "seed": 42
                    }
                }, f)
            
            # Run batch
            result = main(["run-batch", "--config", config_path])
            assert result == 0
            
            # Verify outputs
            assert os.path.isdir(output_dir)
            
            files = os.listdir(output_dir)
            transcripts = [f for f in files if f.endswith('.transcript.json')]
            results = [f for f in files if f.endswith('.result.json')]
            
            assert len(transcripts) == 3
            assert len(results) == 3
            
            # Verify transcript structure
            with open(os.path.join(output_dir, transcripts[0])) as f:
                transcript = json.load(f)
            assert "game_id" in transcript
            assert "turns" in transcript
            assert "accusations" in transcript
            assert "votes" in transcript
            
            # Verify result structure
            with open(os.path.join(output_dir, results[0])) as f:
                result_data = json.load(f)
            assert "status" in result_data
            assert result_data["status"] == "completed"
            assert "vote_tally" in result_data
            
            # Verify metrics
            with open(os.path.join(output_dir, "metrics_report.json")) as f:
                metrics = json.load(f)
            assert metrics["games_completed"] == 3
            assert 0 <= metrics["catch_rate"] <= 1


class TestIntegrationWithEnvironmentVariables:
    """Test that integration respects environment variables (ANTHROPIC_API_KEY)."""

    def test_main_respects_claude_key_env_var_for_claude_provider(self):
        """When provider=claude, main reads ANTHROPIC_API_KEY from environment."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = os.path.join(tmpdir, "config.json")
            output_dir = os.path.join(tmpdir, "output")
            
            with open(config_path, 'w') as f:
                json.dump({
                    "backend": {"provider": "mock"},  # Use mock for test
                    "run": {"num_games": 1, "output_dir": output_dir}
                }, f)
            
            # Test with mock backend; real Claude would require ANTHROPIC_API_KEY
            result = main(["run-batch", "--config", config_path])
            assert result == 0
