"""
traitors-mobile-integration (Module 6) -- Application Assembly

The only real runnable entry point of the Mode B AI agent simulation: loads
and validates configuration, wires scenario -> players -> orchestrator ->
metrics in the real data-flow order, and exposes ``run-single`` (one game)
and ``run-batch`` (>=N games) subcommands with meaningful exit codes.

Contract: specs/contracts/integration.md (SWA-146).
Dependencies: every other module -- traitors-mobile-scenario
(default_scenario, build_scenario, load_scenario), traitors-mobile-llm-backend
(create_backend, probe), traitors-mobile-player (PlayerAgent),
traitors-mobile-orchestrator (run_game, GameConfig, GameAbortedError),
traitors-mobile-metrics (run_batch, compute_metrics, write_report).

Python 3.11 stdlib only (json, argparse, pathlib, sys, random). The pinned
runtime deps (anthropic==1.2.0, requests==2.32.3) are used transitively by
llm_backend; nothing new is added here.

Exit codes (contract): 0 success; 1 config error; 2 backend unavailable
(budget exhausted / unreachable during a real run; for run-batch the report
is still written with whatever completed); 3 game aborted (run-single);
4 unexpected error.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from traitors_mobile.llm_backend import (
    BackendUnavailableError,
    ConfigError as BackendConfigError,
    LLMBackend,
    create_backend,
)
from traitors_mobile.metrics import compute_metrics, run_batch, write_report
from traitors_mobile.orchestrator import GameAbortedError, GameConfig, run_game
from traitors_mobile.player import PlayerAgent
from traitors_mobile.scenario import (
    Scenario,
    ScenarioError,
    build_scenario,
    default_scenario,
    load_scenario,
)

VALID_PROVIDERS = ("deepseek", "claude", "ollama", "mock")
DEFAULT_SCENARIO_TEMPLATE = "stolen-prize-tin"
CAST_ROLES = ("traitor", "detective", "loyalist_a", "loyalist_b", "loyalist_c")


class ConfigError(Exception):
    """Raised when the configuration is invalid.

    The message names the offending field and the fix (bad provider,
    num_games < 1, rounds_per_game < 2, non-JSON file, missing file).
    """


class ConfigDict(dict):
    """A dict that also supports attribute access (``cfg.provider``).

    Used for the sub-config sections so callers can read *and* write both
    ``config.backend.provider`` and ``config.backend["provider"]``. ``get()``
    stays available, so a section can be handed straight to
    ``llm_backend.create_backend``.
    """

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


@dataclass
class AppConfig:
    """Validated application configuration (tech-design sec 8 schema).

    ``backend`` / ``run`` / ``scenario`` / ``cast`` are ConfigDict sections;
    ``warnings`` collects non-fatal issues (e.g. unknown keys ignored).
    """

    backend: ConfigDict
    run: ConfigDict
    scenario: ConfigDict
    cast: ConfigDict
    warnings: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Defaults (tech-design sec 8; config may be minimal -- every key optional)
# --------------------------------------------------------------------------

_DEFAULT_BACKEND = {
    "provider": "claude",
    "model": "claude-haiku-4-5",
    "timeout_seconds": 60,
    "max_retries": 3,
    "retry_backoff_base_seconds": 2.0,
    "ollama_base_url": "http://192.168.0.38:11434",
}

_DEFAULT_RUN = {
    "num_games": 10,
    "rounds_per_game": 6,
    "seed": None,
    "output_dir": "output",
}

_DEFAULT_SCENARIO = {"template": DEFAULT_SCENARIO_TEMPLATE}

_DEFAULT_CAST = {
    "traitor": "The Abbotts",
    "detective": "The Murphys",
    "loyalist_a": "The Chens",
    "loyalist_b": "The Patels",
    "loyalist_c": "The Okayes",
}


def _fresh(section: Dict[str, Any]) -> ConfigDict:
    """Fresh copy of a default section (mutable per AppConfig instance)."""
    return ConfigDict({key: value for key, value in section.items()})


def _merge_section(
    section_name: str,
    target: ConfigDict,
    provided: Any,
    warnings: List[str],
) -> None:
    """Overlay a provided JSON section onto the defaults; warn on unknown keys.

    Keys starting with ``_`` are treated as documentation/comment keys
    (used by config.example.json) and ignored silently.
    """
    if provided is None:
        return
    if not isinstance(provided, dict):
        raise ConfigError(f"config section {section_name!r} must be a JSON object")
    for key, value in provided.items():
        if isinstance(key, str) and key.startswith("_"):
            continue
        if key in target:
            target[key] = value
        else:
            warnings.append(f"unknown config key {section_name!r}.{key!r} ignored")


def load_config(path: Optional[str] = None) -> AppConfig:
    """Load and validate the application configuration.

    With ``path``: reads a JSON config file, validates it against the
    tech-design sec 8 schema, and fills defaults for every optional key.
    Without ``path``: returns the built-in default configuration
    (provider claude / claude-haiku-4-5, 10 games, 6 rounds, output_dir
    ``output``, baseline stolen-prize-tin scenario and cast).

    All top-level sections (backend/run/scenario/cast) are optional and
    defaulted; unknown keys are ignored with a warning collected in
    ``config.warnings``.

    Raises:
        ConfigError: file missing / not valid JSON / not an object; invalid
            ``backend.provider`` (must be deepseek|claude|ollama|mock); ``num_games``
            < 1; ``rounds_per_game`` < 2; empty ``output_dir``; empty
            ``scenario.template``; empty cast household names.
    """
    warnings: List[str] = []

    if path is None:
        return AppConfig(
            backend=_fresh(_DEFAULT_BACKEND),
            run=_fresh(_DEFAULT_RUN),
            scenario=_fresh(_DEFAULT_SCENARIO),
            cast=_fresh(_DEFAULT_CAST),
            warnings=warnings,
        )

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config file {path} is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"Config file {path} must contain a JSON object")

    backend = _fresh(_DEFAULT_BACKEND)
    run = _fresh(_DEFAULT_RUN)
    scenario = _fresh(_DEFAULT_SCENARIO)
    cast = _fresh(_DEFAULT_CAST)

    _merge_section("backend", backend, data.get("backend"), warnings)
    _merge_section("run", run, data.get("run"), warnings)
    _merge_section("scenario", scenario, data.get("scenario"), warnings)
    _merge_section("cast", cast, data.get("cast"), warnings)

    # Provider-aware model default (tech-design sec 8): claude default is
    # claude-haiku-4-5, ollama default is qwen3:8b -- only when the config
    # does not name a model itself.
    provided_backend = data.get("backend")
    if (
        backend.get("provider") == "ollama"
        and (not isinstance(provided_backend, dict) or "model" not in provided_backend)
    ):
        backend["model"] = "qwen3:8b"

    for key in data:
        if isinstance(key, str) and key.startswith("_"):
            continue
        if key not in ("backend", "run", "scenario", "cast"):
            warnings.append(f"unknown config key {key!r} ignored")

    # Validation -- every error names the offending field and the fix.
    provider = backend.get("provider")
    if provider not in VALID_PROVIDERS:
        raise ConfigError(
            f"invalid backend.provider {provider!r}; must be one of "
            f"{', '.join(VALID_PROVIDERS)}"
        )
    num_games = run.get("num_games")
    if not isinstance(num_games, int) or isinstance(num_games, bool) or num_games < 1:
        raise ConfigError(f"run.num_games must be an integer >= 1, got {num_games!r}")
    rounds_per_game = run.get("rounds_per_game")
    if (
        not isinstance(rounds_per_game, int)
        or isinstance(rounds_per_game, bool)
        or rounds_per_game < 2
    ):
        raise ConfigError(
            f"run.rounds_per_game must be an integer >= 2, got {rounds_per_game!r}"
        )
    output_dir = run.get("output_dir")
    if not isinstance(output_dir, str) or not output_dir.strip():
        raise ConfigError(f"run.output_dir must be a non-empty string, got {output_dir!r}")
    template = scenario.get("template")
    if not isinstance(template, str) or not template.strip():
        raise ConfigError(f"scenario.template must be a non-empty string, got {template!r}")
    for role in CAST_ROLES:
        household = cast.get(role)
        if not isinstance(household, str) or not household.strip():
            raise ConfigError(
                f"cast.{role} must be a non-empty household name, got {household!r}"
            )

    return AppConfig(
        backend=backend,
        run=run,
        scenario=scenario,
        cast=cast,
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# Backend creation (mock dry-run default script)
# --------------------------------------------------------------------------


def _default_mock_script(messages: List[Dict[str, str]]) -> str:
    """Deterministic default reply for MockBackend dry runs.

    Emits a valid statement during discussion rounds and a ``no accusation``
    final vote during the private vote phase, so a bare ``provider: mock``
    config produces complete, well-formed games with no network access
    (tech-design sec 8: mock is "available for dry runs").
    """
    system = messages[0].get("content", "") if messages else ""
    if "final_vote" in system:
        return json.dumps(
            {
                "action_type": "final_vote",
                "content": "no accusation",
                "target": "",
                "reason": "",
            }
        )
    return json.dumps(
        {
            "action_type": "statement",
            "content": "I recall the office door being ajar near the corridor around 8:40pm.",
            "target": "",
            "reason": "",
        }
    )


def _create_backend(backend_cfg: Any) -> LLMBackend:
    """Create the LLM backend for a run (delegates to llm_backend.create_backend).

    For ``provider: mock`` without an explicit ``scripted`` list, a
    deterministic default script is installed: a bare MockBackend has an
    empty script and raises ``BackendUnavailableError`` on every call by
    design (never-fabricate rule), which would make even a healthy dry run
    abort every game.
    """
    cfg: Dict[str, Any] = {key: value for key, value in dict(backend_cfg).items()}
    if cfg.get("provider") == "mock" and "scripted" not in cfg:
        cfg["scripted"] = _default_mock_script
    return create_backend(cfg)


# --------------------------------------------------------------------------
# Application assembly
# --------------------------------------------------------------------------


def build_game_components(
    config: AppConfig,
    seed: int,
    game_id: str,
    backend: Optional[LLMBackend] = None,
) -> Tuple[Scenario, Dict[str, PlayerAgent], GameConfig]:
    """Construct the real objects for one game.

    Wires scenario + players + game config for a single game: the scenario
    comes from ``default_scenario()`` (baseline template) or
    ``load_scenario(path)`` for any other template; one ``PlayerAgent`` per
    cast member is built with that member's own role card only; the shared
    ``backend`` is created once per run (via ``_create_backend`` when not
    passed in, so a batch reuses one backend); a ``GameConfig`` is derived
    from ``config.run``.

    Returns ``(scenario, players, game_config)`` -- the triple the Metrics
    ``game_factory`` expects.

    Raises:
        ScenarioError: the scenario template cannot be resolved/loaded.
        ConfigError / BackendConfigError: backend creation fails (e.g.
            provider bogus, or claude without ANTHROPIC_API_KEY).
    """
    if backend is None:
        backend = _create_backend(config.backend)

    template = config.scenario.template
    if template == DEFAULT_SCENARIO_TEMPLATE:
        defn = default_scenario()
    else:
        try:
            defn = load_scenario(template)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise ScenarioError(
                f"scenario template {template!r} could not be loaded: {exc}"
            ) from exc
    scenario = build_scenario(defn, seed)

    players: Dict[str, PlayerAgent] = {}
    for identity in scenario.players:
        players[identity.player_id] = PlayerAgent(
            identity=identity,
            role_card=identity.role_card,
            scenario=scenario,
            backend=backend,
            model_config={},
        )

    game_config = GameConfig(
        rounds_per_game=int(config.run.rounds_per_game),
        seed=int(seed),
        output_dir=Path(config.run.output_dir),
    )
    return scenario, players, game_config


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="traitors_sim",
        description="Mode B (The Parlour) AI agent simulation -- traitors-mobile",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_single = sub.add_parser("run-single", help="run one game")
    p_single.add_argument("--config", default=None, help="path to a JSON config file")
    p_single.add_argument(
        "--seed", type=int, default=None, help="RNG seed (default: random)"
    )

    p_batch = sub.add_parser(
        "run-batch", help="run a batch of games and write the metrics report"
    )
    p_batch.add_argument("--config", default=None, help="path to a JSON config file")
    return parser


def _log_probe(backend: LLMBackend, provider: str) -> None:
    """Probe the configured backend and log availability (never blocks long)."""
    probe = backend.probe()
    if provider == "ollama":
        if probe.available:
            print(
                f"Ollama reachable: {len(probe.models)} model(s) available",
                file=sys.stderr,
            )
        else:
            print(
                f"Ollama unreachable: {probe.error or 'no error detail'}",
                file=sys.stderr,
            )
    else:
        print(
            f"Backend probe ({provider}): available={probe.available}",
            file=sys.stderr,
        )


def _run(args: argparse.Namespace) -> int:
    """Execute the parsed subcommand; raises are converted to exit codes by main."""
    config = load_config(args.config)
    for warning in config.warnings:
        print(f"warning: {warning}", file=sys.stderr)

    output_dir = Path(config.run.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    backend = _create_backend(config.backend)
    _log_probe(backend, config.backend.provider)

    if args.command == "run-single":
        seed = args.seed if args.seed is not None else random.randrange(0, 2**31 - 1)
        game_id = f"single_{seed}"
        scenario, players, game_config = build_game_components(
            config, seed, game_id, backend
        )
        result = run_game(scenario, players, game_config, game_id=game_id)
        print(
            f"Game {game_id}: traitor={result.traitor_id} "
            f"traitor_caught={result.traitor_caught} "
            f"most_accused={result.most_accused} "
            f"exchanges={result.exchange_count} status={result.status}"
        )
        print(f"Transcript: {output_dir / f'game_{game_id}.transcript.json'}")
        print(f"Result: {output_dir / f'game_{game_id}.result.json'}")
        return 0

    # run-batch
    batch_config: Dict[str, Any] = {
        "num_games": config.run.num_games,
        "seed": config.run.seed,
    }
    results = run_batch(
        batch_config,
        lambda seed, game_id: build_game_components(config, seed, game_id, backend),
    )
    report = compute_metrics(results)
    json_path, md_path = write_report(report, output_dir)
    print(
        f"Batch complete: {report.games_completed} game(s) completed, "
        f"{len(report.games_aborted)} aborted, catch_rate={report.catch_rate}"
    )
    print(f"Report: {json_path}")
    print(f"Summary: {md_path}")
    if report.games_completed == 0 and report.games_aborted:
        print(
            "Backend unavailable: every game aborted "
            "(backend unreachable or retry budget exhausted)",
            file=sys.stderr,
        )
        return 2
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Real CLI entry point. Never raises -- errors become exit codes.

    Exit codes: 0 success; 1 config error; 2 backend unavailable; 3 game
    aborted (run-single); 4 unexpected error.
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits (e.g. missing/invalid subcommand, --help); convert
        # to a return code so main() always returns an int.
        return int(exc.code or 0)

    try:
        return _run(args)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    except BackendConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    except BackendUnavailableError as exc:
        print(f"Backend unavailable: {exc}", file=sys.stderr)
        return 2
    except GameAbortedError as exc:
        print(f"Game aborted: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 - unexpected errors become exit 4
        print(f"Unexpected error: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 4


if __name__ == "__main__":
    sys.exit(main())
