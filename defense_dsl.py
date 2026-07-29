"""Typed semantic DSL for simple defense-game QA scenarios.

The DSL deliberately describes *what* to do, not where to tap.  A game profile
and the deterministic compiler translate these steps into the existing
``TestStep`` actions consumed by ``QAOrchestrator``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DefenseAction(str, Enum):
    """Small, intentionally bounded set of defense-game intentions."""

    BOOTSTRAP_TO_LOBBY = "bootstrap_to_lobby"
    ENTER_STAGE = "enter_stage"
    SUMMON = "summon"
    START_WAVE = "start_wave"
    SET_SPEED = "set_speed"
    PAUSE = "pause"
    RESUME = "resume"
    WAIT_FOR_STATE = "wait_for_state"
    VERIFY_STATE = "verify_state"
    CLAIM_RESULT = "claim_result"


class DefenseState(str, Enum):
    """States shared by the first defense-game MVP."""

    LOBBY = "lobby"
    PREP = "prep"
    WAVE_ACTIVE = "wave_active"
    PAUSED = "paused"
    VICTORY = "victory"
    DEFEAT = "defeat"
    RESULT = "result"


class StageEntryRoute(str, Enum):
    """How to enter a stage.

    ``cheat`` is for deterministic setup. ``ui`` is used when the entry flow
    itself, energy spending, or reward eligibility is under test.
    """

    CHEAT = "cheat"
    UI = "ui"


class DefenseIntentStep(BaseModel):
    """One semantic step produced by the natural-language planner.

    A flat model is used instead of a deeply nested union so it remains easy to
    express as a provider-supported JSON schema.  The model validator enforces
    the action-specific contract.
    """

    model_config = ConfigDict(extra="forbid")

    action: DefenseAction
    chapter: int | None = Field(default=None, ge=1, le=999)
    stage: int | None = Field(default=None, ge=1, le=999)
    route: StageEntryRoute | None = None
    count: int | None = Field(default=None, ge=1, le=20)
    speed: str | None = Field(default=None, max_length=30)
    state: DefenseState | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=1800)

    @model_validator(mode="after")
    def validate_action_fields(self) -> "DefenseIntentStep":
        if self.action == DefenseAction.ENTER_STAGE:
            if self.chapter is None or self.stage is None:
                raise ValueError("enter_stage requires chapter and stage")
        elif self.chapter is not None or self.stage is not None or self.route is not None:
            raise ValueError(
                "chapter, stage, and route are only valid for enter_stage"
            )

        if self.action == DefenseAction.SUMMON:
            if self.count is None:
                self.count = 1
        elif self.count is not None:
            raise ValueError("count is only valid for summon")

        if self.action == DefenseAction.SET_SPEED:
            if not (self.speed or "").strip():
                self.speed = "next"
        elif self.speed is not None:
            raise ValueError("speed is only valid for set_speed")

        if self.action in {
            DefenseAction.WAIT_FOR_STATE,
            DefenseAction.VERIFY_STATE,
        }:
            if self.state is None:
                raise ValueError(f"{self.action.value} requires state")
        elif self.state is not None:
            raise ValueError(
                "state is only valid for wait_for_state or verify_state"
            )

        if self.timeout_seconds is not None and self.action not in {
            DefenseAction.BOOTSTRAP_TO_LOBBY,
            DefenseAction.ENTER_STAGE,
            DefenseAction.WAIT_FOR_STATE,
        }:
            raise ValueError(
                "timeout_seconds is only valid for bootstrap_to_lobby, "
                "enter_stage, or wait_for_state"
            )
        return self

    def compile_params(self) -> dict[str, Any]:
        """Return non-null parameters for deterministic profile rendering."""

        data = self.model_dump(mode="json", exclude_none=True)
        data.pop("action", None)
        return data


_REQUIRED_STATES: dict[DefenseAction, frozenset[DefenseState]] = {
    DefenseAction.ENTER_STAGE: frozenset({DefenseState.LOBBY}),
    DefenseAction.SUMMON: frozenset({DefenseState.PREP}),
    DefenseAction.START_WAVE: frozenset({DefenseState.PREP}),
    DefenseAction.SET_SPEED: frozenset({DefenseState.WAVE_ACTIVE}),
    DefenseAction.PAUSE: frozenset({DefenseState.WAVE_ACTIVE}),
    DefenseAction.RESUME: frozenset({DefenseState.PAUSED}),
    DefenseAction.CLAIM_RESULT: frozenset(
        {
            DefenseState.RESULT,
            DefenseState.VICTORY,
            DefenseState.DEFEAT,
        }
    ),
}

_POST_STATES: dict[DefenseAction, DefenseState] = {
    DefenseAction.BOOTSTRAP_TO_LOBBY: DefenseState.LOBBY,
    DefenseAction.ENTER_STAGE: DefenseState.PREP,
    DefenseAction.START_WAVE: DefenseState.WAVE_ACTIVE,
    DefenseAction.PAUSE: DefenseState.PAUSED,
    DefenseAction.RESUME: DefenseState.WAVE_ACTIVE,
    DefenseAction.CLAIM_RESULT: DefenseState.LOBBY,
}


def required_defense_states(
    action: DefenseAction,
) -> frozenset[DefenseState] | None:
    """Return the known precondition states for one semantic action."""

    return _REQUIRED_STATES.get(action)


def known_defense_state_after(
    step: DefenseIntentStep,
    current: DefenseState | None,
) -> DefenseState | None:
    """Advance the shared Planner/Compiler state model."""

    if step.action in {
        DefenseAction.WAIT_FOR_STATE,
        DefenseAction.VERIFY_STATE,
    }:
        return step.state
    return _POST_STATES.get(step.action, current)


class DefenseIntentPlan(BaseModel):
    """Structured output contract for the intent compiler."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    description: str = ""
    package: str = ""
    supported: bool = True
    unsupported_reason: str = ""
    steps: list[DefenseIntentStep] = Field(default_factory=list, max_length=50)
    expected_results: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def validate_supported_plan(self) -> "DefenseIntentPlan":
        if self.supported and not self.steps:
            raise ValueError("supported defense plans require at least one step")
        if not self.supported and not self.unsupported_reason.strip():
            raise ValueError("unsupported plans require unsupported_reason")
        return self
