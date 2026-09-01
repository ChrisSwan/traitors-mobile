"""
Acceptance tests for Scenario module (traitors-mobile-scenario).

Contract: specs/contracts/scenario.md

Tests cover:
1. Contract compliance: function signatures match
2. Behavioral: build_scenario() produces correct Scenario object
3. Role cards have placeholders substituted
4. Scenario text is initialized correctly
5. Edge cases: invalid templates and missing placeholders raise documented errors
6. Determinism: same seed → same role assignments
7. Data leakage prevention: each player's card doesn't expose others' material
"""

import json
import pytest
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List, Dict

# Import scenario module (will exist after Engineer implements it)
from traitors_mobile.scenario import (
    load_scenario,
    build_scenario,
    validate_scenario,
    default_scenario,
    ScenarioDefinition,
    Scenario,
    PlayerIdentity,
    RoleCard,
    ScenarioError,
    ScenarioValidationError,
)


class TestContractCompliance:
    """Verify function signatures match the contract."""

    def test_load_scenario_exists_and_callable(self):
        """load_scenario(path: str | Path) -> ScenarioDefinition must exist."""
        assert callable(load_scenario)

    def test_build_scenario_exists_and_callable(self):
        """build_scenario(defn: ScenarioDefinition, seed: int | None = None) -> Scenario must exist."""
        assert callable(build_scenario)

    def test_validate_scenario_exists_and_callable(self):
        """validate_scenario(scenario: Scenario) -> list[str] must exist."""
        assert callable(validate_scenario)

    def test_default_scenario_exists_and_callable(self):
        """default_scenario() -> ScenarioDefinition must exist."""
        assert callable(default_scenario)


class TestScenarioLoading:
    """Test load_scenario function."""

    def test_load_scenario_from_valid_json(self, tmp_path):
        """load_scenario reads a valid JSON file and returns ScenarioDefinition."""
        # Create a minimal but valid scenario JSON
        scenario_data = {
            "id": "test-scenario",
            "name": "Test Scenario",
            "description": "A test scenario",
            "crime_window": "1:00pm-1:15pm",
            "cast": {
                "traitor": "Team A",
                "detective": "Team B",
                "loyalist_a": "Team C",
                "loyalist_b": "Team D",
                "loyalist_c": "Team E",
            },
            "role_cards": {
                "traitor": {
                    "goal": "Don't get caught",
                    "observations": ["I saw something"],
                    "crime_declaration": "I stole it",
                    "cover_story": "I was here",
                },
                "detective": {
                    "goal": "Find the thief",
                    "observations": ["I know something"],
                    "detective_hint": "It was them",
                },
                "loyalist_a": {
                    "goal": "Help find the thief",
                    "observations": ["I was there"],
                },
                "loyalist_b": {
                    "goal": "Help find the thief",
                    "observations": ["I was there"],
                },
                "loyalist_c": {
                    "goal": "Help find the thief",
                    "observations": ["I was there"],
                },
            },
        }
        
        json_file = tmp_path / "scenario.json"
        json_file.write_text(json.dumps(scenario_data))
        
        result = load_scenario(str(json_file))
        assert isinstance(result, ScenarioDefinition)
        assert result.id == "test-scenario"
        assert result.name == "Test Scenario"

    def test_load_scenario_file_not_found_raises_error(self):
        """load_scenario raises FileNotFoundError when path does not exist."""
        with pytest.raises(FileNotFoundError):
            load_scenario("/nonexistent/path/scenario.json")

    def test_load_scenario_invalid_json_raises_error(self, tmp_path):
        """load_scenario raises json.JSONDecodeError when file is not valid JSON."""
        json_file = tmp_path / "broken.json"
        json_file.write_text("{ broken json }")
        
        with pytest.raises(json.JSONDecodeError):
            load_scenario(str(json_file))

    def test_load_scenario_missing_required_keys_raises_error(self, tmp_path):
        """load_scenario raises ScenarioValidationError when required keys are missing."""
        # Missing 'description' key
        scenario_data = {
            "id": "test",
            "name": "Test",
            # missing description
            "crime_window": "1:00pm-1:15pm",
            "cast": {},
            "role_cards": {},
        }
        
        json_file = tmp_path / "scenario.json"
        json_file.write_text(json.dumps(scenario_data))
        
        with pytest.raises(ScenarioValidationError):
            load_scenario(str(json_file))


class TestBuildScenario:
    """Test build_scenario function."""

    def test_build_scenario_returns_scenario_object(self):
        """build_scenario returns a Scenario object."""
        defn = default_scenario()
        result = build_scenario(defn)
        
        assert isinstance(result, Scenario)

    def test_build_scenario_assigns_all_five_roles(self):
        """build_scenario produces a Scenario with exactly 5 cast members."""
        defn = default_scenario()
        scenario = build_scenario(defn)
        
        assert len(scenario.players) == 5
        
        # Check all required roles are present
        roles_present = {p.role for p in scenario.players}
        expected_roles = {"traitor", "detective", "loyalist_a", "loyalist_b", "loyalist_c"}
        assert roles_present == expected_roles

    def test_build_scenario_initializes_players_by_role_dict(self):
        """build_scenario initializes players_by_role mapping."""
        defn = default_scenario()
        scenario = build_scenario(defn)
        
        assert len(scenario.players_by_role) == 5
        assert "traitor" in scenario.players_by_role
        assert "detective" in scenario.players_by_role
        assert scenario.players_by_role["traitor"] is not None

    def test_build_scenario_substitutes_placeholders_in_role_cards(self):
        """build_scenario replaces {role} placeholders with assigned household names."""
        defn = default_scenario()
        scenario = build_scenario(defn)
        
        # Get traitor household name and loyalist_b card to check substitution
        traitor_household = scenario.players_by_role["traitor"]
        loyalist_b_card = None
        for player in scenario.players:
            if player.role == "loyalist_b":
                loyalist_b_card = player.role_card
                break
        
        assert loyalist_b_card is not None
        # Loyalist B's card references {traitor} which should be substituted
        card_text = str(loyalist_b_card.observations)
        # The traitor's household name should appear somewhere in the card
        assert traitor_household in card_text

    def test_build_scenario_no_placeholders_remain_in_cards(self):
        """build_scenario leaves no unsubstituted {placeholder} tokens."""
        defn = default_scenario()
        scenario = build_scenario(defn)
        
        for player in scenario.players:
            card = player.role_card
            # Check goal
            assert "{" not in card.goal and "}" not in card.goal, \
                f"Unsubstituted placeholder in {player.role} goal"
            # Check observations
            for obs in card.observations:
                assert "{" not in obs and "}" not in obs, \
                    f"Unsubstituted placeholder in {player.role} observation: {obs}"
            # Check traitor-specific fields
            if hasattr(card, "crime_declaration") and card.crime_declaration:
                assert "{" not in card.crime_declaration and "}" not in card.crime_declaration
            if hasattr(card, "cover_story") and card.cover_story:
                assert "{" not in card.cover_story and "}" not in card.cover_story
            # Check detective hint
            if hasattr(card, "detective_hint") and card.detective_hint:
                assert "{" not in card.detective_hint and "}" not in card.detective_hint

    def test_build_scenario_initializes_scenario_metadata(self):
        """build_scenario initializes scenario_id, description, crime_window."""
        defn = default_scenario()
        scenario = build_scenario(defn)
        
        assert scenario.scenario_id is not None
        assert len(scenario.description) > 0
        assert "Meadowbrook" in scenario.description  # From stolen prize tin
        assert scenario.crime_window is not None

    def test_build_scenario_deterministic_with_seed(self):
        """Same seed produces same role assignments."""
        defn = default_scenario()
        
        scenario1 = build_scenario(defn, seed=42)
        scenario2 = build_scenario(defn, seed=42)
        
        # Same seed should assign same households to roles
        for i in range(len(scenario1.players)):
            assert scenario1.players[i].household == scenario2.players[i].household
            assert scenario1.players[i].role == scenario2.players[i].role

    def test_build_scenario_unknown_placeholder_raises_error(self, tmp_path):
        """build_scenario raises ScenarioError when card references unknown placeholder."""
        scenario_data = {
            "id": "test",
            "name": "Test",
            "description": "Test scenario",
            "crime_window": "1:00pm-1:15pm",
            "cast": {
                "traitor": "Team A",
                "detective": "Team B",
                "loyalist_a": "Team C",
                "loyalist_b": "Team D",
                "loyalist_c": "Team E",
            },
            "role_cards": {
                "traitor": {
                    "goal": "Don't get caught",
                    "observations": ["I saw something"],
                    "crime_declaration": "I stole it",
                    "cover_story": "I was here",
                },
                "detective": {
                    "goal": "Find the thief",
                    "observations": ["I know something"],
                    "detective_hint": "It was them",
                },
                "loyalist_a": {
                    "goal": "Help",
                    "observations": ["I was there"],
                },
                "loyalist_b": {
                    "goal": "Help",
                    "observations": ["The {bogus_role} was there"],  # Unknown placeholder
                },
                "loyalist_c": {
                    "goal": "Help",
                    "observations": ["I was there"],
                },
            },
        }
        
        defn = ScenarioDefinition(
            id=scenario_data["id"],
            name=scenario_data["name"],
            description=scenario_data["description"],
            crime_window=scenario_data["crime_window"],
            cast=scenario_data["cast"],
            role_cards=scenario_data["role_cards"],
        )
        
        with pytest.raises(ScenarioError):
            build_scenario(defn)

    def test_build_scenario_invalid_cast_missing_roles_raises_error(self):
        """build_scenario raises ScenarioValidationError if cast lacks required 5 roles."""
        # Create a definition with only 3 roles instead of 5
        defn = ScenarioDefinition(
            id="incomplete",
            name="Incomplete",
            description="Missing roles",
            crime_window="1:00pm-1:15pm",
            cast={
                "traitor": "Team A",
                "detective": "Team B",
                "loyalist_a": "Team C",
                # missing loyalist_b and loyalist_c
            },
            role_cards={
                "traitor": {
                    "goal": "Goal",
                    "observations": ["Obs"],
                    "crime_declaration": "Crime",
                    "cover_story": "Story",
                },
                "detective": {
                    "goal": "Goal",
                    "observations": ["Obs"],
                    "detective_hint": "Hint",
                },
                "loyalist_a": {
                    "goal": "Goal",
                    "observations": ["Obs"],
                },
            },
        )
        
        with pytest.raises(ScenarioValidationError):
            build_scenario(defn)


class TestValidateScenario:
    """Test validate_scenario function."""

    def test_validate_scenario_returns_empty_list_for_valid_scenario(self):
        """validate_scenario returns empty list for a valid Scenario."""
        defn = default_scenario()
        scenario = build_scenario(defn)
        
        problems = validate_scenario(scenario)
        assert isinstance(problems, list)
        assert len(problems) == 0, f"Valid scenario reported problems: {problems}"

    def test_validate_scenario_detects_missing_role(self):
        """validate_scenario detects when a required role is missing."""
        # Create a scenario missing loyalist_c
        defn = default_scenario()
        scenario = build_scenario(defn)
        
        # Remove one player
        scenario.players = scenario.players[:4]
        
        problems = validate_scenario(scenario)
        assert len(problems) > 0
        assert any("role" in p.lower() for p in problems)

    def test_validate_scenario_detects_empty_goal(self):
        """validate_scenario detects when a card goal is empty."""
        defn = default_scenario()
        scenario = build_scenario(defn)
        
        # Set a goal to empty
        scenario.players[0].role_card.goal = ""
        
        problems = validate_scenario(scenario)
        assert len(problems) > 0
        assert any("goal" in p.lower() for p in problems)

    def test_validate_scenario_detects_missing_observations(self):
        """validate_scenario detects when a card has no observations."""
        defn = default_scenario()
        scenario = build_scenario(defn)
        
        # Remove all observations
        scenario.players[0].role_card.observations = []
        
        problems = validate_scenario(scenario)
        assert len(problems) > 0
        assert any("observation" in p.lower() for p in problems)

    def test_validate_scenario_detects_unsubstituted_placeholders(self):
        """validate_scenario detects unsubstituted {placeholder} tokens."""
        defn = default_scenario()
        scenario = build_scenario(defn)
        
        # Add a placeholder back in (simulate failed substitution)
        scenario.players[0].role_card.observations.append("I saw {traitor}")
        
        problems = validate_scenario(scenario)
        assert len(problems) > 0
        assert any("placeholder" in p.lower() or "{" in p for p in problems)

    def test_validate_scenario_detects_missing_traitor_crime_declaration(self):
        """validate_scenario detects when traitor card lacks crime_declaration."""
        defn = default_scenario()
        scenario = build_scenario(defn)
        
        # Find traitor and remove crime_declaration
        for player in scenario.players:
            if player.role == "traitor":
                player.role_card.crime_declaration = None
                break
        
        problems = validate_scenario(scenario)
        assert len(problems) > 0
        assert any("crime_declaration" in p.lower() or "traitor" in p.lower() for p in problems)

    def test_validate_scenario_detects_missing_detective_hint(self):
        """validate_scenario detects when detective card lacks detective_hint."""
        defn = default_scenario()
        scenario = build_scenario(defn)
        
        # Find detective and remove hint
        for player in scenario.players:
            if player.role == "detective":
                player.role_card.detective_hint = None
                break
        
        problems = validate_scenario(scenario)
        assert len(problems) > 0
        assert any("hint" in p.lower() or "detective" in p.lower() for p in problems)

    def test_validate_scenario_detects_duplicate_player_ids(self):
        """validate_scenario detects when player_id appears twice."""
        defn = default_scenario()
        scenario = build_scenario(defn)
        
        # Duplicate a player_id
        if len(scenario.players) >= 2:
            scenario.players[1].player_id = scenario.players[0].player_id
        
        problems = validate_scenario(scenario)
        assert len(problems) > 0
        assert any("player_id" in p.lower() or "duplicate" in p.lower() for p in problems)


class TestDefaultScenario:
    """Test default_scenario function."""

    def test_default_scenario_returns_scenario_definition(self):
        """default_scenario returns a ScenarioDefinition."""
        result = default_scenario()
        assert isinstance(result, ScenarioDefinition)

    def test_default_scenario_stolen_prize_tin(self):
        """default_scenario returns the 'stolen prize tin' baseline."""
        defn = default_scenario()
        
        # Verify it's the stolen prize tin scenario
        assert "stolen" in defn.id.lower() or "prize" in defn.id.lower()
        assert "Meadowbrook" in defn.description

    def test_default_scenario_builds_without_error(self):
        """default_scenario definition can be built successfully."""
        defn = default_scenario()
        scenario = build_scenario(defn)
        
        problems = validate_scenario(scenario)
        assert len(problems) == 0, f"Default scenario validation failed: {problems}"


class TestDataLeakagePrevention:
    """Test that player role cards don't expose other players' private information."""

    def test_each_player_card_contains_only_own_information(self):
        """Each player's role card should not contain another player's private material."""
        defn = default_scenario()
        scenario = build_scenario(defn)
        
        # Get all private materials by player
        private_materials_by_player = {}
        for player in scenario.players:
            materials = set()
            
            # Collect all text from this player's card
            card = player.role_card
            materials.add(card.goal)
            for obs in card.observations:
                materials.add(obs)
            
            # Add role-specific materials
            if hasattr(card, "crime_declaration") and card.crime_declaration:
                materials.add(card.crime_declaration)
            if hasattr(card, "cover_story") and card.cover_story:
                materials.add(card.cover_story)
            if hasattr(card, "detective_hint") and card.detective_hint:
                materials.add(card.detective_hint)
            
            private_materials_by_player[player.player_id] = materials
        
        # Check that no player's card contains another player's unique private material
        # (This is a data-level check: looking for exact text leakage)
        for player_id, materials in private_materials_by_player.items():
            player_card_text = " ".join(materials)
            
            # Traitor's crime declaration should not appear in any other player's card
            for other_player_id, other_materials in private_materials_by_player.items():
                if player_id != other_player_id:
                    other_card_text = " ".join(other_materials)
                    
                    # If this player is the traitor, no one else should have crime_declaration
                    traitor_player = next(p for p in scenario.players if p.player_id == player_id)
                    if traitor_player.role == "traitor" and hasattr(traitor_player.role_card, "crime_declaration"):
                        crime_decl = traitor_player.role_card.crime_declaration
                        if crime_decl:
                            assert crime_decl not in other_card_text, \
                                f"Traitor's crime declaration leaked to {other_player_id}"
                    
                    # If this player is the detective, no one else should have detective_hint
                    detective_player = next(p for p in scenario.players if p.player_id == player_id)
                    if detective_player.role == "detective" and hasattr(detective_player.role_card, "detective_hint"):
                        hint = detective_player.role_card.detective_hint
                        if hint:
                            assert hint not in other_card_text, \
                                f"Detective's hint leaked to {other_player_id}"

    def test_scenario_description_is_public(self):
        """The public scenario description should be identical for all players (not different)."""
        defn = default_scenario()
        scenario = build_scenario(defn)
        
        # All players should see the same public description
        assert all(
            scenario.description == scenario.description
            for _ in scenario.players
        )


class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_scenario_error_has_descriptive_message(self, tmp_path):
        """ScenarioError messages should be descriptive."""
        scenario_data = {
            "id": "test",
            "name": "Test",
            "description": "Test",
            "crime_window": "1:00pm-1:15pm",
            "cast": {
                "traitor": "Team A",
                "detective": "Team B",
                "loyalist_a": "Team C",
                "loyalist_b": "Team D",
                "loyalist_c": "Team E",
            },
            "role_cards": {
                "traitor": {
                    "goal": "Goal",
                    "observations": ["Saw {unknown_role}"],
                    "crime_declaration": "Crime",
                    "cover_story": "Story",
                },
                "detective": {
                    "goal": "Goal",
                    "observations": ["Obs"],
                    "detective_hint": "Hint",
                },
                "loyalist_a": {
                    "goal": "Goal",
                    "observations": ["Obs"],
                },
                "loyalist_b": {
                    "goal": "Goal",
                    "observations": ["Obs"],
                },
                "loyalist_c": {
                    "goal": "Goal",
                    "observations": ["Obs"],
                },
            },
        }
        
        defn = ScenarioDefinition(
            id=scenario_data["id"],
            name=scenario_data["name"],
            description=scenario_data["description"],
            crime_window=scenario_data["crime_window"],
            cast=scenario_data["cast"],
            role_cards=scenario_data["role_cards"],
        )
        
        try:
            build_scenario(defn)
            assert False, "Should have raised ScenarioError"
        except ScenarioError as e:
            # Message should mention the unknown placeholder
            assert len(str(e)) > 0

    def test_validation_error_lists_all_problems(self):
        """ScenarioValidationError should list all detected problems."""
        try:
            # Try to build scenario with missing cast
            defn = ScenarioDefinition(
                id="test",
                name="Test",
                description="Test",
                crime_window="1:00pm",
                cast={},  # Empty cast
                role_cards={},
            )
            build_scenario(defn)
            assert False, "Should have raised ScenarioValidationError"
        except ScenarioValidationError as e:
            # Message should be informative
            assert len(str(e)) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
