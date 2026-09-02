"""
traitors-mobile-player (Module 2)

LLM-backed player agent module. Builds the per-player prompt (own role card +
public scenario + shared transcript + session rules + allowed action types),
calls the LLM backend, and parses/validates the model's output into one of the
six structured actions (statement / question / challenge / corroboration /
formal accusation / final vote). Re-prompts once on format violations; never
crashes on malformed model output; propagates BackendError from the backend so
a failed LLM call can never become a fake exchange.

Contract: specs/contracts/player.md (SWA-146).
Dependencies: traitors-mobile-llm-backend (LLMBackend.complete),
traitors-mobile-scenario (RoleCard, PlayerIdentity, Scenario). No network
calls here -- all model access goes through ``llm_backend``. Adds no new
dependencies beyond Python 3.11 stdlib: dataclasses, re, json.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

# All six structured action types (spec §6).
ALL_ACTION_TYPES = [
    "statement",
    "question",
    "challenge",
    "corroboration",
    "formal_accusation",
    "final_vote",
]

# Action types available during discussion rounds (final_vote is only produced
# through ``PlayerAgent.final_vote``).
DISCUSSION_ACTION_TYPES = [
    "statement",
    "question",
    "challenge",
    "corroboration",
    "formal_accusation",
]

FINAL_VOTE_NO_ACCUSATION = "no accusation"

# Phrases that reveal the speaker's role (rule §4); matched case-insensitively.
ROLE_REVEAL_PHRASES = [
    "i am the traitor",
    "as a traitor",
    "my role is",
    "i am the detective",
    "i am a detective",
    "i am the loyalist",
    "i am a loyalist",
]

# Out-of-character chatter (rule §3); matched case-insensitively.
OUT_OF_CHARACTER_PHRASES = [
    "as an ai",
    "i'm an ai",
    "i am an ai",
    "i am an llm",
    "i'm an llm",
    "i'm a language model",
    "i am a language model",
    "as a language model",
]


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------


@dataclass
class Action:
    """A valid, parsed action from a player."""

    action_type: str
    content: str
    target: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class ParseFailure:
    """Returned by parse_action when the structure cannot be extracted."""

    errors: List[str] = field(default_factory=list)


@dataclass
class NonCompliantAction:
    """A turn where the player's response was invalid after re-prompting."""

    raw_text: str
    reason: str
    action_type: str = "non_compliant"


@dataclass
class PlayerState:
    """Everything ``build_player_prompt`` needs for one player's turn."""

    player_id: str
    role: str
    role_card: Any
    scenario: Any
    backend: Any
    model_config: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------


class PromptError(Exception):
    """Raised when prompt construction fails (missing role card, scenario)."""


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------


def _normalize(text: Any) -> str:
    """Lowercase and collapse all whitespace (contract's comparison mode)."""
    return re.sub(r"\s+", " ", str(text)).strip().lower()


def _get(state: Any, key: str) -> Any:
    """Read ``key`` from a dict or from an object (duck-typed state)."""
    if isinstance(state, dict):
        return state.get(key)
    return getattr(state, key, None)


def _ex_field(exchange: Any, key: str, default: str = "") -> Any:
    """Read a field from a transcript exchange (dict or dataclass)."""
    if isinstance(exchange, dict):
        return exchange.get(key, default)
    return getattr(exchange, key, default)


def _coerce_content(value: Any) -> Optional[str]:
    """Flatten JSON content into a string; None if absent."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


# --------------------------------------------------------------------------
# Prompt construction and isolation
# --------------------------------------------------------------------------


def build_player_prompt(
    state: Any,
    transcript: List[Any],
    round_info: Dict[str, Any],
    must_respond_to: Optional[str] = None,
) -> List[Dict[str, str]]:
    """
    Build the messages list for the LLM backend.

    System message: role, goal, session rules, allowed action types and the
    required JSON reply format. User message: public scenario text, the
    player's own private role card, the shared transcript, current round/phase,
    any pending question, and the reply instruction.

    Raises PromptError if ``state`` lacks a role card or scenario.
    Side effects: none. Isolation property: the prompt contains ONLY the
    player's own role card plus public material -- never another player's
    cards, never the Traitor's crime declaration/cover story (unless this
    player IS the Traitor), never the Detective hint (unless this player IS
    the Detective).
    """
    role_card = _get(state, "role_card")
    scenario = _get(state, "scenario")
    if role_card is None:
        raise PromptError("build_player_prompt: state lacks a role card")
    if scenario is None:
        raise PromptError("build_player_prompt: state lacks a scenario")

    player_id = str(_get(state, "player_id") or "unknown")
    role = str(_get(state, "role") or "unknown")
    round_info = round_info or {}
    scenario_name = str(getattr(scenario, "name", "") or "")
    scenario_description = str(getattr(scenario, "description", "") or "")
    crime_window = str(getattr(scenario, "crime_window", "") or "")

    phase = str(round_info.get("phase", "")).lower()
    if phase in ("final_vote", "final", "vote"):
        allowed = ["final_vote"]
    else:
        allowed = DISCUSSION_ACTION_TYPES
    allowed_text = ", ".join(allowed)

    goal = str(getattr(role_card, "goal", "") or "")
    system_parts = [
        f"You are {player_id}, a player in a social-deduction game"
        + (f" ({scenario_name})" if scenario_name else "") + ".",
        f"Your role: {role}. Your goal: {goal}",
        "Session rules:",
        "- Never announce your role and never reveal any player's role.",
        "- You may share or withhold your observations, but never claim "
        "observations you were not given.",
        "- Stay in character at all times; never mention being an AI or a "
        "language model.",
        f"- Your reply must be exactly one action of one of these types: "
        f"{allowed_text}.",
        '- Reply with ONLY a JSON object: {"action_type": "...", "content": '
        '"...", "target": "...", "reason": "..."}.',
    ]
    system_content = "\n".join(system_parts)

    user_parts: List[str] = []
    user_parts.append("PUBLIC SCENARIO")
    user_parts.append(scenario_description)
    if crime_window:
        user_parts.append(f"Crime window: {crime_window}")
    user_parts.append("")
    user_parts.append("YOUR PRIVATE ROLE CARD (keep it secret)")

    observations = getattr(role_card, "observations", None) or []
    if observations:
        user_parts.append("Observations:")
        for obs in observations:
            user_parts.append(f"- {obs}")

    crime_declaration = getattr(role_card, "crime_declaration", None)
    if crime_declaration:
        user_parts.append(f"Crime declaration (never reveal): {crime_declaration}")
    cover_story = getattr(role_card, "cover_story", None)
    if cover_story:
        user_parts.append(f"Cover story (never reveal): {cover_story}")
    detective_hint = getattr(role_card, "detective_hint", None)
    if detective_hint:
        user_parts.append(f"Detective hint (never reveal): {detective_hint}")

    user_parts.append("")
    user_parts.append("TRANSCRIPT OF PRIOR EXCHANGES")
    if transcript:
        for exchange in transcript:
            speaker = str(_ex_field(exchange, "player_id", "?"))
            action_type = str(_ex_field(exchange, "action_type", "statement"))
            content = str(_ex_field(exchange, "content", ""))
            line = f"{speaker} ({action_type}): {content}"
            target = _ex_field(exchange, "target")
            reason = _ex_field(exchange, "reason")
            if target:
                line += f" [target: {target}]"
            if reason:
                line += f" [reason: {reason}]"
            user_parts.append(line)
    else:
        user_parts.append("No prior exchanges yet.")

    user_parts.append("")
    user_parts.append(
        f"Current round: {round_info.get('round', '?')} | "
        f"Phase: {round_info.get('phase', '?')}"
    )
    if must_respond_to:
        user_parts.append(
            f"There is a question pending to you from {must_respond_to}; "
            "you must respond to it."
        )
    if allowed == ["final_vote"]:
        user_parts.append(
            "You are voting privately. Your vote must be one cast member's "
            "household name, or 'no accusation'."
        )
    user_parts.append("")
    user_parts.append(
        "Reply with exactly one action in this JSON format: "
        f'{{"action_type": "<one of {allowed_text}>", "content": "what you '
        'say or do", "target": "named player or empty", "reason": "only for '
        'formal accusation"}'
    )
    user_content = "\n".join(user_parts)

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


def assert_prompt_isolated(
    prompt_text: str,
    private_materials_by_player: Dict[str, Any],
    player_id: str,
) -> List[str]:
    """
    Pure helper: check whether the prompt leaks other players' private material.

    For every OTHER player's private material strings (goal, observations,
    crime declaration, cover story, detective hint) and household name, checks
    whether the material appears in ``prompt_text`` (case-insensitive, after
    collapsing whitespace). Own material may appear -- occurrences of the
    current player's own materials are excluded from the scanned text first,
    so a name that only appears inside the player's own card (e.g. the
    Traitor's card mentioning another household) is not a leak.

    Returns a list of violation descriptions; empty list = isolated.
    Never raises. No side effects.
    """
    violations: List[str] = []
    norm_prompt = _normalize(prompt_text)

    own_materials = private_materials_by_player.get(player_id, ()) or ()
    own_normalized = [_normalize(m) for m in own_materials if str(m).strip()]

    remaining = norm_prompt
    for own in own_normalized:
        remaining = remaining.replace(own, " ")

    for other_id, materials in private_materials_by_player.items():
        if other_id == player_id:
            continue
        for material in materials:
            norm_material = _normalize(material)
            if norm_material and norm_material in remaining:
                violations.append(
                    f"prompt for {player_id} contains private material from "
                    f"{other_id}: {material}"
                )
        other_name = _normalize(other_id)
        if other_name and other_name in remaining:
            violations.append(
                f"prompt for {player_id} contains the household name of {other_id}"
            )

    return violations


# --------------------------------------------------------------------------
# Action parsing and validation
# --------------------------------------------------------------------------


def parse_action(
    raw_text: str,
    allowed_types: List[str],
    cast_names: List[str],
) -> Union[Action, ParseFailure]:
    """
    Parse the model's reply into an Action.

    Accepts JSON (``{"action_type": ..., "content": ..., "target": ...,
    "reason": ...}``, possibly embedded in prose or markdown fences), a
    labelled plain-text format (``ACTION: ...`` / ``TARGET: ...`` /
    ``REASON: ...`` / ``TEXT: ...``), and -- for final votes -- a bare cast
    member name or "no accusation".

    Returns ParseFailure(errors) when the structure cannot be extracted.
    Never raises. No side effects.
    """
    allowed = list(allowed_types or [])
    cast_names = list(cast_names or [])
    errors: List[str] = []

    parsed = _parse_json_action(raw_text, allowed, errors)
    if parsed is not None:
        return parsed

    parsed = _parse_plain_text_action(raw_text, allowed, errors)
    if parsed is not None:
        return parsed

    if "final_vote" in allowed:
        candidate = raw_text.strip()
        candidate = re.sub(r"[.!\"'`]+$", "", candidate).strip()
        if candidate.lower() == FINAL_VOTE_NO_ACCUSATION or candidate.lower() in {
            c.lower() for c in cast_names
        }:
            return Action(action_type="final_vote", content=candidate)

    if not errors:
        errors.append("could not extract an action from the reply")
    return ParseFailure(errors=errors)


def _parse_json_action(
    raw_text: str,
    allowed: List[str],
    errors: List[str],
) -> Optional[Action]:
    """Attempt JSON extraction (first balanced object, fences/prose tolerant)."""
    match = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    action_type = data.get("action_type")
    if not isinstance(action_type, str) or not action_type.strip():
        errors.append("missing action_type")
        return None
    action_type = action_type.strip()
    if action_type not in allowed:
        errors.append(
            f"action type {action_type!r} is not allowed "
            f"(allowed: {', '.join(allowed)})"
        )
        return None

    content = _coerce_content(data.get("content"))
    if content is None or not content.strip():
        errors.append("content must be non-empty")
        return None

    target = data.get("target")
    if target is not None and not isinstance(target, str):
        target = str(target)
    reason = data.get("reason")
    if reason is not None and not isinstance(reason, str):
        reason = str(reason)

    if action_type == "question" and (target is None or not target.strip()):
        errors.append("question requires a named target")
        return None
    if action_type == "formal_accusation":
        if target is None or not target.strip():
            errors.append("formal_accusation requires a target")
            return None
        if reason is None or not str(reason).strip():
            errors.append("formal_accusation requires a reason")
            return None

    return Action(
        action_type=action_type,
        content=content.strip(),
        target=target.strip() if isinstance(target, str) else None,
        reason=reason.strip() if isinstance(reason, str) else None,
    )


def _parse_plain_text_action(
    raw_text: str,
    allowed: List[str],
    errors: List[str],
) -> Optional[Action]:
    """Attempt the labelled plain-text format (resilience bonus)."""
    m_type = re.search(r"(?im)^\s*ACTION\s*:\s*([^\n]+?)\s*$", raw_text)
    if not m_type:
        return None

    action_type = m_type.group(1).strip()
    if action_type not in allowed:
        errors.append(
            f"action type {action_type!r} is not allowed "
            f"(allowed: {', '.join(allowed)})"
        )
        return None

    m_text = re.search(
        r"(?is)^\s*TEXT\s*:\s*(.+?)(?=^\s*(?:ACTION|TARGET|REASON)\s*:|\Z)",
        raw_text,
    )
    m_target = re.search(r"(?im)^\s*TARGET\s*:\s*([^\n]+?)\s*$", raw_text)
    m_reason = re.search(r"(?im)^\s*REASON\s*:\s*([^\n]+?)\s*$", raw_text)

    content = m_text.group(1).strip() if m_text else None
    if content is None or not content.strip():
        errors.append("content must be non-empty")
        return None

    target = m_target.group(1).strip() if m_target else None
    if target and target.lower() in ("none", "null", "n/a"):
        target = None
    reason = m_reason.group(1).strip() if m_reason else None

    if action_type == "question" and (target is None or not target.strip()):
        errors.append("question requires a named target")
        return None
    if action_type == "formal_accusation":
        if target is None or not target.strip():
            errors.append("formal_accusation requires a target")
            return None
        if reason is None or not reason.strip():
            errors.append("formal_accusation requires a reason")
            return None

    return Action(
        action_type=action_type,
        content=content,
        target=target,
        reason=reason,
    )


def validate_action(
    action: Action,
    cast_names: List[str],
    rules: Dict[str, Any],
) -> List[str]:
    """
    Validate an action against the game rules.

    Checks: question requires a target in the cast; formal_accusation requires
    a target in the cast and a non-empty reason; final_vote content must be a
    cast member name or "no accusation"; any non-empty target must be in the cast;
    content must be non-empty; content must not contain role-revealing phrases
    (rule §4) or out-of-character chatter (rule §3).

    Returns a list of problems (empty = valid). Never raises. No side effects.
    """
    problems: List[str] = []
    rules = rules or {}
    cast_lower = {str(c).strip().lower() for c in (cast_names or [])}

    action_type = action.action_type
    content = str(action.content or "").strip()
    target = action.target
    reason = action.reason

    if not content:
        problems.append("content must be non-empty")

    # Normalize target: treat empty strings as None (Claude may return "" for optional targets)
    if isinstance(target, str) and not target.strip():
        target = None

    if target is not None:
        if str(target).strip().lower() not in cast_lower:
            problems.append(f"target {target!r} is not in the cast")

    if action_type == "question":
        if target is None or not str(target).strip():
            problems.append("question requires a target in the cast")
    elif action_type == "formal_accusation":
        if target is None or not str(target).strip():
            problems.append("formal_accusation requires a target in the cast")
        if reason is None or not str(reason).strip():
            problems.append("formal_accusation requires a non-empty reason")
    elif action_type == "final_vote":
        vote = content.lower()
        if vote != FINAL_VOTE_NO_ACCUSATION and vote not in cast_lower:
            problems.append(
                f"final_vote content must be a cast member name or "
                f"{FINAL_VOTE_NO_ACCUSATION!r}, got {content!r}"
            )

    content_lower = content.lower()
    reveal_phrases = rules.get("role_reveal_phrases", ROLE_REVEAL_PHRASES)
    for phrase in reveal_phrases:
        if phrase in content_lower:
            problems.append(f"content reveals your role (contains {phrase!r})")
            break
    ooc_phrases = rules.get("out_of_character_phrases", OUT_OF_CHARACTER_PHRASES)
    for phrase in ooc_phrases:
        if phrase in content_lower:
            problems.append(
                f"content contains out-of-character AI chatter (contains {phrase!r})"
            )
            break

    return problems


# --------------------------------------------------------------------------
# Player agent
# --------------------------------------------------------------------------


class PlayerAgent:
    """LLM-backed player agent."""

    def __init__(
        self,
        identity: Any,
        role_card: Any,
        scenario: Any,
        backend: Any,
        model_config: Dict[str, Any],
    ) -> None:
        self.identity = identity
        self.role_card = role_card
        self.scenario = scenario
        self.backend = backend
        self.model_config = model_config or {}
        self.cast_names = [p.household for p in scenario.players]
        self.state = PlayerState(
            player_id=identity.player_id,
            role=identity.role,
            role_card=role_card,
            scenario=scenario,
            backend=backend,
            model_config=self.model_config,
        )

    def act(
        self,
        transcript: List[Any],
        round_info: Dict[str, Any],
        must_respond_to: Optional[str] = None,
    ) -> Union[Action, NonCompliantAction]:
        """
        Generate one discussion action for this player.

        Returns Action or NonCompliantAction. Raises BackendError subclasses
        (from the backend) and PromptError (from prompt construction).
        """
        return self._generate_action(transcript, round_info, must_respond_to)

    def final_vote(self, transcript: List[Any]) -> Union[Action, NonCompliantAction]:
        """
        Generate the player's private final vote (restricted to final_vote).

        Returns Action (final_vote) or NonCompliantAction. Raises BackendError
        subclasses and PromptError.
        """
        return self._generate_action(
            transcript,
            {"round": "final", "phase": "final_vote"},
            must_respond_to=None,
        )

    def _generate_action(
        self,
        transcript: List[Any],
        round_info: Dict[str, Any],
        must_respond_to: Optional[str],
    ) -> Union[Action, NonCompliantAction]:
        phase = str((round_info or {}).get("phase", "")).lower()
        if phase in ("final_vote", "final", "vote"):
            allowed = ["final_vote"]
        else:
            allowed = DISCUSSION_ACTION_TYPES

        messages = build_player_prompt(self.state, transcript, round_info, must_respond_to)

        kwargs: Dict[str, Any] = {}
        for key in ("max_tokens", "temperature", "timeout"):
            if key in self.model_config:
                kwargs[key] = self.model_config[key]
        rules = self.model_config.get("rules", {})

        last_raw = ""
        last_errors: List[str] = []
        for attempt in range(2):
            response = self.backend.complete(messages, **kwargs)
            last_raw = response.text
            parsed = parse_action(response.text, allowed, self.cast_names)
            if isinstance(parsed, ParseFailure):
                last_errors = list(parsed.errors)
            else:
                problems = validate_action(parsed, self.cast_names, rules)
                if not problems:
                    return parsed
                last_errors = problems
            if attempt == 0:
                messages = list(messages) + [
                    {
                        "role": "user",
                        "content": (
                            "Your previous reply was invalid: "
                            + "; ".join(last_errors)
                            + ". Reply with exactly one valid action."
                        ),
                    }
                ]
        return NonCompliantAction(raw_text=last_raw, reason="; ".join(last_errors))


__all__ = [
    "Action",
    "NonCompliantAction",
    "ParseFailure",
    "PromptError",
    "PlayerState",
    "PlayerAgent",
    "build_player_prompt",
    "assert_prompt_isolated",
    "parse_action",
    "validate_action",
]
