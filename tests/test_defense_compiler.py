"""Unit tests for the typed defense intent DSL and deterministic compiler."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from pydantic import ValidationError

from defense_compiler import (
    DefenseCompileError,
    DefensePlanCompiler,
    load_defense_profile,
)
from defense_dsl import (
    DefenseAction,
    DefenseIntentPlan,
    DefenseIntentStep,
    DefenseState,
)
from test_manager import ActionType


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = PROJECT_ROOT / "game_profiles"
AEGIS_PACKAGE = "com.supermagic.aos.aegisdefense"


class DefenseDslValidationTests(unittest.TestCase):
    def test_action_specific_fields_and_defaults(self) -> None:
        summon = DefenseIntentStep(action="summon")
        self.assertEqual(summon.count, 1)

        speed = DefenseIntentStep(action="set_speed")
        self.assertEqual(speed.speed, "next")

        entry = DefenseIntentStep(
            action="enter_stage",
            chapter=1,
            stage=2,
            route="cheat",
        )
        self.assertEqual(
            entry.compile_params(),
            {
                "chapter": 1,
                "stage": 2,
                "route": "cheat",
            },
        )

        invalid_steps = [
            {"action": "enter_stage", "chapter": 1},
            {"action": "summon", "speed": "next"},
            {"action": "verify_state"},
            {"action": "pause", "timeout_seconds": 10},
            {"action": "start_wave", "unknown_field": True},
        ]
        for payload in invalid_steps:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    DefenseIntentStep.model_validate(payload)


class AegisProfileTests(unittest.TestCase):
    def test_loads_exact_aegis_profile(self) -> None:
        profile = load_defense_profile(AEGIS_PACKAGE, PROFILE_DIR)

        self.assertEqual(profile.version, 1)
        self.assertEqual(profile.game, "이지스 디펜스")
        self.assertEqual(profile.package, AEGIS_PACKAGE)
        self.assertEqual(profile.domain, "defense")
        self.assertEqual(
            profile.supported_actions,
            [
                DefenseAction.BOOTSTRAP_TO_LOBBY,
                DefenseAction.ENTER_STAGE,
                DefenseAction.SUMMON,
                DefenseAction.START_WAVE,
                DefenseAction.SET_SPEED,
                DefenseAction.PAUSE,
                DefenseAction.RESUME,
                DefenseAction.WAIT_FOR_STATE,
                DefenseAction.VERIFY_STATE,
                DefenseAction.CLAIM_RESULT,
            ],
        )
        self.assertEqual(
            list(profile.states),
            [
                DefenseState.LOBBY,
                DefenseState.PREP,
                DefenseState.WAVE_ACTIVE,
                DefenseState.PAUSED,
                DefenseState.VICTORY,
                DefenseState.DEFEAT,
                DefenseState.RESULT,
            ],
        )
        self.assertEqual(
            set(profile.recipes),
            {
                "bootstrap_to_lobby",
                "enter_stage_cheat",
                "enter_stage_ui",
                "summon",
                "start_wave",
                "set_speed",
                "pause",
                "resume",
                "claim_result",
            },
        )
        self.assertEqual(
            profile.options,
            {
                "default_stage": {
                    "chapter": 1,
                    "stage": 1,
                    "route": "cheat",
                },
                "speed_values": ["next"],
            },
        )
        self.assertEqual(profile.states[DefenseState.LOBBY].scene, "outgame")

    def test_bootstrap_normalizes_fresh_tutorial_and_completed_accounts(self) -> None:
        profile = load_defense_profile(AEGIS_PACKAGE, PROFILE_DIR)
        recipe = profile.recipes["bootstrap_to_lobby"]

        initial_scene_wait = next(
            step
            for step in recipe
            if step.get("action") == "wait"
            and not (step.get("params") or {}).get("run_if_flag")
        )
        self.assertEqual(
            initial_scene_wait["params"]["until_scene"],
            "ingame|outgame",
        )

        popup_index = next(
            index
            for index, step in enumerate(recipe)
            if step.get("action") == "find_and_tap"
            and "추가 데이터 다운로드" in str(step.get("target"))
        )
        wait_index = recipe.index(initial_scene_wait)
        self.assertLess(popup_index, wait_index)
        self.assertGreaterEqual(recipe[popup_index]["retry"], 2)

        tutorial_probe = next(
            step
            for step in recipe
            if (step.get("params") or {}).get("set_flag_on_pass") == "in_tutorial"
        )
        self.assertTrue(tutorial_probe["params"]["optional"])

        ingame_gated_actions = {
            step["action"]
            for step in recipe
            if (step.get("params") or {}).get("run_if_flag") == "in_tutorial"
        }
        self.assertEqual(
            ingame_gated_actions,
            {"call_cheat"},
        )

        tutorial_wait = next(
            step
            for step in recipe
            if step.get("action") == "wait"
            and step.get("description") == "튜토리얼 스킵 후 아웃게임 전환 대기"
        )
        tutorial_cleanup = next(
            step
            for step in recipe
            if step.get("action") == "repeat_until"
            and step.get("description")
            == "아웃게임 튜토리얼 안내를 따라 사용 가능한 로비까지 진행"
        )

        self.assertNotIn("run_if_flag", tutorial_wait["params"])
        self.assertNotIn("run_if_flag", tutorial_cleanup["params"])
        self.assertLess(recipe.index(tutorial_wait), recipe.index(tutorial_cleanup))

        tutorial_skip = next(
            step
            for step in recipe
            if step.get("action") == "call_cheat"
            and step.get("target") == "ingame.tutorial.skip"
        )
        self.assertNotIn("not_found_ok", tutorial_skip["params"])

        final_step = recipe[-1]
        self.assertEqual(final_step["action"], "verify")
        self.assertNotIn("scene", final_step["params"])
        self.assertIn("딤 오버레이", final_step["target"])


class DefenseCompilerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.profile = load_defense_profile(AEGIS_PACKAGE, PROFILE_DIR)
        cls.compiler = DefensePlanCompiler(cls.profile)

    def test_compile_is_deterministic_and_matches_exact_runtime_plan(self) -> None:
        semantic_plan = DefenseIntentPlan.model_validate(
            {
                "title": "state",
                "package": AEGIS_PACKAGE,
                "steps": [
                    {
                        "action": "verify_state",
                        "state": "lobby",
                    }
                ],
            }
        )

        first = self.compiler.compile(semantic_plan).model_dump(mode="json")
        second = self.compiler.compile(semantic_plan).model_dump(mode="json")

        expected = {
            "title": "state",
            "description": "",
            "package": AEGIS_PACKAGE,
            "steps": [
                {
                    "action": "verify",
                    "target": "아웃게임 스테이지 선택 화면",
                    "params": {"scene": "outgame"},
                    "description": "아웃게임 로비 상태 확인",
                    "timeout": 30,
                    "retry": 2,
                }
            ],
            "expected_results": [],
            "required_tab": None,
        }
        self.assertEqual(first, second)
        self.assertEqual(first, expected)

    def test_summon_count_expands_to_unique_repeated_runtime_steps(self) -> None:
        semantic_plan = DefenseIntentPlan.model_validate(
            {
                "title": "세 번 소환",
                "package": AEGIS_PACKAGE,
                "steps": [{"action": "summon", "count": 3}],
            }
        )

        compiled = self.compiler.compile(semantic_plan)
        self.assertEqual(len(compiled.steps), 9)
        self.assertEqual(
            [step["action"] for step in compiled.steps],
            ["read_screen", "find_and_tap", "read_text"] * 3,
        )

        for instance in range(1, 4):
            offset = (instance - 1) * 3
            before = compiled.steps[offset]
            tap = compiled.steps[offset + 1]
            after = compiled.steps[offset + 2]

            self.assertEqual(
                before["params"]["items"][0]["save_as"],
                f"credit_before_{instance}",
            )
            self.assertEqual(
                before["params"]["items"][1]["save_as"],
                f"summon_cost_{instance}",
            )
            self.assertEqual(tap["description"], f"이지스 소환 {instance}회차")
            self.assertEqual(
                after["params"]["compare_with"],
                f"credit_before_{instance}",
            )
            self.assertEqual(
                after["params"]["expect_delta_from"],
                f"summon_cost_{instance}",
            )
            self.assertEqual(
                after["params"]["save_as"],
                f"credit_after_{instance}",
            )

        self.assertNotIn(
            "{{",
            json.dumps(compiled.steps, ensure_ascii=False),
        )

    def test_compiled_plan_contains_only_existing_runtime_actions(self) -> None:
        semantic_plan = DefenseIntentPlan.model_validate(
            {
                "title": "전체 의미 액션 컴파일",
                "package": AEGIS_PACKAGE,
                "steps": [
                    {
                        "action": "bootstrap_to_lobby",
                        "timeout_seconds": 90,
                    },
                    {
                        "action": "enter_stage",
                        "chapter": 1,
                        "stage": 1,
                    },
                    {"action": "summon", "count": 1},
                    {"action": "start_wave"},
                    {"action": "set_speed", "speed": "next"},
                    {"action": "pause"},
                    {"action": "resume"},
                    {
                        "action": "wait_for_state",
                        "state": "result",
                        "timeout_seconds": 120,
                    },
                    {"action": "claim_result"},
                    {"action": "verify_state", "state": "lobby"},
                ],
            }
        )

        compiled = self.compiler.compile(semantic_plan)
        runtime_actions = {action.value for action in ActionType}
        semantic_actions = {action.value for action in DefenseAction}
        emitted_actions = {step["action"] for step in compiled.steps}

        self.assertTrue(emitted_actions)
        self.assertLessEqual(emitted_actions, runtime_actions)
        self.assertTrue(emitted_actions.isdisjoint(semantic_actions))

    def test_unsupported_plan_action_and_package_mismatch_fail(self) -> None:
        unsupported_plan = DefenseIntentPlan.model_validate(
            {
                "title": "지원하지 않는 시나리오",
                "package": AEGIS_PACKAGE,
                "supported": False,
                "unsupported_reason": "드래그 합성은 defense.v1 범위 밖입니다.",
            }
        )
        with self.assertRaisesRegex(DefenseCompileError, "드래그 합성"):
            self.compiler.compile(unsupported_plan)

        mismatched_plan = DefenseIntentPlan.model_validate(
            {
                "title": "다른 패키지",
                "package": "com.example.other",
                "steps": [{"action": "bootstrap_to_lobby"}],
            }
        )
        with self.assertRaisesRegex(DefenseCompileError, "does not match profile"):
            self.compiler.compile(mismatched_plan)

        restricted_profile = self.profile.model_copy(
            update={
                "supported_actions": [
                    action
                    for action in self.profile.supported_actions
                    if action != DefenseAction.SUMMON
                ]
            }
        )
        restricted_compiler = DefensePlanCompiler(restricted_profile)
        summon_plan = DefenseIntentPlan.model_validate(
            {
                "title": "지원되지 않는 액션",
                "package": AEGIS_PACKAGE,
                "steps": [{"action": "summon"}],
            }
        )
        with self.assertRaisesRegex(
            DefenseCompileError,
            "does not support action 'summon'",
        ):
            restricted_compiler.compile(summon_plan)

    def test_known_impossible_state_transition_fails_before_execution(self) -> None:
        invalid_order = DefenseIntentPlan.model_validate(
            {
                "title": "잘못된 상태 순서",
                "package": AEGIS_PACKAGE,
                "steps": [
                    {"action": "bootstrap_to_lobby"},
                    {"action": "pause"},
                ],
            }
        )

        with self.assertRaisesRegex(
            DefenseCompileError,
            "requires state wave_active, current state is lobby",
        ):
            self.compiler.compile(invalid_order)


if __name__ == "__main__":
    unittest.main()
