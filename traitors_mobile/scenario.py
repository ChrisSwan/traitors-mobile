"""
traitors-mobile-scenario (Module 1)

Defines, loads, validates and initialises the concrete game scenario for the
Mode B AI agent simulation: the public scenario text, the fixed 5-player cast
(1 Traitor, 1 Detective, 3 Loyalists), per-player role cards (goal, private
observations, Traitor's sealed crime declaration + cover story, Detective's
directional hint), and the crime window.

Contract: specs/contracts/scenario.md (SWA-146).

Pure data + stdlib only: json, dataclasses, random (seedable assignment),
pathlib. No LLM calls, no network, no writes to disk.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# The exactly-required baseline cast, per contract (spec §3).
REQUIRED_ROLES = ("traitor", "detective", "loyalist_a", "loyalist_b", "loyalist_c")

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


class ScenarioError(Exception):
    """Raised when a scenario definition cannot be built (e.g. unknown placeholder)."""


class ScenarioValidationError(Exception):
    """Raised when scenario data is structurally invalid (missing/wrong-type keys)."""


@dataclass
class RoleCard:
    """A single player's private role material."""

    goal: str
    observations: List[str]
    crime_declaration: Optional[str] = None
    cover_story: Optional[str] = None
    detective_hint: Optional[str] = None


@dataclass
class PlayerIdentity:
    """A cast member bound to a role and their private role card."""

    player_id: str  # household name
    role: str
    household: str
    role_card: RoleCard = field(default_factory=lambda: RoleCard(goal="", observations=[]))


@dataclass
class Scenario:
    """An initialised game scenario: cast assignment + substituted role cards."""

    scenario_id: str
    description: str
    crime_window: str
    players: List[PlayerIdentity]
    players_by_role: Dict[str, str]  # role -> player_id (household name)


@dataclass
class ScenarioDefinition:
    """A scenario template as loaded from JSON (pre-substitution)."""

    id: str
    name: str
    description: str
    crime_window: str
    cast: Dict[str, str]  # role -> household name
    role_cards: Dict[str, Dict[str, Any]]  # role -> card data


def load_scenario(path: str | Path) -> ScenarioDefinition:
    """Read a JSON scenario file and validate it as a ScenarioDefinition.

    Required keys: id, name, description, crime_window, cast (role->household),
    role_cards (per-role cards). Card observations may contain {role} placeholders.

    Raises:
        FileNotFoundError: path does not exist.
        json.JSONDecodeError: file is not valid JSON.
        ScenarioValidationError: required keys missing or of the wrong type
            (message lists all problems).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Scenario file not found: {path}")
    with path.open(encoding="utf-8") as f:
        data = json.load(f)

    problems: List[str] = []
    required_keys = ("id", "name", "description", "crime_window", "cast", "role_cards")
    for key in required_keys:
        if key not in data:
            problems.append(f"missing required key: {key!r}")

    if problems:
        raise ScenarioValidationError(
            f"Invalid scenario definition: {'; '.join(problems)}"
        )

    # Type checks (collected so the message lists every problem at once).
    for key in ("id", "name", "description", "crime_window"):
        if not isinstance(data[key], str):
            problems.append(f"key {key!r} must be a string, got {type(data[key]).__name__}")

    cast = data["cast"]
    if not isinstance(cast, dict):
        problems.append(f"key 'cast' must be an object mapping role->household, got {type(cast).__name__}")
    else:
        for role, household in cast.items():
            if not isinstance(household, str) or not household.strip():
                problems.append(f"cast entry {role!r} must map to a non-empty household name")

    role_cards = data["role_cards"]
    if not isinstance(role_cards, dict):
        problems.append(f"key 'role_cards' must be an object mapping role->card, got {type(role_cards).__name__}")
    else:
        for role, card in role_cards.items():
            if not isinstance(card, dict):
                problems.append(f"role card for {role!r} must be an object with 'goal' and 'observations'")
                continue
            goal = card.get("goal")
            if not isinstance(goal, str) or not goal.strip():
                problems.append(f"role card for {role!r}: 'goal' must be a non-empty string")
            observations = card.get("observations")
            if not isinstance(observations, list) or any(
                not isinstance(obs, str) for obs in observations
            ):
                problems.append(f"role card for {role!r}: 'observations' must be a list of strings")
            if role == "traitor":
                for extra in ("crime_declaration", "cover_story"):
                    value = card.get(extra)
                    if not isinstance(value, str) or not value.strip():
                        problems.append(f"traitor role card: {extra!r} must be a non-empty string")
            if role == "detective":
                value = card.get("detective_hint")
                if not isinstance(value, str) or not value.strip():
                    problems.append(f"detective role card: 'detective_hint' must be a non-empty string")

    if problems:
        raise ScenarioValidationError(f"Invalid scenario definition: {'; '.join(problems)}")

    return ScenarioDefinition(
        id=data["id"],
        name=data["name"],
        description=data["description"],
        crime_window=data["crime_window"],
        cast=cast,
        role_cards=role_cards,
    )


def build_scenario(defn: ScenarioDefinition, seed: int | None = None) -> Scenario:
    """Initialise a Scenario from a definition.

    Validates the definition, assigns each role to its configured household,
    and substitutes every {role} placeholder in every card field with the
    assigned household name (case preserved). Deterministic given the same
    definition and seed (seed is reserved for future cast-shuffling).

    Raises:
        ScenarioError: a card references an unknown {placeholder}.
        ScenarioValidationError: the cast does not contain exactly the 5
            baseline roles.
    """
    # Seed a private RNG so determinism is available without mutating the
    # global random state (today assignment is fixed; seed is reserved).
    _rng = random.Random(seed)

    cast_roles = set(defn.cast.keys())
    required_set = set(REQUIRED_ROLES)
    if cast_roles != required_set:
        missing = sorted(required_set - cast_roles)
        extra = sorted(cast_roles - required_set)
        parts = []
        if missing:
            parts.append(f"missing roles: {', '.join(missing)}")
        if extra:
            parts.append(f"extra roles: {', '.join(extra)}")
        raise ScenarioValidationError(
            f"Cast must contain exactly the 5 baseline roles (traitor, detective, "
            f"loyalist_a, loyalist_b, loyalist_c); {'; '.join(parts)}"
        )

    def substitute(text: str) -> str:
        def replacer(match: "re.Match[str]") -> str:
            placeholder = match.group(1)
            if placeholder not in defn.cast:
                raise ScenarioError(
                    f"Unknown placeholder {{{placeholder}}} in scenario "
                    f"{defn.id!r}: not a role in the cast"
                )
            return defn.cast[placeholder]

        return _PLACEHOLDER_RE.sub(replacer, text)

    players: List[PlayerIdentity] = []
    players_by_role: Dict[str, str] = {}
    for role in REQUIRED_ROLES:
        household = defn.cast[role]
        card_data = defn.role_cards.get(role, {})
        role_card = RoleCard(
            goal=substitute(str(card_data.get("goal", ""))),
            observations=[substitute(str(obs)) for obs in card_data.get("observations", [])],
        )
        if role == "traitor":
            role_card.crime_declaration = substitute(str(card_data.get("crime_declaration", ""))) or None
            role_card.cover_story = substitute(str(card_data.get("cover_story", ""))) or None
        if role == "detective":
            role_card.detective_hint = substitute(str(card_data.get("detective_hint", ""))) or None
        player = PlayerIdentity(
            player_id=household,
            role=role,
            household=household,
            role_card=role_card,
        )
        players.append(player)
        players_by_role[role] = household

    return Scenario(
        scenario_id=defn.id,
        description=defn.description,
        crime_window=defn.crime_window,
        players=players,
        players_by_role=players_by_role,
    )


def validate_scenario(scenario: Scenario) -> list[str]:
    """Return human-readable problems; an empty list means the scenario is valid.

    Checks: all 5 roles present; every card has a non-empty goal and >=1
    observation; traitor card has crime_declaration and cover_story; detective
    card has detective_hint; no {placeholder} token remains un-substituted in
    any card; no player_id appears twice. Never raises.
    """
    problems: List[str] = []

    roles_present = {p.role for p in scenario.players}
    missing_roles = set(REQUIRED_ROLES) - roles_present
    if missing_roles:
        problems.append(f"Missing roles: {', '.join(sorted(missing_roles))}")
    extra_roles = roles_present - set(REQUIRED_ROLES)
    if extra_roles:
        problems.append(f"Unknown roles present: {', '.join(sorted(extra_roles))}")

    player_ids = [p.player_id for p in scenario.players]
    if len(player_ids) != len(set(player_ids)):
        duplicates = sorted({pid for pid in player_ids if player_ids.count(pid) > 1})
        problems.append(f"Duplicate player_id: {', '.join(duplicates)}")

    for player in scenario.players:
        card = player.role_card
        if not card.goal or not card.goal.strip():
            problems.append(f"{player.role}: goal is empty")
        if not card.observations:
            problems.append(f"{player.role}: no observations")

        for field_name, value in (
            ("goal", card.goal),
            ("crime_declaration", card.crime_declaration),
            ("cover_story", card.cover_story),
            ("detective_hint", card.detective_hint),
        ):
            if value and ("{" in value or "}" in value):
                problems.append(
                    f"{player.role}: unsubstituted placeholder in {field_name}: {value}"
                )
        for i, obs in enumerate(card.observations):
            if "{" in obs or "}" in obs:
                problems.append(
                    f"{player.role}: unsubstituted placeholder in observation {i}: {obs}"
                )

        if player.role == "traitor":
            if not card.crime_declaration or not card.crime_declaration.strip():
                problems.append("traitor: missing crime_declaration")
            if not card.cover_story or not card.cover_story.strip():
                problems.append("traitor: missing cover_story")
        if player.role == "detective":
            if not card.detective_hint or not card.detective_hint.strip():
                problems.append("detective: missing detective_hint")

    return problems


def default_scenario() -> ScenarioDefinition:
    """Return the built-in 'stolen prize tin' baseline ScenarioDefinition."""
    return ScenarioDefinition(
        id="stolen-prize-tin",
        name="The Stolen Prize Tin",
        description=(
            "Meadowbrook Community Summer Fair, Saturday evening. The prize fund — £340 in a "
            "locked tin — was confirmed present and locked at 7:30pm by two committee members. "
            "At 9:15pm, event organiser Janet Marsh discovered it missing from the community "
            "centre office. The office was left unlocked during the event, as normal when the "
            "building is staffed. Everyone in the group was present during the relevant window "
            "(7:30pm–9:15pm). At least one person in the group stole the tin; it may have been "
            "done alone."
        ),
        crime_window="8:40pm–8:55pm",
        cast={
            "traitor": "The Abbotts",
            "detective": "The Murphys",
            "loyalist_a": "The Chens",
            "loyalist_b": "The Patels",
            "loyalist_c": "The Okayes",
        },
        role_cards={
            "traitor": {
                "goal": (
                    "Avoid being formally accused; redirect suspicion; may lie; may share or "
                    "withhold cards selectively (never reveal the crime declaration)."
                ),
                "observations": [
                    "Signed the visitor book on arrival at 7:10pm — earlier than most people.",
                    "Noticed the office door was unlocked at ~8:40pm while walking past on the way to the toilets.",
                    "When re-entering the bar at 8:57pm, noticed The Patels (Loyalist B) looking in their direction.",
                ],
                "crime_declaration": (
                    "Slipped away from the bar at 8:45pm, told no one, went to the office, found it "
                    "unlocked, took the tin, hid it in their bag, back at the bar by 8:57pm "
                    "(just before the 9:00pm toast)."
                ),
                "cover_story": (
                    "At the bar the entire evening from 8:20pm; bought a round of drinks at ~8:30pm; "
                    "never left the bar area."
                ),
            },
            "detective": {
                "goal": (
                    "Help identify the thief; share observations strategically; corroborate others; "
                    "watch for inconsistencies between claims and known facts."
                ),
                "observations": [
                    "At the buffet table 8:30pm–9:05pm with a clear sightline to the corridor leading to the office.",
                    "Between 8:40pm and 9:00pm, two separate people left the main hall toward the corridor; couldn't clearly identify the second; the first was The Chens (Loyalist A).",
                    "Spoke to the caretaker at 9:10pm; he mentioned someone had asked him about the heating earlier — odd, because the heating was fine all night.",
                ],
                "detective_hint": "Believes the theft occurred between 8:40pm and 8:55pm.",
            },
            "loyalist_a": {
                "goal": "Help the group; share observations honestly; be sceptical of claims that don't add up.",
                "observations": [
                    "Talking to the event organiser near the stage 8:15pm–8:50pm.",
                    "At ~8:40pm noticed the office corridor door ajar; assumed the caretaker.",
                    "Walked through the corridor at 8:50pm — office door closed by then.",
                    "Passed someone in the corridor at ~8:55pm heading toward the main hall; no clear look.",
                ],
            },
            "loyalist_b": {
                "goal": "Help the group; pay attention to what doesn't add up.",
                "observations": [
                    "At the bar from 8:20pm until the 9:00pm toast.",
                    "Fairly confident {traitor} were at the bar earlier, but absent from ~8:45pm until just before the toast.",
                    "Saw the caretaker near the office corridor at ~8:35pm, then not again until after the toast.",
                    "When {traitor} returned just before the toast, they seemed slightly flustered (could be misremembering).",
                ],
            },
            "loyalist_c": {
                "goal": "Help the group; ask good questions and apply pressure; has less information than others.",
                "observations": [
                    "Went to the cloakroom (far end of the office corridor) at 8:58pm.",
                    "Passing the office at 8:58pm, door closed, nothing unusual.",
                    "Came back through the corridor at 9:03pm — office door now open, light on; assumed someone working late.",
                    "No strong observations about who was where.",
                ],
            },
        },
    )
