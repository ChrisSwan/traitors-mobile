"""
Acceptance tests for Player module (traitors-mobile-player).

Contract: specs/contracts/player.md

Tests cover:
1. Contract compliance: PlayerAgent, build_player_prompt, parse_action, validate_action,
   assert_prompt_isolated, and related data types exist with correct signatures.
2. Behavioral: build_player_prompt constructs valid prompts with scenario + role card + transcript.
3. Prompt isolation: build_player_prompt for each of 5 players passes assert_prompt_isolated
   against all other players' private materials (zero violations).
4. Action parsing: parse_action on all six action types yields correct Action fields;
   invalid structures yield ParseFailure.
5. Action validation: validate_action flags role-revealing and out-of-character content.
6. Re-prompting on failure: MockBackend returning garbage twice → NonCompliantAction with
   recorded reason; caller continues (no exception escapes act).
7. Backend error propagation: MockBackend raising BackendUnavailableError propagates out of act.
8. Final vote parsing/validation: bare name or 'no accusation' are valid.
9. Error types: PromptError, ParseFailure, NonCompliantAction are defined and used correctly.
"""

import pytest
import json
from unittest.mock import Mock, patch, MagicMock
from dataclasses import dataclass, field
from typing import Optional, List, Dict

# Import Player module (will exist after Engineer implements it)
from traitors_mobile.player import (
    Action,
    NonCompliantAction,
    ParseFailure,
    PromptError,
    PlayerAgent,
    build_player_prompt,
    parse_action,
    validate_action,
    assert_prompt_isolated,
)

# Import dependencies from other modules
from traitors_mobile.scenario import (
    PlayerIdentity,
    RoleCard,
    Scenario,
    default_scenario,
    build_scenario,
)

from traitors_mobile.llm_backend import (
    LLMResponse,
    MockBackend,
    BackendUnavailableError,
)


# Test fixtures and helper data

@pytest.fixture
def mock_scenario():
    """A built scenario with 5 players for testing."""
    defn = default_scenario()
    return build_scenario(defn, seed=42)


@pytest.fixture
def mock_backend():
    """A MockBackend for deterministic tests."""
    return MockBackend(scripted=[
        '{"action_type": "statement", "content": "I saw something"}',
        "Invalid garbage response",
    ])


@pytest.fixture
def default_cast_names(mock_scenario):
    """Extract the 5 cast member names from the scenario."""
    return [p.household for p in mock_scenario.players]


def extract_private_materials(scenario):
    """Helper: extract all private materials from a scenario for leakage checks.
    
    Returns: dict[player_id] -> set of text strings from that player's card.
    """
    materials_by_player = {}
    for player in scenario.players:
        materials = set()
        card = player.role_card
        
        materials.add(card.goal.lower())
        for obs in card.observations:
            materials.add(obs.lower())
        if card.crime_declaration:
            materials.add(card.crime_declaration.lower())
        if card.cover_story:
            materials.add(card.cover_story.lower())
        if card.detective_hint:
            materials.add(card.detective_hint.lower())
        
        materials_by_player[player.player_id] = materials
    
    return materials_by_player


class TestContractCompliance:
    """Verify that required types, functions, and signatures exist."""

    def test_action_dataclass_exists(self):
        """Action dataclass must exist with correct fields."""
        action = Action(
            action_type="statement",
            content="I saw something",
            target=None,
            reason=None
        )
        assert action.action_type == "statement"
        assert action.content == "I saw something"
        assert action.target is None
        assert action.reason is None

    def test_action_question_with_target(self):
        """Action for question type must have target field."""
        action = Action(
            action_type="question",
            content="What did you see?",
            target="Alice",
            reason=None
        )
        assert action.target == "Alice"

    def test_action_formal_accusation_with_reason(self):
        """Action for formal_accusation must have target and reason."""
        action = Action(
            action_type="formal_accusation",
            content="I accuse you",
            target="Bob",
            reason="You acted suspiciously"
        )
        assert action.target == "Bob"
        assert action.reason == "You acted suspiciously"

    def test_action_final_vote(self):
        """Action for final_vote has content (player name or 'no accusation')."""
        action = Action(
            action_type="final_vote",
            content="Alice",
            target=None,
            reason=None
        )
        assert action.content == "Alice"

    def test_non_compliant_action_dataclass_exists(self):
        """NonCompliantAction must exist and capture raw text + reason."""
        nc_action = NonCompliantAction(
            raw_text="garbage response",
            reason="Could not parse action type",
            action_type="non_compliant"
        )
        assert nc_action.raw_text == "garbage response"
        assert nc_action.reason == "Could not parse action type"
        assert nc_action.action_type == "non_compliant"

    def test_parse_failure_dataclass_exists(self):
        """ParseFailure must exist (returned by parse_action on invalid input)."""
        pf = ParseFailure(errors=["Missing action_type", "Invalid target"])
        assert pf.errors == ["Missing action_type", "Invalid target"]

    def test_prompt_error_exception_exists(self):
        """PromptError exception must exist (raised by build_player_prompt)."""
        err = PromptError("Missing role card")
        assert isinstance(err, Exception)
        assert "Missing" in str(err)

    def test_build_player_prompt_exists_and_callable(self):
        """build_player_prompt(state, transcript, round_info, must_respond_to) must exist."""
        assert callable(build_player_prompt)

    def test_assert_prompt_isolated_exists_and_callable(self):
        """assert_prompt_isolated(prompt_text, private_materials_by_player, player_id) must exist."""
        assert callable(assert_prompt_isolated)

    def test_parse_action_exists_and_callable(self):
        """parse_action(raw_text, allowed_types, cast_names) must exist."""
        assert callable(parse_action)

    def test_validate_action_exists_and_callable(self):
        """validate_action(action, cast_names, rules) must exist."""
        assert callable(validate_action)

    def test_player_agent_class_exists(self):
        """PlayerAgent class must exist."""
        assert callable(PlayerAgent)

    def test_player_agent_act_method_exists(self):
        """PlayerAgent.act(transcript, round_info, must_respond_to) must exist."""
        # Create a minimal PlayerAgent to verify the method exists
        assert hasattr(PlayerAgent, 'act')
        assert callable(getattr(PlayerAgent, 'act'))

    def test_player_agent_final_vote_method_exists(self):
        """PlayerAgent.final_vote(transcript) must exist."""
        assert hasattr(PlayerAgent, 'final_vote')
        assert callable(getattr(PlayerAgent, 'final_vote'))


class TestBuildPlayerPrompt:
    """Test build_player_prompt function."""

    def test_build_player_prompt_returns_messages_list(self, mock_scenario):
        """build_player_prompt returns a list of message dicts."""
        player = mock_scenario.players[0]
        
        state = {
            "player_id": player.player_id,
            "role": player.role,
            "role_card": player.role_card,
            "scenario": mock_scenario,
            "backend": MockBackend(scripted=[]),
            "model_config": {},
        }
        
        messages = build_player_prompt(
            state,
            transcript=[],
            round_info={"round": 1, "phase": "discussion"},
            must_respond_to=None
        )
        
        assert isinstance(messages, list)
        assert len(messages) >= 1
        assert all(isinstance(msg, dict) for msg in messages)
        assert all("role" in msg and "content" in msg for msg in messages)

    def test_build_player_prompt_has_system_message(self, mock_scenario):
        """Prompt must have a system message with role/goal/rules."""
        player = mock_scenario.players[0]
        
        state = {
            "player_id": player.player_id,
            "role": player.role,
            "role_card": player.role_card,
            "scenario": mock_scenario,
            "backend": MockBackend(scripted=[]),
            "model_config": {},
        }
        
        messages = build_player_prompt(
            state,
            transcript=[],
            round_info={"round": 1, "phase": "discussion"},
        )
        
        # Should have a system message
        assert messages[0]["role"] == "system"
        system_text = messages[0]["content"].lower()
        
        # System message should reference game rules/actions
        assert any(word in system_text for word in ["statement", "question", "challenge", "action"])

    def test_build_player_prompt_includes_role_card(self, mock_scenario):
        """Prompt must include the player's own role card."""
        player = mock_scenario.players[0]
        
        state = {
            "player_id": player.player_id,
            "role": player.role,
            "role_card": player.role_card,
            "scenario": mock_scenario,
            "backend": MockBackend(scripted=[]),
            "model_config": {},
        }
        
        messages = build_player_prompt(
            state,
            transcript=[],
            round_info={"round": 1, "phase": "discussion"},
        )
        
        # Concatenate all messages to check for role card content
        full_text = " ".join(msg["content"] for msg in messages).lower()
        
        # Should include at least one observation from the player's card
        assert any(
            obs.lower() in full_text
            for obs in player.role_card.observations
        )

    def test_build_player_prompt_includes_scenario_text(self, mock_scenario):
        """Prompt must include the public scenario text."""
        player = mock_scenario.players[0]
        
        state = {
            "player_id": player.player_id,
            "role": player.role,
            "role_card": player.role_card,
            "scenario": mock_scenario,
            "backend": MockBackend(scripted=[]),
            "model_config": {},
        }
        
        messages = build_player_prompt(
            state,
            transcript=[],
            round_info={"round": 1, "phase": "discussion"},
        )
        
        full_text = " ".join(msg["content"] for msg in messages)
        
        # Should include scenario description
        assert "Meadowbrook" in full_text or mock_scenario.scenario_id in full_text

    def test_build_player_prompt_raises_on_missing_role_card(self, mock_scenario):
        """build_player_prompt raises PromptError if state lacks role_card."""
        state = {
            "player_id": "test",
            "role": "traitor",
            # Missing role_card
            "scenario": mock_scenario,
            "backend": MockBackend(scripted=[]),
            "model_config": {},
        }
        
        with pytest.raises(PromptError):
            build_player_prompt(
                state,
                transcript=[],
                round_info={"round": 1, "phase": "discussion"},
            )

    def test_build_player_prompt_raises_on_missing_scenario(self, mock_scenario):
        """build_player_prompt raises PromptError if state lacks scenario."""
        player = mock_scenario.players[0]
        
        state = {
            "player_id": player.player_id,
            "role": player.role,
            "role_card": player.role_card,
            # Missing scenario
            "backend": MockBackend(scripted=[]),
            "model_config": {},
        }
        
        with pytest.raises(PromptError):
            build_player_prompt(
                state,
                transcript=[],
                round_info={"round": 1, "phase": "discussion"},
            )

    def test_build_player_prompt_includes_transcript_when_present(self, mock_scenario):
        """Prompt should include prior exchanges when transcript is non-empty."""
        player = mock_scenario.players[0]
        
        # Mock a simple transcript exchange
        transcript = [
            {
                "player_id": "Alice",
                "action_type": "statement",
                "content": "I saw the culprit",
            }
        ]
        
        state = {
            "player_id": player.player_id,
            "role": player.role,
            "role_card": player.role_card,
            "scenario": mock_scenario,
            "backend": MockBackend(scripted=[]),
            "model_config": {},
        }
        
        messages = build_player_prompt(
            state,
            transcript=transcript,
            round_info={"round": 2, "phase": "discussion"},
        )
        
        full_text = " ".join(msg["content"] for msg in messages)
        
        # Transcript content should appear
        assert "Alice" in full_text or "statement" in full_text.lower()

    def test_build_player_prompt_includes_must_respond_to_when_set(self, mock_scenario):
        """When must_respond_to is set, prompt should mention the pending question."""
        player = mock_scenario.players[0]
        
        state = {
            "player_id": player.player_id,
            "role": player.role,
            "role_card": player.role_card,
            "scenario": mock_scenario,
            "backend": MockBackend(scripted=[]),
            "model_config": {},
        }
        
        messages = build_player_prompt(
            state,
            transcript=[],
            round_info={"round": 1, "phase": "discussion"},
            must_respond_to="Alice"
        )
        
        full_text = " ".join(msg["content"] for msg in messages)
        
        # Should mention that there's a pending question
        assert "question" in full_text.lower() or "respond" in full_text.lower() or "Alice" in full_text


class TestPromptIsolation:
    """Test assert_prompt_isolated and prompt leakage prevention."""

    def test_assert_prompt_isolated_returns_list(self, mock_scenario):
        """assert_prompt_isolated returns a list (empty = isolated)."""
        player = mock_scenario.players[0]
        
        state = {
            "player_id": player.player_id,
            "role": player.role,
            "role_card": player.role_card,
            "scenario": mock_scenario,
            "backend": MockBackend(scripted=[]),
            "model_config": {},
        }
        
        prompt_messages = build_player_prompt(
            state,
            transcript=[],
            round_info={"round": 1, "phase": "discussion"},
        )
        prompt_text = " ".join(msg["content"] for msg in prompt_messages)
        
        private_materials = extract_private_materials(mock_scenario)
        
        violations = assert_prompt_isolated(
            prompt_text,
            private_materials,
            player.player_id
        )
        
        assert isinstance(violations, list)

    def test_assert_prompt_isolated_detects_traitor_crime_in_other_prompts(self, mock_scenario):
        """Traitor's crime_declaration should not appear in any other player's prompt."""
        # Get traitor and loyalist
        traitor = next(p for p in mock_scenario.players if p.role == "traitor")
        loyalist = next(p for p in mock_scenario.players if p.role == "loyalist_a")
        
        # Get private materials
        private_materials = extract_private_materials(mock_scenario)
        
        # Build loyalist's prompt
        state = {
            "player_id": loyalist.player_id,
            "role": loyalist.role,
            "role_card": loyalist.role_card,
            "scenario": mock_scenario,
            "backend": MockBackend(scripted=[]),
            "model_config": {},
        }
        
        prompt_messages = build_player_prompt(
            state,
            transcript=[],
            round_info={"round": 1, "phase": "discussion"},
        )
        prompt_text = " ".join(msg["content"] for msg in prompt_messages)
        
        violations = assert_prompt_isolated(
            prompt_text,
            private_materials,
            loyalist.player_id
        )
        
        # Should have no violations (traitor's crime not leaked)
        assert isinstance(violations, list)
        # If traitor has a crime_declaration, it should not be in violations
        if traitor.role_card.crime_declaration:
            crime_lower = traitor.role_card.crime_declaration.lower()
            # Check that the crime declaration is not mentioned in violation strings
            assert not any(crime_lower in v.lower() for v in violations)

    def test_all_five_players_have_isolated_prompts(self, mock_scenario):
        """For all 5 players, their prompt must not leak other players' private material."""
        private_materials = extract_private_materials(mock_scenario)
        
        all_violations = {}
        
        for player in mock_scenario.players:
            state = {
                "player_id": player.player_id,
                "role": player.role,
                "role_card": player.role_card,
                "scenario": mock_scenario,
                "backend": MockBackend(scripted=[]),
                "model_config": {},
            }
            
            prompt_messages = build_player_prompt(
                state,
                transcript=[],
                round_info={"round": 1, "phase": "discussion"},
            )
            prompt_text = " ".join(msg["content"] for msg in prompt_messages)
            
            violations = assert_prompt_isolated(
                prompt_text,
                private_materials,
                player.player_id
            )
            
            all_violations[player.player_id] = violations
        
        # Assert no player has any leakage
        for player_id, violations in all_violations.items():
            assert violations == [], f"Player {player_id} has prompt isolation violations: {violations}"


class TestParseAction:
    """Test parse_action function."""

    def test_parse_action_json_statement(self):
        """parse_action on valid JSON statement yields Action."""
        raw_text = '{"action_type": "statement", "content": "I saw the culprit", "target": null, "reason": null}'
        allowed_types = ["statement", "question", "challenge", "corroboration", "formal_accusation", "final_vote"]
        cast_names = ["Alice", "Bob", "Carol"]
        
        result = parse_action(raw_text, allowed_types, cast_names)
        
        assert isinstance(result, Action)
        assert result.action_type == "statement"
        assert result.content == "I saw the culprit"

    def test_parse_action_json_question_with_target(self):
        """parse_action on question JSON requires and returns target."""
        raw_text = '{"action_type": "question", "content": "What did you see?", "target": "Alice", "reason": null}'
        allowed_types = ["statement", "question", "challenge", "corroboration", "formal_accusation", "final_vote"]
        cast_names = ["Alice", "Bob", "Carol"]
        
        result = parse_action(raw_text, allowed_types, cast_names)
        
        assert isinstance(result, Action)
        assert result.action_type == "question"
        assert result.target == "Alice"

    def test_parse_action_json_challenge(self):
        """parse_action on challenge JSON."""
        raw_text = '{"action_type": "challenge", "content": "That makes no sense", "target": "Bob", "reason": null}'
        allowed_types = ["statement", "question", "challenge", "corroboration", "formal_accusation", "final_vote"]
        cast_names = ["Alice", "Bob", "Carol"]
        
        result = parse_action(raw_text, allowed_types, cast_names)
        
        assert isinstance(result, Action)
        assert result.action_type == "challenge"

    def test_parse_action_json_corroboration(self):
        """parse_action on corroboration JSON."""
        raw_text = '{"action_type": "corroboration", "content": "I agree with Alice", "target": "Alice", "reason": null}'
        allowed_types = ["statement", "question", "challenge", "corroboration", "formal_accusation", "final_vote"]
        cast_names = ["Alice", "Bob", "Carol"]
        
        result = parse_action(raw_text, allowed_types, cast_names)
        
        assert isinstance(result, Action)
        assert result.action_type == "corroboration"

    def test_parse_action_json_formal_accusation(self):
        """parse_action on formal_accusation JSON requires target and reason."""
        raw_text = '{"action_type": "formal_accusation", "content": "I accuse you", "target": "Carol", "reason": "Suspicious behavior"}'
        allowed_types = ["statement", "question", "challenge", "corroboration", "formal_accusation", "final_vote"]
        cast_names = ["Alice", "Bob", "Carol"]
        
        result = parse_action(raw_text, allowed_types, cast_names)
        
        assert isinstance(result, Action)
        assert result.action_type == "formal_accusation"
        assert result.target == "Carol"
        assert result.reason == "Suspicious behavior"

    def test_parse_action_json_final_vote(self):
        """parse_action on final_vote JSON - bare name or 'no accusation'."""
        raw_text = '{"action_type": "final_vote", "content": "Alice", "target": null, "reason": null}'
        allowed_types = ["final_vote"]
        cast_names = ["Alice", "Bob", "Carol"]
        
        result = parse_action(raw_text, allowed_types, cast_names)
        
        assert isinstance(result, Action)
        assert result.action_type == "final_vote"
        assert result.content == "Alice"

    def test_parse_action_final_vote_no_accusation(self):
        """parse_action on final_vote with 'no accusation'."""
        raw_text = '{"action_type": "final_vote", "content": "no accusation", "target": null, "reason": null}'
        allowed_types = ["final_vote"]
        cast_names = ["Alice", "Bob", "Carol"]
        
        result = parse_action(raw_text, allowed_types, cast_names)
        
        assert isinstance(result, Action)
        assert result.action_type == "final_vote"
        assert result.content == "no accusation"

    def test_parse_action_invalid_json_returns_parse_failure(self):
        """parse_action on invalid JSON returns ParseFailure."""
        raw_text = "{ broken json }"
        allowed_types = ["statement"]
        cast_names = ["Alice"]
        
        result = parse_action(raw_text, allowed_types, cast_names)
        
        assert isinstance(result, ParseFailure)
        assert len(result.errors) > 0

    def test_parse_action_missing_action_type_returns_parse_failure(self):
        """parse_action on JSON without action_type returns ParseFailure."""
        raw_text = '{"content": "Something", "target": null}'
        allowed_types = ["statement"]
        cast_names = ["Alice"]
        
        result = parse_action(raw_text, allowed_types, cast_names)
        
        assert isinstance(result, ParseFailure)
        assert len(result.errors) > 0

    def test_parse_action_empty_content_returns_parse_failure(self):
        """parse_action with empty content returns ParseFailure."""
        raw_text = '{"action_type": "statement", "content": ""}'
        allowed_types = ["statement"]
        cast_names = ["Alice"]
        
        result = parse_action(raw_text, allowed_types, cast_names)
        
        assert isinstance(result, ParseFailure)

    def test_parse_action_question_without_target_returns_parse_failure(self):
        """parse_action on question without target returns ParseFailure."""
        raw_text = '{"action_type": "question", "content": "What?", "target": null}'
        allowed_types = ["question"]
        cast_names = ["Alice"]
        
        result = parse_action(raw_text, allowed_types, cast_names)
        
        assert isinstance(result, ParseFailure)


class TestValidateAction:
    """Test validate_action function."""

    def test_validate_action_valid_statement_returns_empty_list(self):
        """validate_action on valid statement returns empty list."""
        action = Action(
            action_type="statement",
            content="I saw the culprit",
            target=None,
            reason=None
        )
        cast_names = ["Alice", "Bob", "Carol"]
        rules = {}
        
        problems = validate_action(action, cast_names, rules)
        
        assert isinstance(problems, list)
        assert len(problems) == 0

    def test_validate_action_detects_role_revelation(self):
        """validate_action flags 'I am the traitor' style content."""
        action = Action(
            action_type="statement",
            content="I am the traitor and I stole the prize",
            target=None,
            reason=None
        )
        cast_names = ["Alice", "Bob", "Carol"]
        rules = {}
        
        problems = validate_action(action, cast_names, rules)
        
        assert len(problems) > 0
        assert any("role" in p.lower() or "traitor" in p.lower() for p in problems)

    def test_validate_action_detects_as_a_traitor(self):
        """validate_action flags 'as a traitor' style content."""
        action = Action(
            action_type="statement",
            content="As a traitor, I know the truth",
            target=None,
            reason=None
        )
        cast_names = ["Alice"]
        rules = {}
        
        problems = validate_action(action, cast_names, rules)
        
        assert len(problems) > 0

    def test_validate_action_detects_my_role_is(self):
        """validate_action flags 'my role is' style content."""
        action = Action(
            action_type="statement",
            content="My role is the detective",
            target=None,
            reason=None
        )
        cast_names = ["Alice"]
        rules = {}
        
        problems = validate_action(action, cast_names, rules)
        
        assert len(problems) > 0

    def test_validate_action_detects_ai_language_model(self):
        """validate_action flags 'I am a language model' out-of-character content."""
        action = Action(
            action_type="statement",
            content="I'm a language model and I must follow my instructions",
            target=None,
            reason=None
        )
        cast_names = ["Alice"]
        rules = {}
        
        problems = validate_action(action, cast_names, rules)
        
        assert len(problems) > 0
        assert any("ai" in p.lower() or "language model" in p.lower() or "character" in p.lower()
                   for p in problems)

    def test_validate_action_detects_as_an_ai(self):
        """validate_action flags 'as an AI' content."""
        action = Action(
            action_type="statement",
            content="As an AI, I cannot determine the truth",
            target=None,
            reason=None
        )
        cast_names = ["Alice"]
        rules = {}
        
        problems = validate_action(action, cast_names, rules)
        
        assert len(problems) > 0

    def test_validate_action_question_requires_target_in_cast(self):
        """validate_action flags question with target not in cast."""
        action = Action(
            action_type="question",
            content="What did you see?",
            target="NonexistentPlayer",
            reason=None
        )
        cast_names = ["Alice", "Bob", "Carol"]
        rules = {}
        
        problems = validate_action(action, cast_names, rules)
        
        assert len(problems) > 0
        assert any("target" in p.lower() or "cast" in p.lower() for p in problems)

    def test_validate_action_formal_accusation_requires_target(self):
        """validate_action flags formal_accusation without target."""
        action = Action(
            action_type="formal_accusation",
            content="I accuse someone",
            target=None,
            reason="They acted suspiciously"
        )
        cast_names = ["Alice"]
        rules = {}
        
        problems = validate_action(action, cast_names, rules)
        
        assert len(problems) > 0

    def test_validate_action_formal_accusation_requires_reason(self):
        """validate_action flags formal_accusation without reason."""
        action = Action(
            action_type="formal_accusation",
            content="I accuse someone",
            target="Alice",
            reason=None
        )
        cast_names = ["Alice"]
        rules = {}
        
        problems = validate_action(action, cast_names, rules)
        
        assert len(problems) > 0
        assert any("reason" in p.lower() for p in problems)

    def test_validate_action_final_vote_must_be_cast_member_or_no_accusation(self):
        """validate_action flags final_vote with invalid target."""
        action = Action(
            action_type="final_vote",
            content="NonexistentPlayer",
            target=None,
            reason=None
        )
        cast_names = ["Alice", "Bob", "Carol"]
        rules = {}
        
        problems = validate_action(action, cast_names, rules)
        
        assert len(problems) > 0

    def test_validate_action_final_vote_valid_name(self):
        """validate_action accepts final_vote with valid cast member name."""
        action = Action(
            action_type="final_vote",
            content="Alice",
            target=None,
            reason=None
        )
        cast_names = ["Alice", "Bob", "Carol"]
        rules = {}
        
        problems = validate_action(action, cast_names, rules)
        
        assert len(problems) == 0

    def test_validate_action_final_vote_no_accusation(self):
        """validate_action accepts final_vote with 'no accusation'."""
        action = Action(
            action_type="final_vote",
            content="no accusation",
            target=None,
            reason=None
        )
        cast_names = ["Alice", "Bob", "Carol"]
        rules = {}
        
        problems = validate_action(action, cast_names, rules)
        
        assert len(problems) == 0

    def test_validate_action_empty_content(self):
        """validate_action flags actions with empty content."""
        action = Action(
            action_type="statement",
            content="",
            target=None,
            reason=None
        )
        cast_names = ["Alice"]
        rules = {}
        
        problems = validate_action(action, cast_names, rules)
        
        assert len(problems) > 0

    def test_validate_action_empty_string_target_on_optional_target_action(self):
        """Empty-string targets on optional-target actions (statement, challenge, etc.) are normalized to None and accepted.
        
        This handles Claude's reliable behavior of returning empty strings for
        optional targets instead of null. See SWA-164.
        """
        action = Action(
            action_type="statement",
            content="I saw something suspicious",
            target="",  # Empty string, not None
            reason=None
        )
        cast_names = ["Alice", "Bob", "Carol"]
        rules = {}
        
        problems = validate_action(action, cast_names, rules)
        
        # Should be valid - empty string on optional-target action is normalized to None
        assert len(problems) == 0

    def test_validate_action_empty_string_target_on_required_target_action_question(self):
        """Empty-string targets on required-target actions (question) are flagged as invalid."""
        action = Action(
            action_type="question",
            content="What did you see?",
            target="",  # Empty string for a question (which requires a target)
            reason=None
        )
        cast_names = ["Alice", "Bob", "Carol"]
        rules = {}
        
        problems = validate_action(action, cast_names, rules)
        
        # Should be invalid - question requires a non-empty target
        assert len(problems) > 0
        assert any("question requires" in p.lower() or "target" in p.lower() for p in problems)

    def test_validate_action_empty_string_target_on_required_target_action_formal_accusation(self):
        """Empty-string targets on formal_accusation are flagged as invalid."""
        action = Action(
            action_type="formal_accusation",
            content="I accuse you",
            target="",  # Empty string for formal_accusation (which requires a target)
            reason="You acted suspiciously"
        )
        cast_names = ["Alice", "Bob", "Carol"]
        rules = {}
        
        problems = validate_action(action, cast_names, rules)
        
        # Should be invalid - formal_accusation requires a non-empty target
        assert len(problems) > 0
        assert any("formal_accusation requires" in p.lower() or "target" in p.lower() for p in problems)


class TestPlayerAgentAct:
    """Test PlayerAgent.act method."""

    def test_player_agent_act_returns_action(self, mock_scenario):
        """PlayerAgent.act returns an Action on valid response."""
        player = mock_scenario.players[0]
        backend = MockBackend(scripted=[
            '{"action_type": "statement", "content": "I saw the culprit"}'
        ])
        
        agent = PlayerAgent(
            identity=PlayerIdentity(
                player_id=player.player_id,
                role=player.role,
                household=player.household
            ),
            role_card=player.role_card,
            scenario=mock_scenario,
            backend=backend,
            model_config={}
        )
        
        action = agent.act(
            transcript=[],
            round_info={"round": 1, "phase": "discussion"},
            must_respond_to=None
        )
        
        assert isinstance(action, Action)
        assert action.action_type == "statement"

    def test_player_agent_act_reprompts_on_garbage(self, mock_scenario):
        """PlayerAgent.act re-prompts once on garbage response."""
        player = mock_scenario.players[0]
        
        # First response is garbage, second is valid
        backend = MockBackend(scripted=[
            "garbage that cannot be parsed",
            '{"action_type": "statement", "content": "Second attempt"}',
        ])
        
        agent = PlayerAgent(
            identity=PlayerIdentity(
                player_id=player.player_id,
                role=player.role,
                household=player.household
            ),
            role_card=player.role_card,
            scenario=mock_scenario,
            backend=backend,
            model_config={}
        )
        
        action = agent.act(
            transcript=[],
            round_info={"round": 1, "phase": "discussion"},
        )
        
        # Should have succeeded on re-prompt
        assert isinstance(action, Action)
        assert action.content == "Second attempt"

    def test_player_agent_act_returns_non_compliant_after_two_failures(self, mock_scenario):
        """PlayerAgent.act returns NonCompliantAction after two failed attempts."""
        player = mock_scenario.players[0]
        
        # Both responses are garbage
        backend = MockBackend(scripted=[
            "garbage response one",
            "garbage response two",
        ])
        
        agent = PlayerAgent(
            identity=PlayerIdentity(
                player_id=player.player_id,
                role=player.role,
                household=player.household
            ),
            role_card=player.role_card,
            scenario=mock_scenario,
            backend=backend,
            model_config={}
        )
        
        result = agent.act(
            transcript=[],
            round_info={"round": 1, "phase": "discussion"},
        )
        
        assert isinstance(result, NonCompliantAction)
        assert result.action_type == "non_compliant"
        assert len(result.reason) > 0

    def test_player_agent_act_propagates_backend_unavailable_error(self, mock_scenario):
        """PlayerAgent.act propagates BackendUnavailableError."""
        player = mock_scenario.players[0]
        
        backend = MockBackend(scripted=[])  # Empty = will raise BackendUnavailableError
        
        agent = PlayerAgent(
            identity=PlayerIdentity(
                player_id=player.player_id,
                role=player.role,
                household=player.household
            ),
            role_card=player.role_card,
            scenario=mock_scenario,
            backend=backend,
            model_config={}
        )
        
        with pytest.raises(BackendUnavailableError):
            agent.act(
                transcript=[],
                round_info={"round": 1, "phase": "discussion"},
            )

    def test_player_agent_act_never_raises_on_parse_failure(self, mock_scenario):
        """PlayerAgent.act never raises on parse/validation failure."""
        player = mock_scenario.players[0]
        
        # Responses that fail validation
        backend = MockBackend(scripted=[
            '{"action_type": "question", "content": "?"}',  # Missing target for question
            '{"action_type": "statement", "content": "I am the traitor"}',  # Role reveal
        ])
        
        agent = PlayerAgent(
            identity=PlayerIdentity(
                player_id=player.player_id,
                role=player.role,
                household=player.household
            ),
            role_card=player.role_card,
            scenario=mock_scenario,
            backend=backend,
            model_config={}
        )
        
        # Should not raise; returns NonCompliantAction
        result = agent.act(
            transcript=[],
            round_info={"round": 1, "phase": "discussion"},
        )
        
        assert isinstance(result, (Action, NonCompliantAction))


class TestPlayerAgentFinalVote:
    """Test PlayerAgent.final_vote method."""

    def test_player_agent_final_vote_returns_action(self, mock_scenario):
        """PlayerAgent.final_vote returns Action on valid response."""
        player = mock_scenario.players[0]
        
        other_player = mock_scenario.players[1]
        backend = MockBackend(scripted=[
            f'{{"action_type": "final_vote", "content": "{other_player.household}"}}'
        ])
        
        agent = PlayerAgent(
            identity=PlayerIdentity(
                player_id=player.player_id,
                role=player.role,
                household=player.household
            ),
            role_card=player.role_card,
            scenario=mock_scenario,
            backend=backend,
            model_config={}
        )
        
        action = agent.final_vote(transcript=[])
        
        assert isinstance(action, Action)
        assert action.action_type == "final_vote"
        assert action.content == other_player.household

    def test_player_agent_final_vote_accepts_no_accusation(self, mock_scenario):
        """PlayerAgent.final_vote accepts 'no accusation' content."""
        player = mock_scenario.players[0]
        
        backend = MockBackend(scripted=[
            '{"action_type": "final_vote", "content": "no accusation"}'
        ])
        
        agent = PlayerAgent(
            identity=PlayerIdentity(
                player_id=player.player_id,
                role=player.role,
                household=player.household
            ),
            role_card=player.role_card,
            scenario=mock_scenario,
            backend=backend,
            model_config={}
        )
        
        action = agent.final_vote(transcript=[])
        
        assert isinstance(action, Action)
        assert action.content == "no accusation"

    def test_player_agent_final_vote_returns_non_compliant_after_two_failures(self, mock_scenario):
        """PlayerAgent.final_vote returns NonCompliantAction after two failed attempts."""
        player = mock_scenario.players[0]
        
        backend = MockBackend(scripted=[
            '{"action_type": "final_vote", "content": "InvalidPlayer"}',
            '{"action_type": "statement", "content": "Wrong action type"}',
        ])
        
        agent = PlayerAgent(
            identity=PlayerIdentity(
                player_id=player.player_id,
                role=player.role,
                household=player.household
            ),
            role_card=player.role_card,
            scenario=mock_scenario,
            backend=backend,
            model_config={}
        )
        
        result = agent.final_vote(transcript=[])
        
        assert isinstance(result, NonCompliantAction)


class TestActionTypeEnum:
    """Test that all action types are properly handled."""

    def test_all_six_action_types_parseable(self):
        """All six action types can be parsed."""
        cast_names = ["Alice", "Bob"]
        allowed_types = ["statement", "question", "challenge", "corroboration", "formal_accusation", "final_vote"]
        
        test_cases = [
            ('{"action_type": "statement", "content": "I saw something"}', "statement"),
            ('{"action_type": "question", "content": "Why?", "target": "Alice"}', "question"),
            ('{"action_type": "challenge", "content": "Disagreed"}', "challenge"),
            ('{"action_type": "corroboration", "content": "Agree", "target": "Bob"}', "corroboration"),
            ('{"action_type": "formal_accusation", "content": "Accuse", "target": "Alice", "reason": "Suspicious"}', "formal_accusation"),
            ('{"action_type": "final_vote", "content": "Alice"}', "final_vote"),
        ]
        
        for raw_text, expected_type in test_cases:
            result = parse_action(raw_text, allowed_types, cast_names)
            assert isinstance(result, Action), f"Failed to parse {expected_type}"
            assert result.action_type == expected_type


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
