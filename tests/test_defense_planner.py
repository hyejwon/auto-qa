"""Planner boundary tests without a network model call."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from defense_compiler import DefensePlanCompiler, load_defense_profile
from planner_node import PlannerNode, PlannerResponseFormatError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AEGIS_PACKAGE = "com.supermagic.aos.aegisdefense"


class _FakeModels:
    def __init__(self, parsed):
        self.parsed = parsed
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(parsed=self.parsed, text="")


class DefensePlannerBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = load_defense_profile(
            AEGIS_PACKAGE,
            PROJECT_ROOT / "game_profiles",
        )

    @staticmethod
    def _planner_with_response(parsed):
        planner = object.__new__(PlannerNode)
        planner.model = "intent-test-model"
        models = _FakeModels(parsed)
        planner.client = SimpleNamespace(models=models)
        return planner, models

    def test_structured_semantic_output_is_accepted_and_package_is_pinned(self):
        planner, models = self._planner_with_response(
            {
                "title": "소환",
                "package": "com.model.tried.to.change",
                "steps": [
                    {"action": "enter_stage", "chapter": 1, "stage": 1},
                    {"action": "summon", "count": 2},
                ],
            }
        )

        plan = planner.create_defense_plan(
            "1-1에 들어가 두 번 소환해",
            AEGIS_PACKAGE,
            self.profile,
        )

        self.assertEqual(plan.package, AEGIS_PACKAGE)
        self.assertEqual([step.action.value for step in plan.steps], [
            "enter_stage",
            "summon",
        ])
        self.assertEqual(models.calls[0]["model"], "intent-test-model")
        config = models.calls[0]["config"]
        self.assertEqual(config.response_mime_type, "application/json")
        self.assertIsNotNone(config.response_schema)
        provider_schema = config.response_schema.model_dump(
            by_alias=True,
            exclude_none=True,
        )

        def contains_key(value, key):
            if isinstance(value, dict):
                return key in value or any(
                    contains_key(item, key) for item in value.values()
                )
            if isinstance(value, list):
                return any(contains_key(item, key) for item in value)
            return False

        self.assertFalse(
            contains_key(provider_schema, "additionalProperties")
        )

    def test_low_level_action_is_rejected_at_planner_boundary(self):
        planner, _models = self._planner_with_response(
            {
                "title": "잘못된 출력",
                "steps": [{"action": "find_and_tap"}],
            }
        )

        with self.assertRaises(PlannerResponseFormatError):
            planner.create_defense_plan(
                "소환 버튼을 눌러",
                AEGIS_PACKAGE,
                self.profile,
            )

    def test_missing_lobby_to_prep_transition_uses_profile_default_stage(self):
        planner, _models = self._planner_with_response(
            {
                "title": "이지스 소환 및 2웨이브 진행",
                "steps": [
                    {"action": "bootstrap_to_lobby"},
                    {"action": "summon", "count": 2},
                    {"action": "start_wave"},
                ],
            }
        )

        plan = planner.create_defense_plan(
            "앱을 초기화하고 이지스를 2회 소환한 뒤 웨이브를 시작해",
            AEGIS_PACKAGE,
            self.profile,
        )

        self.assertEqual(
            [step.action.value for step in plan.steps],
            [
                "bootstrap_to_lobby",
                "enter_stage",
                "summon",
                "start_wave",
            ],
        )
        inserted = plan.steps[1]
        self.assertEqual((inserted.chapter, inserted.stage), (1, 1))
        self.assertEqual(inserted.route.value, "cheat")
        DefensePlanCompiler(self.profile).compile(plan)

    def test_missing_transition_uses_explicit_stage_from_scenario(self):
        planner, _models = self._planner_with_response(
            {
                "title": "2-3 소환",
                "steps": [
                    {"action": "bootstrap_to_lobby"},
                    {"action": "summon"},
                ],
            }
        )

        plan = planner.create_defense_plan(
            "초기화한 뒤 2-3에서 이지스를 소환해",
            AEGIS_PACKAGE,
            self.profile,
        )

        inserted = plan.steps[1]
        self.assertEqual(inserted.action.value, "enter_stage")
        self.assertEqual((inserted.chapter, inserted.stage), (2, 3))

    def test_summon_count_range_is_not_mistaken_for_stage(self):
        planner, _models = self._planner_with_response(
            {
                "title": "여러 번 소환",
                "steps": [
                    {"action": "bootstrap_to_lobby"},
                    {"action": "summon", "count": 3},
                ],
            }
        )

        plan = planner.create_defense_plan(
            "초기화하고 이지스를 3-4회 정도 소환할 수 있는지 확인해",
            AEGIS_PACKAGE,
            self.profile,
        )

        inserted = plan.steps[1]
        self.assertEqual((inserted.chapter, inserted.stage), (1, 1))

    def test_repeated_wave_inserts_wait_for_next_prep_state(self):
        planner, _models = self._planner_with_response(
            {
                "title": "2웨이브 진행",
                "steps": [
                    {"action": "bootstrap_to_lobby"},
                    {
                        "action": "enter_stage",
                        "chapter": 1,
                        "stage": 1,
                    },
                    {"action": "summon", "count": 2},
                    {"action": "start_wave"},
                    {
                        "action": "wait_for_state",
                        "state": "wave_active",
                    },
                    {"action": "start_wave"},
                ],
            }
        )

        plan = planner.create_defense_plan(
            "앱을 초기화하고 이지스를 2회 소환한 뒤 2웨이브까지 진행해",
            AEGIS_PACKAGE,
            self.profile,
        )

        self.assertEqual(
            [
                (step.action.value, step.state.value if step.state else None)
                for step in plan.steps
            ],
            [
                ("bootstrap_to_lobby", None),
                ("enter_stage", None),
                ("summon", None),
                ("start_wave", None),
                ("wait_for_state", "wave_active"),
                ("wait_for_state", "prep"),
                ("start_wave", None),
            ],
        )
        DefensePlanCompiler(self.profile).compile(plan)


if __name__ == "__main__":
    unittest.main()
