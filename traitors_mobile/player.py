"""
traitors-mobile-player (Module 2)

LLM-backed player agent module. This is a stub that defines the interface
so acceptance tests can be imported and executed.

Contract: specs/contracts/player.md (SWA-146).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union


# --------------------------------------------------------------------------
# Data Types
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


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------


class PromptError(Exception):
    """Raised when prompt construction fails (missing role card, scenario, etc.)."""
    pass


# --------------------------------------------------------------------------
# Functions and Classes (stubs to be implemented)
# --------------------------------------------------------------------------


def build_player_prompt(
    state: Dict[str, Any],
    transcript: List[Dict[str, Any]],
    round_info: Dict[str, Any],
    must_respond_to: Optional[str] = None,
) -> List[Dict[str, str]]:
    """
    Build the messages list for the LLM backend.
    
    Returns: list of {role, content} dicts for the LLM backend.
    Raises: PromptError if state lacks role_card or scenario.
    """
    raise NotImplementedError("build_player_prompt not yet implemented")


def assert_prompt_isolated(
    prompt_text: str,
    private_materials_by_player: Dict[str, set],
    player_id: str,
) -> List[str]:
    """
    Check whether the prompt contains other players' private materials.
    
    Returns: list of violation descriptions (empty = isolated).
    """
    raise NotImplementedError("assert_prompt_isolated not yet implemented")


def parse_action(
    raw_text: str,
    allowed_types: List[str],
    cast_names: List[str],
) -> Union[Action, ParseFailure]:
    """
    Parse the model's reply into an Action.
    
    Returns: Action on success, ParseFailure on structure errors.
    """
    raise NotImplementedError("parse_action not yet implemented")


def validate_action(
    action: Action,
    cast_names: List[str],
    rules: Dict[str, Any],
) -> List[str]:
    """
    Validate the action against game rules.
    
    Returns: list of problems (empty = valid).
    """
    raise NotImplementedError("validate_action not yet implemented")


class PlayerAgent:
    """LLM-backed player agent."""

    def __init__(
        self,
        identity,
        role_card,
        scenario,
        backend,
        model_config: Dict[str, Any],
    ):
        """
        Initialize a PlayerAgent.
        
        Args:
            identity: PlayerIdentity object
            role_card: RoleCard object
            scenario: Scenario object
            backend: LLMBackend instance
            model_config: Configuration dict
        """
        raise NotImplementedError("PlayerAgent.__init__ not yet implemented")

    def act(
        self,
        transcript: List[Dict[str, Any]],
        round_info: Dict[str, Any],
        must_respond_to: Optional[str] = None,
    ) -> Union[Action, NonCompliantAction]:
        """
        Generate an action for this player.
        
        Returns: Action or NonCompliantAction.
        Raises: PromptError, BackendError subclasses.
        """
        raise NotImplementedError("PlayerAgent.act not yet implemented")

    def final_vote(
        self,
        transcript: List[Dict[str, Any]],
    ) -> Union[Action, NonCompliantAction]:
        """
        Generate a final vote for this player.
        
        Returns: Action (final_vote) or NonCompliantAction.
        Raises: PromptError, BackendError subclasses.
        """
        raise NotImplementedError("PlayerAgent.final_vote not yet implemented")
