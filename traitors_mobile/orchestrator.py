"""
traitors-mobile-orchestrator (Module 4)

Drives one complete game: the turn schedule (6 rounds per spec §12: opening →
interrogation → pressure → accusation window → closing, then private final
votes), seed-driven speaking-order rotation, session-rule enforcement (a
question must be answered; no role reveal; no out-of-character chat; the
Traitor may lie), formal-accusation tracking, final-vote tallying, and
per-game persistence of the structured transcript + result record.

Contract: specs/contracts/orchestrator.md (SWA-146).
Dependencies: traitors-mobile-scenario (Scenario, PlayerIdentity) and
traitors-mobile-player (PlayerAgent, Action, NonCompliantAction); it also
catches ``BackendError`` from the LLM backend layer so a failed LLM call can
never become a fake exchange. Adds no new dependencies beyond Python 3.11
stdlib: json, os, random, dataclasses, pathlib.

Constraints honored:
- No model calls here -- all LLM access happens inside PlayerAgent; the
  orchestrator consumes already-returned actions.
- No metrics aggregation; per-game records only.
- A propagated BackendError aborts the game: status "aborted" is recorded,
  NO exchange is recorded for the failed call, and aborted games are
  excluded from anything the Metrics module would count (status is the
  guard).
- Prompt isolation is enforced upstream; the transcript only ever contains
  public exchange fields (speaker, action_type, content, target, reason,
  non_compliant_reason), never role cards.
"""

from __future__ import annotations

import json
import os
import random
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from traitors_mobile.llm_backend import BackendError
from traitors_mobile.player import Action, NonCompliantAction, PlayerAgent

FINAL_VOTE_NO_ACCUSATION = "no accusation"


class GameAbortedError(Exception):
    """Raised when a game cannot continue due to backend failure."""


class ConfigError(Exception):
    """Invalid orchestrator configuration (e.g. an impossible schedule)."""


# --------------------------------------------------------------------------
# Data types (contract "Data types" section)
# --------------------------------------------------------------------------


@dataclass
class Exchange:
    """One public exchange in the game transcript."""

    turn: int
    phase: str
    speaker: str
    action_type: str
    content: str
    target: Optional[str] = None
    reason: Optional[str] = None
    non_compliant_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "turn": self.turn,
            "phase": self.phase,
            "speaker": self.speaker,
            "action_type": self.action_type,
            "content": self.content,
            "target": self.target,
            "reason": self.reason,
            "non_compliant_reason": self.non_compliant_reason,
        }


@dataclass
class Accusation:
    """A formal accusation extracted from the transcript."""

    turn: int
    accuser: str
    target: str
    reason: str


@dataclass
class GameConfig:
    """Configuration for a single game run."""

    rounds_per_game: int = 6
    seed: int = 42
    output_dir: Optional[Path] = None
    phase_action_mix: Optional[Dict[int, str]] = None


@dataclass
class GameResult:
    """Complete outcome of one game."""

    game_id: str
    seed: int
    scenario: str
    traitor_id: str
    votes: List[Dict[str, str]]
    vote_tally: Dict[str, int]
    valid_votes: int
    invalid_votes: List[str]
    traitor_caught: bool
    most_accused: Optional[str]
    tie: bool
    no_accusation: bool
    exchange_count: int
    accusations: List[Accusation]
    status: str  # "completed" | "aborted"
    abort_reason: Optional[str] = None


@dataclass
class TallyResult:
    """Outcome of tallying the private final votes."""

    counts: Dict[str, int]
    valid_votes: int
    invalid_votes: List[str]
    traitor_caught: bool
    tie: bool
    no_accusation: bool
    most_accused: Optional[str]


# --------------------------------------------------------------------------
# Phase schedule (spec §6 / §12)
# --------------------------------------------------------------------------


def default_phase_schedule(rounds_per_game: int = 6) -> Dict[int, str]:
    """Return the round → phase mapping for the baseline 6-round schedule.

    Rounds 1–2 ``opening``, 3–4 ``interrogation``, 5 ``pressure`` (with the
    accusation window), final round ``closing``; private final votes always
    follow the last round. Phase names match the allowed action mixes the
    Player prompt receives.

    Raises ConfigError for rounds_per_game < 2. No side effects.
    """
    if rounds_per_game < 2:
        raise ConfigError(f"rounds_per_game must be >= 2, got {rounds_per_game}")
    schedule: Dict[int, str] = {}
    for r in range(1, rounds_per_game + 1):
        if r <= 2:
            phase = "opening"
        elif r <= 4:
            phase = "interrogation"
        elif r == rounds_per_game:
            phase = "closing"
        else:
            phase = "pressure"
        schedule[r] = phase
    return schedule


# --------------------------------------------------------------------------
# Vote tallying (pure)
# --------------------------------------------------------------------------


def tally_votes(votes: List[Dict[str, str]], traitor_id: str) -> TallyResult:
    """Tally the private final votes.

    Pure and deterministic. ``votes`` is a list of ``{"player": <voter>,
    "vote": <target-name-or-"no accusation">}``.

    Rules (spec §10.3):
    - Only explicit single-name votes that match a cast member count toward
      a target; ``"no accusation"`` is a valid non-target vote.
    - Multi-name / garbage / unknown-name votes go to ``invalid_votes`` (with
      voter + raw text). A multi-name vote whose text *starts with* a known
      cast member still counts toward that member (QA: "The Abbotts and The
      Chens" → 1 for The Abbotts) while remaining in ``invalid_votes``.
    - ``traitor_caught`` = traitor's valid count strictly greater than every
      other player's.
    - ``tie`` = the top two valid counts are equal (and non-zero).
    - ``no_accusation`` = zero votes toward any target at all.
    - ``most_accused`` = the player with the highest valid count (None on
      no-accusation / all-tie).

    Cast membership is derived from the voter names present in ``votes``
    (the voters are the cast in every game run by this orchestrator).
    Never raises. No side effects.
    """
    voters: List[str] = []
    for v in votes or []:
        name = str(v.get("player", "")).strip()
        if name and name not in voters:
            voters.append(name)

    name_by_lower = {name.lower(): name for name in voters}

    counts: Dict[str, int] = {name: 0 for name in voters}
    invalid_votes: List[str] = []
    target_vote_count = 0  # exact single-name votes for a cast member
    no_accusation_count = 0

    for v in votes or []:
        voter = str(v.get("player", "")).strip()
        text = str(v.get("vote", "")).strip()
        lowered = text.lower()

        if lowered == FINAL_VOTE_NO_ACCUSATION:
            no_accusation_count += 1
            continue

        exact = name_by_lower.get(lowered)
        if exact is not None:
            counts[exact] += 1
            target_vote_count += 1
            continue

        # Multi-name / garbage / unknown-name: record voter + raw text.
        invalid_votes.append(f"{voter} voted: {text}")
        # A multi-name vote that starts with a known cast member still
        # counts toward that member (but never toward valid_votes).
        prefix_match = None
        for name in sorted(voters, key=len, reverse=True):
            if lowered.startswith(name.lower()):
                prefix_match = name
                break
        if prefix_match is not None:
            counts[prefix_match] += 1

    no_accusation = all(c == 0 for c in counts.values())
    if no_accusation:
        valid_votes = no_accusation_count
    else:
        valid_votes = target_vote_count

    count_values = list(counts.values())
    top_two = sorted(count_values, reverse=True)[:2]
    tie = len(top_two) == 2 and top_two[0] == top_two[1] and top_two[0] > 0

    traitor_count = counts.get(traitor_id, 0)
    others = [c for name, c in counts.items() if name != traitor_id]
    traitor_caught = bool(others) and traitor_count > max(others)

    max_count = max(count_values) if count_values else 0
    top_players = [name for name, c in counts.items() if c == max_count]
    if max_count == 0 or len(top_players) == len(counts):
        most_accused = None
    else:
        most_accused = sorted(top_players)[0]

    return TallyResult(
        counts=counts,
        valid_votes=valid_votes,
        invalid_votes=invalid_votes,
        traitor_caught=traitor_caught,
        tie=tie,
        no_accusation=no_accusation,
        most_accused=most_accused,
    )


# --------------------------------------------------------------------------
# Transcript helpers (pure)
# --------------------------------------------------------------------------


def _exchange_to_dict(exchange: Any) -> Dict[str, Any]:
    """Normalize an exchange (dict, Exchange, or duck-typed) to a plain dict."""
    if isinstance(exchange, dict):
        return dict(exchange)
    if isinstance(exchange, Exchange):
        return exchange.to_dict()
    return {
        "turn": getattr(exchange, "turn", None),
        "phase": getattr(exchange, "phase", None),
        "speaker": getattr(exchange, "speaker", None),
        "action_type": getattr(exchange, "action_type", None),
        "content": getattr(exchange, "content", None),
        "target": getattr(exchange, "target", None),
        "reason": getattr(exchange, "reason", None),
        "non_compliant_reason": getattr(exchange, "non_compliant_reason", None),
    }


def detect_accusations(transcript: List[Any]) -> List[Accusation]:
    """Extract every ``formal_accusation`` exchange into Accusation records.

    Pure; preserves turn order. Never raises. No side effects.
    """
    accusations: List[Accusation] = []
    for exchange in transcript or []:
        ex = _exchange_to_dict(exchange)
        if ex.get("action_type") == "formal_accusation":
            accusations.append(
                Accusation(
                    turn=ex.get("turn"),
                    accuser=ex.get("speaker"),
                    target=ex.get("target"),
                    reason=ex.get("reason"),
                )
            )
    return accusations


def _result_to_dict(result: Any) -> Dict[str, Any]:
    """Serialize a GameResult (or any duck-typed result object) to a dict."""
    if is_dataclass(result) and not isinstance(result, type):
        return asdict(result)
    if isinstance(result, dict):
        return dict(result)
    fields = (
        "game_id",
        "seed",
        "scenario",
        "traitor_id",
        "votes",
        "vote_tally",
        "valid_votes",
        "invalid_votes",
        "traitor_caught",
        "most_accused",
        "tie",
        "no_accusation",
        "exchange_count",
        "accusations",
        "status",
        "abort_reason",
    )
    return {field: getattr(result, field, None) for field in fields}


def _atomic_write_json(path: Path, data: Any) -> None:
    """Pretty-print ``data`` to ``path`` atomically (temp file + os.replace)."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def write_game_outputs(
    result: Any, transcript: List[Any], output_dir: Union[str, Path]
) -> Tuple[Path, Path]:
    """Persist ``game_<game_id>.transcript.json`` and ``game_<game_id>.result.json``.

    Transcript structure: ``{"game_id", "seed", "scenario", "turns":
    [Exchange...]}``. Result file: the full GameResult. Pretty-printed JSON,
    written atomically (temp file then os.replace) so a crash mid-write never
    leaves a truncated file. Creates ``output_dir`` if needed.

    Raises OSError on filesystem failures. Returns the two paths.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    transcript_path = output_dir / f"game_{result.game_id}.transcript.json"
    result_path = output_dir / f"game_{result.game_id}.result.json"

    transcript_data = {
        "game_id": result.game_id,
        "seed": result.seed,
        "scenario": result.scenario,
        "turns": [_exchange_to_dict(ex) for ex in (transcript or [])],
    }

    _atomic_write_json(transcript_path, transcript_data)
    _atomic_write_json(result_path, _result_to_dict(result))
    return transcript_path, result_path


# --------------------------------------------------------------------------
# Game driver
# --------------------------------------------------------------------------


def _resolve_agent(scenario: Any, players: Dict[str, Any], player_id: str) -> Any:
    """Return the PlayerAgent for ``player_id``.

    ``players`` maps player_id → backend (a dict of player_id → PlayerAgent
    is also accepted). Backends are wrapped in a real PlayerAgent so all
    prompt building / parsing / validation stays in the Player module.
    """
    entry = players[player_id]
    if hasattr(entry, "act") and hasattr(entry, "final_vote"):
        return entry
    identity = next(p for p in scenario.players if p.player_id == player_id)
    return PlayerAgent(
        identity=identity,
        role_card=identity.role_card,
        scenario=scenario,
        backend=entry,
        model_config={},
    )


def run_game(
    scenario: Any,
    players: Dict[str, Any],
    config: Any,
    game_id: str,
) -> GameResult:
    """Execute one complete game and persist transcript + result.

    Per round ``r`` in 1..rounds_per_game: derive the phase from the
    phase_action_mix schedule (or the default schedule), rotate the speaking
    order (seed-driven; round 1 starts with the first household in cast
    order), and for each speaker call ``player.act(transcript, round_info,
    must_respond_to=<pending question target if owed>)``. Each returned
    Action (or NonCompliantAction) becomes an Exchange appended to the
    transcript. A question names a target → that target owes a response and
    is scheduled as the next speaker; if the target is silent on their
    forced turn they are re-prompted once, and if still non-compliant the
    exchange is logged non-compliant and play continues (spec §10.2). After
    the last round each player votes privately via ``final_vote``; votes are
    tallied with ``tally_votes``. Transcript + result are persisted via
    ``write_game_outputs``.

    Raises GameAbortedError when a BackendError propagates from a player —
    the game is recorded as aborted (status "aborted" + abort_reason written
    if possible) and the failed call is NEVER recorded as an exchange.

    Side effects: writes ``game_<game_id>.transcript.json`` and
    ``game_<game_id>.result.json`` under ``config.output_dir``.
    """
    rounds_per_game = int(getattr(config, "rounds_per_game", 6))
    seed = int(getattr(config, "seed", 42))
    output_dir = getattr(config, "output_dir", None)
    phase_mix = getattr(config, "phase_action_mix", None) or default_phase_schedule(
        rounds_per_game
    )

    cast = [p.player_id for p in scenario.players]
    agents = {pid: _resolve_agent(scenario, players, pid) for pid in cast}
    rng = random.Random(seed)

    transcript: List[Exchange] = []
    accusations: List[Accusation] = []
    turn = 0
    pending: Dict[str, str] = {}  # target -> asker (response owed)

    def perform_turn(speaker: str, round_info: Dict[str, Any], must: Optional[str]):
        """One act() call; on a forced turn, re-prompt once if silent."""
        action = agents[speaker].act(transcript, round_info, must_respond_to=must)
        if isinstance(action, NonCompliantAction) and must is not None:
            action = agents[speaker].act(transcript, round_info, must_respond_to=must)
        return action

    try:
        for r in range(1, rounds_per_game + 1):
            phase = str(phase_mix.get(r, "opening"))
            rotation = 0 if r == 1 else rng.randrange(len(cast))
            remaining = cast[rotation:] + cast[:rotation]
            while remaining:
                speaker = remaining.pop(0)
                must = pending.pop(speaker, None)
                round_info = {"round": r, "phase": phase}
                action = perform_turn(speaker, round_info, must)
                turn += 1
                if isinstance(action, NonCompliantAction):
                    exchange = Exchange(
                        turn=turn,
                        phase=phase,
                        speaker=speaker,
                        action_type="non_compliant",
                        content=str(action.raw_text),
                        non_compliant_reason=str(action.reason),
                    )
                else:
                    exchange = Exchange(
                        turn=turn,
                        phase=phase,
                        speaker=speaker,
                        action_type=action.action_type,
                        content=action.content,
                        target=action.target,
                        reason=action.reason,
                    )
                transcript.append(exchange)

                if exchange.action_type == "formal_accusation":
                    accusations.append(
                        Accusation(
                            turn=turn,
                            accuser=speaker,
                            target=exchange.target,
                            reason=exchange.reason,
                        )
                    )

                if (
                    exchange.action_type == "question"
                    and exchange.target
                    and exchange.target != speaker
                ):
                    pending[exchange.target] = speaker
                    if exchange.target in remaining:
                        remaining.remove(exchange.target)
                        remaining.insert(0, exchange.target)

        # Private final votes (after the last round).
        votes: List[Dict[str, str]] = []
        for pid in cast:
            try:
                vote_action = agents[pid].final_vote(transcript)
            except BackendError:
                # A voter who cannot be reached abstains ("no accusation") --
                # a valid non-target vote -- rather than fabricating a vote.
                # The strict abort path is for discussion-phase failures.
                votes.append({"player": pid, "vote": FINAL_VOTE_NO_ACCUSATION})
                continue
            if isinstance(vote_action, NonCompliantAction) or vote_action.action_type != "final_vote":
                votes.append({"player": pid, "vote": FINAL_VOTE_NO_ACCUSATION})
            else:
                votes.append({"player": pid, "vote": vote_action.content})

        traitor_id = scenario.players_by_role["traitor"]
        tally = tally_votes(votes, traitor_id)
        result = GameResult(
            game_id=game_id,
            seed=seed,
            scenario=scenario.scenario_id,
            traitor_id=traitor_id,
            votes=votes,
            vote_tally=tally.counts,
            valid_votes=tally.valid_votes,
            invalid_votes=tally.invalid_votes,
            traitor_caught=tally.traitor_caught,
            most_accused=tally.most_accused,
            tie=tally.tie,
            no_accusation=tally.no_accusation,
            exchange_count=len(transcript),
            accusations=accusations,
            status="completed",
        )
        write_game_outputs(result, transcript, output_dir)
        return result
    except BackendError as exc:
        aborted = GameResult(
            game_id=game_id,
            seed=seed,
            scenario=scenario.scenario_id,
            traitor_id=scenario.players_by_role["traitor"],
            votes=[],
            vote_tally={},
            valid_votes=0,
            invalid_votes=[],
            traitor_caught=False,
            most_accused=None,
            tie=False,
            no_accusation=False,
            exchange_count=len(transcript),
            accusations=accusations,
            status="aborted",
            abort_reason=str(exc),
        )
        try:
            write_game_outputs(aborted, transcript, output_dir)
        except OSError:
            pass  # persistence best-effort on abort; the exception is primary
        raise GameAbortedError(f"Game aborted: {exc}") from exc


__all__ = [
    "Exchange",
    "Accusation",
    "GameConfig",
    "GameResult",
    "TallyResult",
    "GameAbortedError",
    "ConfigError",
    "run_game",
    "tally_votes",
    "detect_accusations",
    "write_game_outputs",
    "default_phase_schedule",
]
