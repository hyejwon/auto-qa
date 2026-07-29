"""Game-profile loader and deterministic defense-plan compiler."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from defense_dsl import (
    DefenseAction,
    DefenseIntentPlan,
    DefenseIntentStep,
    DefenseState,
    StageEntryRoute,
    known_defense_state_after,
    required_defense_states,
)
from test_manager import ActionType, TestStep


DEFAULT_PROFILE_DIR = Path(__file__).resolve().parent / "game_profiles"
_FULL_PLACEHOLDER = re.compile(r"^\{\{\s*([a-zA-Z0-9_]+)\s*\}\}$")
_ANY_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


class DefenseProfileError(ValueError):
    """A game profile is missing, malformed, or internally inconsistent."""


class DefenseCompileError(ValueError):
    """A semantic defense plan cannot be compiled safely."""


class ProfileState(BaseModel):
    """How the runtime can identify one semantic game state."""

    model_config = ConfigDict(extra="forbid")

    scene: str | None = None
    target: str | None = None
    description: str = ""

    @model_validator(mode="after")
    def require_signal(self) -> "ProfileState":
        if not (self.scene or "").strip() and not (self.target or "").strip():
            raise ValueError("a profile state requires scene or target")
        return self


class DefenseGameProfile(BaseModel):
    """Declarative, game-specific mapping from intentions to runtime steps."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1)
    game: str = Field(min_length=1)
    package: str = Field(min_length=1)
    domain: Literal["defense"] = "defense"
    intent_keywords: list[str] = Field(default_factory=list)
    supported_actions: list[DefenseAction]
    states: dict[DefenseState, ProfileState]
    recipes: dict[str, list[dict[str, Any]]]
    options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_recipes(self) -> "DefenseGameProfile":
        direct_recipe_actions = set(self.supported_actions) - {
            DefenseAction.ENTER_STAGE,
            DefenseAction.WAIT_FOR_STATE,
            DefenseAction.VERIFY_STATE,
        }
        missing = [
            action.value
            for action in direct_recipe_actions
            if action.value not in self.recipes
        ]
        if DefenseAction.ENTER_STAGE in self.supported_actions:
            for route in StageEntryRoute:
                key = f"enter_stage_{route.value}"
                if key not in self.recipes:
                    missing.append(key)
        if missing:
            raise ValueError(f"missing recipes: {', '.join(sorted(missing))}")

        default_stage = self.options.get("default_stage")
        if default_stage is not None:
            if not isinstance(default_stage, dict):
                raise ValueError("options.default_stage must be an object")
            if "action" in default_stage:
                raise ValueError(
                    "options.default_stage must not contain action"
                )
            try:
                DefenseIntentStep.model_validate(
                    {
                        "action": DefenseAction.ENTER_STAGE,
                        **default_stage,
                    }
                )
            except ValidationError as exc:
                raise ValueError(
                    f"invalid options.default_stage: {exc}"
                ) from exc
        return self

    def planner_context(self) -> str:
        """Compact profile description for the intent-only LLM prompt."""

        payload = {
            "game": self.game,
            "package": self.package,
            "allowed_actions": [action.value for action in self.supported_actions],
            "states": {
                state.value: spec.description or spec.target or spec.scene
                for state, spec in self.states.items()
            },
            "entry_routes": [route.value for route in StageEntryRoute],
            "options": self.options,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)


class CompiledDefensePlan(BaseModel):
    """Low-level plan that is compatible with the existing runner."""

    model_config = ConfigDict(extra="forbid")

    title: str
    description: str = ""
    package: str
    steps: list[dict[str, Any]]
    expected_results: list[str] = Field(default_factory=list)
    required_tab: str | None = None


def load_defense_profile(
    package: str,
    profile_dir: Path | None = None,
) -> DefenseGameProfile:
    """Load the exact profile for ``package``.

    Profiles are scanned rather than inferred from filenames so package is the
    only source of truth.
    """

    wanted = package.strip()
    if not wanted:
        raise DefenseProfileError("defense planner requires a package")

    root = profile_dir or DEFAULT_PROFILE_DIR
    if not root.exists():
        raise DefenseProfileError(f"game profile directory not found: {root}")

    malformed: list[str] = []
    for path in sorted(root.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            profile = DefenseGameProfile.model_validate(raw)
        except (OSError, yaml.YAMLError, ValidationError, ValueError) as exc:
            malformed.append(f"{path.name}: {exc}")
            continue
        if profile.package == wanted:
            return profile

    if malformed:
        detail = "; ".join(malformed[:3])
        raise DefenseProfileError(
            f"no valid defense profile for package '{wanted}' ({detail})"
        )
    raise DefenseProfileError(f"no defense profile for package '{wanted}'")


def has_defense_profile(package: str, profile_dir: Path | None = None) -> bool:
    try:
        load_defense_profile(package, profile_dir)
        return True
    except DefenseProfileError:
        return False


def _render(value: Any, context: dict[str, Any]) -> Any:
    """Recursively render ``{{name}}`` placeholders, preserving scalar types."""

    if isinstance(value, str):
        full = _FULL_PLACEHOLDER.match(value)
        if full:
            key = full.group(1)
            if key not in context:
                raise DefenseCompileError(f"unknown profile placeholder: {key}")
            return copy.deepcopy(context[key])

        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in context:
                raise DefenseCompileError(f"unknown profile placeholder: {key}")
            return str(context[key])

        return _ANY_PLACEHOLDER.sub(replace, value)
    if isinstance(value, list):
        return [_render(item, context) for item in value]
    if isinstance(value, dict):
        return {key: _render(item, context) for key, item in value.items()}
    return copy.deepcopy(value)


_MUTATING_ACTIONS = {
    DefenseAction.BOOTSTRAP_TO_LOBBY,
    DefenseAction.ENTER_STAGE,
    DefenseAction.SUMMON,
    DefenseAction.START_WAVE,
    DefenseAction.SET_SPEED,
    DefenseAction.PAUSE,
    DefenseAction.RESUME,
    DefenseAction.CLAIM_RESULT,
}

_ASSERTION_ACTIONS = {
    ActionType.VERIFY.value,
    ActionType.READ_TEXT.value,
    ActionType.READ_ITEMS.value,
    ActionType.READ_SCREEN.value,
    ActionType.CHECK_PROPERTY.value,
}


def _has_postcondition(steps: list[dict[str, Any]]) -> bool:
    for step in steps:
        action = str(step.get("action") or "")
        params = step.get("params") or {}
        if action in _ASSERTION_ACTIONS:
            return True
        if action == ActionType.FIND_AND_TAP.value and (
            params.get("expect_visible") or params.get("expect_hidden")
        ):
            return True
        if action == ActionType.WAIT.value and any(
            params.get(key)
            for key in (
                "until_scene",
                "until_visible",
                "until_hidden",
                "until_property",
                "until_unity_button",
                "until_unity_button_hidden",
            )
        ):
            return True
    return False


class DefensePlanCompiler:
    """Compile a semantic plan into validated, existing ``TestStep`` actions."""

    def __init__(self, profile: DefenseGameProfile):
        self.profile = profile

    def compile(self, plan: DefenseIntentPlan) -> CompiledDefensePlan:
        if not plan.supported:
            raise DefenseCompileError(
                plan.unsupported_reason or "scenario is not supported by defense DSL"
            )
        if plan.package and plan.package != self.profile.package:
            raise DefenseCompileError(
                f"plan package '{plan.package}' does not match profile "
                f"'{self.profile.package}'"
            )

        compiled: list[dict[str, Any]] = []
        instance_counter = 0
        current_state: DefenseState | None = None
        for semantic_index, step in enumerate(plan.steps, start=1):
            if step.action not in self.profile.supported_actions:
                raise DefenseCompileError(
                    f"profile does not support action '{step.action.value}'"
                )
            current_state = self._next_state(
                step,
                current_state,
                semantic_index=semantic_index,
            )

            repeats = step.count or 1
            if step.action != DefenseAction.SUMMON:
                repeats = 1

            for repeat_index in range(1, repeats + 1):
                instance_counter += 1
                expanded = self._expand_step(
                    step,
                    semantic_index=semantic_index,
                    repeat_index=repeat_index,
                    instance=instance_counter,
                )
                if step.action in _MUTATING_ACTIONS and not _has_postcondition(expanded):
                    raise DefenseCompileError(
                        f"profile recipe '{step.action.value}' has no postcondition"
                    )
                compiled.extend(self._validate_low_level_steps(expanded))

        if not compiled:
            raise DefenseCompileError("compiled defense plan has no runtime steps")

        return CompiledDefensePlan(
            title=plan.title,
            description=plan.description,
            package=self.profile.package,
            steps=compiled,
            expected_results=plan.expected_results,
        )

    @staticmethod
    def _next_state(
        step: DefenseIntentStep,
        current: DefenseState | None,
        *,
        semantic_index: int,
    ) -> DefenseState | None:
        """Reject impossible known-state transitions without a runtime Agent."""

        allowed = required_defense_states(step.action)
        if current is not None and allowed is not None and current not in allowed:
            expected = "|".join(sorted(state.value for state in allowed))
            raise DefenseCompileError(
                f"semantic step {semantic_index} '{step.action.value}' requires "
                f"state {expected}, current state is {current.value}"
            )

        return known_defense_state_after(step, current)

    def _expand_step(
        self,
        step: DefenseIntentStep,
        *,
        semantic_index: int,
        repeat_index: int,
        instance: int,
    ) -> list[dict[str, Any]]:
        if step.action == DefenseAction.WAIT_FOR_STATE:
            return [self._state_step(step, wait=True)]
        if step.action == DefenseAction.VERIFY_STATE:
            return [self._state_step(step, wait=False)]

        if step.action == DefenseAction.ENTER_STAGE:
            route = step.route or StageEntryRoute.CHEAT
            recipe_name = f"enter_stage_{route.value}"
        else:
            recipe_name = step.action.value

        recipe = self.profile.recipes.get(recipe_name)
        if not recipe:
            raise DefenseCompileError(f"profile recipe not found: {recipe_name}")

        if step.action == DefenseAction.SET_SPEED:
            allowed = self.profile.options.get("speed_values")
            if isinstance(allowed, list) and step.speed not in allowed:
                raise DefenseCompileError(
                    f"unsupported speed '{step.speed}', allowed={allowed}"
                )

        context: dict[str, Any] = {
            "package": self.profile.package,
            "semantic_index": semantic_index,
            "repeat_index": repeat_index,
            "instance": instance,
            "chapter": step.chapter,
            "stage": step.stage,
            "route": (step.route or StageEntryRoute.CHEAT).value,
            "count": step.count or 1,
            "speed": step.speed or "next",
            "state": step.state.value if step.state else "",
            "timeout_seconds": step.timeout_seconds or 120,
        }
        return _render(recipe, context)

    def _state_step(
        self,
        step: DefenseIntentStep,
        *,
        wait: bool,
    ) -> dict[str, Any]:
        if step.state is None:
            raise DefenseCompileError(f"{step.action.value} requires state")
        spec = self.profile.states.get(step.state)
        if not spec:
            raise DefenseCompileError(
                f"profile has no detector for state '{step.state.value}'"
            )

        label = spec.description or step.state.value
        if wait:
            params: dict[str, Any] = {
                "poll_interval_seconds": 1.0,
            }
            if spec.scene:
                params["until_scene"] = spec.scene
            else:
                params["until_visible"] = spec.target
            return {
                "action": ActionType.WAIT.value,
                "target": spec.target,
                "description": f"{label} 상태가 될 때까지 대기",
                "timeout": step.timeout_seconds or 120,
                "retry": 1,
                "params": params,
            }

        if spec.scene:
            return {
                "action": ActionType.VERIFY.value,
                "target": spec.target or f"{label} (씬 판정)",
                "description": f"{label} 상태 확인",
                "timeout": step.timeout_seconds or 30,
                "retry": 2,
                "params": {"scene": spec.scene},
            }
        return {
            "action": ActionType.VERIFY.value,
            "target": spec.target,
            "description": f"{label} 상태 확인",
            "timeout": step.timeout_seconds or 30,
            "retry": 2,
            "params": {},
        }

    @staticmethod
    def _validate_low_level_steps(
        steps: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        validated: list[dict[str, Any]] = []
        for index, step in enumerate(steps, start=1):
            try:
                parsed = TestStep.model_validate(step)
            except ValidationError as exc:
                raise DefenseCompileError(
                    f"compiled runtime step {index} is invalid: {exc}"
                ) from exc
            validated.append(parsed.model_dump(mode="json"))
        return validated
