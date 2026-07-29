from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from element_cache import CachedElement
from qa_orchestrator import QAOrchestrator
from test_manager import ActionType, TestStep
from unity_api_client import UnityAPIClient, UnityMatch


SCREEN_WIDTH = 1080
SCREEN_HEIGHT = 2316


def _button(name: str, x: int = 100, y: int = 200, **extra):
    return {
        "GameObjectName": name,
        "PositionX": x,
        "PositionY": y,
        **extra,
    }


def _unity_client_with_buttons(buttons):
    client = object.__new__(UnityAPIClient)
    client.adb = SimpleNamespace(width=SCREEN_WIDTH, height=SCREEN_HEIGHT)
    client._fetch_buttons = lambda: list(buttons)
    return client


class UnityExactButtonTests(unittest.TestCase):
    def test_find_exact_button_returns_unique_normalized_match(self):
        expected = _button("Btn_SummonAegis", x=538, y=419)
        client = _unity_client_with_buttons(
            [
                _button("Btn_AegisNavigation"),
                expected,
                _button("Btn_StartBattle"),
            ]
        )

        match = client.find_exact_button(["btn_summonaegis", "unused alias"])

        self.assertIsNotNone(match)
        self.assertIs(match.button, expected)
        self.assertEqual(match.score, 1.0)

    def test_find_exact_button_returns_none_when_missing(self):
        client = _unity_client_with_buttons([_button("Btn_StartBattle")])

        self.assertIsNone(client.find_exact_button("Btn_SummonAegis"))

    def test_find_exact_button_rejects_ambiguous_matches(self):
        client = _unity_client_with_buttons(
            [
                _button("Btn_SummonAegis", x=100),
                _button("Btn_SummonAegis", x=200),
            ]
        )

        self.assertIsNone(client.find_exact_button("Btn_SummonAegis"))

    def test_unity_to_screen_coords_flips_y_axis(self):
        coords = UnityAPIClient.unity_to_screen_coords(
            {"PositionX": 100, "PositionY": 200},
            SCREEN_WIDTH,
            SCREEN_HEIGHT,
        )

        self.assertEqual(coords, {"x": 100, "y": 2116})

    def test_unity_to_screen_coords_accepts_last_valid_pixels(self):
        coords = UnityAPIClient.unity_to_screen_coords(
            {"PositionX": SCREEN_WIDTH - 1, "PositionY": 1},
            SCREEN_WIDTH,
            SCREEN_HEIGHT,
        )

        self.assertEqual(
            coords,
            {"x": SCREEN_WIDTH - 1, "y": SCREEN_HEIGHT - 1},
        )

    def test_unity_to_screen_coords_rejects_screen_edges_and_invalid_values(self):
        invalid_buttons = [
            {"PositionX": SCREEN_WIDTH, "PositionY": 1},
            {"PositionX": -1, "PositionY": 1},
            {"PositionX": 1, "PositionY": 0},
            {"PositionX": 1, "PositionY": SCREEN_HEIGHT + 1},
            {"PositionX": "1", "PositionY": 1},
            {"PositionX": 1},
        ]

        for button in invalid_buttons:
            with self.subTest(button=button):
                self.assertIsNone(
                    UnityAPIClient.unity_to_screen_coords(
                        button,
                        SCREEN_WIDTH,
                        SCREEN_HEIGHT,
                    )
                )

    def test_legacy_llm_candidate_below_minimum_confidence_is_rejected(self):
        button = _button("Unrelated")
        client = _unity_client_with_buttons([button])
        client._choose_with_llm = (
            lambda _target, _buttons: UnityMatch(button=button, score=0.2)
        )
        client._score = lambda _target, _button: 0.0

        self.assertIsNone(client.find_best_button("소환", min_score=0.55))


class _SequencedUnity:
    def __init__(self, *, scenes=None, properties=None):
        self._scenes = list(scenes or [])
        self._properties = list(properties or [])
        self.scene_calls = 0
        self.property_calls = 0

    @staticmethod
    def _next(values):
        if not values:
            return []
        if len(values) == 1:
            return values[0]
        return values.pop(0)

    def current_scene_prefixes_v2(self):
        self.scene_calls += 1
        return self._next(self._scenes)

    def get_property_values_v2(self, ids):
        self.property_calls += 1
        return self._next(self._properties)


class _FakeStopEvent:
    def __init__(self, *, set_now=False, stop_during_wait=False):
        self.set_now = set_now
        self.stop_during_wait = stop_during_wait
        self.wait_calls = []

    def is_set(self):
        return self.set_now

    def wait(self, seconds):
        self.wait_calls.append(seconds)
        if self.stop_during_wait:
            self.set_now = True
            return True
        return False


def _orchestrator_for_wait(unity=None, stop_event=None):
    orchestrator = object.__new__(QAOrchestrator)
    orchestrator.unity = unity or _SequencedUnity()
    orchestrator._stop_event = stop_event
    orchestrator._last_failure_reason = ""
    orchestrator._last_pass_detail = ""
    return orchestrator


class WaitStepTests(unittest.TestCase):
    def test_wait_until_scene_polls_until_expected_scene(self):
        unity = _SequencedUnity(scenes=[[], ["outgame"]])
        orchestrator = _orchestrator_for_wait(unity=unity)
        slept = []
        orchestrator._sleep_interruptible = lambda seconds: (
            slept.append(seconds) or True
        )
        step = TestStep(
            action=ActionType.WAIT,
            timeout=5,
            params={
                "until_scene": "ingame|outgame",
                "poll_interval_seconds": 0.1,
            },
        )

        self.assertTrue(orchestrator._execute_wait_step(step))
        self.assertEqual(unity.scene_calls, 2)
        self.assertEqual(slept, [0.1])
        self.assertIn("scene=['outgame']", orchestrator._last_pass_detail)

    def test_wait_until_property_polls_until_expected_value(self):
        prop_id = "ingame.wave"
        unity = _SequencedUnity(
            properties=[
                {prop_id: {"Value": 1}},
                {prop_id: {"Value": {"value": 2}}},
            ]
        )
        orchestrator = _orchestrator_for_wait(unity=unity)
        orchestrator._sleep_interruptible = lambda _seconds: True
        step = TestStep(
            action=ActionType.WAIT,
            timeout=5,
            params={
                "until_property": {"id": prop_id, "equals": 2},
                "poll_interval_seconds": 0.1,
            },
        )

        self.assertTrue(orchestrator._execute_wait_step(step))
        self.assertEqual(unity.property_calls, 2)
        self.assertIn("ingame.wave=2", orchestrator._last_pass_detail)

    def test_wait_can_require_consecutive_scene_matches(self):
        unity = _SequencedUnity(
            scenes=[["outgame"], [], ["outgame"], ["outgame"]]
        )
        orchestrator = _orchestrator_for_wait(unity=unity)
        orchestrator._sleep_interruptible = lambda _seconds: True
        step = TestStep(
            action=ActionType.WAIT,
            timeout=5,
            params={
                "until_scene": "outgame",
                "poll_interval_seconds": 0.1,
                "consecutive_matches": 2,
            },
        )

        self.assertTrue(orchestrator._execute_wait_step(step))
        self.assertEqual(unity.scene_calls, 4)

    def test_wait_for_hidden_unity_button_requires_positive_api_signal(self):
        class UnityButtons:
            main_calls = 0

            def find_exact_button(self, name):
                if name == "Btn Pause":
                    return SimpleNamespace()
                if name == "Btn Main":
                    self.main_calls += 1
                    return SimpleNamespace() if self.main_calls == 1 else None
                return None

        unity = UnityButtons()
        orchestrator = _orchestrator_for_wait(unity=unity)
        orchestrator._sleep_interruptible = lambda _seconds: True
        step = TestStep(
            action=ActionType.WAIT,
            timeout=5,
            params={
                "until_unity_button": "Btn Pause",
                "until_unity_button_hidden": "Btn Main",
                "poll_interval_seconds": 0.1,
            },
        )

        self.assertTrue(orchestrator._execute_wait_step(step))
        self.assertEqual(unity.main_calls, 2)

    def test_fixed_wait_uses_interruptible_stop_event(self):
        stop_event = _FakeStopEvent()
        orchestrator = _orchestrator_for_wait(stop_event=stop_event)
        step = TestStep(
            action=ActionType.WAIT,
            params={"seconds": 3.5},
        )

        self.assertTrue(orchestrator._execute_wait_step(step))
        self.assertEqual(stop_event.wait_calls, [3.5])
        self.assertEqual(orchestrator._last_pass_detail, "3.5초 대기 완료")

    def test_fixed_wait_fails_when_stop_is_requested_during_wait(self):
        stop_event = _FakeStopEvent(stop_during_wait=True)
        orchestrator = _orchestrator_for_wait(stop_event=stop_event)
        step = TestStep(
            action=ActionType.WAIT,
            params={"seconds": 60},
        )

        self.assertFalse(orchestrator._execute_wait_step(step))
        self.assertEqual(stop_event.wait_calls, [60.0])
        self.assertEqual(
            orchestrator._last_failure_reason,
            "wait: 중단 요청으로 종료",
        )

    def test_condition_wait_stops_before_polling_external_state(self):
        stop_event = _FakeStopEvent(set_now=True)
        unity = _SequencedUnity(scenes=[["outgame"]])
        orchestrator = _orchestrator_for_wait(
            unity=unity,
            stop_event=stop_event,
        )
        step = TestStep(
            action=ActionType.WAIT,
            timeout=5,
            params={"until_scene": "outgame"},
        )

        self.assertFalse(orchestrator._execute_wait_step(step))
        self.assertEqual(unity.scene_calls, 0)
        self.assertEqual(
            orchestrator._last_failure_reason,
            "wait: 중단 요청으로 종료",
        )

    def test_execute_wait_step_does_not_capture_a_screenshot(self):
        class NoScreenshotADB:
            def screenshot(self, _path):
                raise AssertionError("WAIT must not capture a screenshot")

        orchestrator = _orchestrator_for_wait()
        orchestrator.adb = NoScreenshotADB()
        orchestrator._refresh_state_context = lambda _step: None
        result = SimpleNamespace(screenshots=[])
        step = TestStep(
            action=ActionType.WAIT,
            params={"seconds": 0},
        )

        success, confidence = orchestrator._execute_step(step, result)

        self.assertTrue(success)
        self.assertEqual(confidence, 1.0)
        self.assertEqual(result.screenshots, [])


class RuntimeResolverTests(unittest.TestCase):
    @staticmethod
    def _base_orchestrator():
        orchestrator = object.__new__(QAOrchestrator)
        taps = []
        orchestrator.adb = SimpleNamespace(
            width=SCREEN_WIDTH,
            height=SCREEN_HEIGHT,
            tap=lambda x, y: taps.append((x, y)) or True,
        )
        orchestrator._current_package = "com.example.game"
        orchestrator._current_screen_type = ""
        orchestrator._resolution = f"{SCREEN_WIDTH}x{SCREEN_HEIGHT}"
        orchestrator._last_failure_reason = ""
        orchestrator._last_pass_detail = ""
        orchestrator._last_tap_performed = False
        orchestrator._last_post_verify_screenshot = None
        orchestrator._capture_runtime_screenshot = (
            lambda prefix="runtime": Path(f"/tmp/{prefix}.png")
        )
        orchestrator._save_tap_debug = lambda *_args, **_kwargs: None
        orchestrator._auto_register_common = lambda *_args, **_kwargs: None
        return orchestrator, taps

    def test_unity_exact_hit_skips_cache_and_vision_and_taps_once(self):
        orchestrator, taps = self._base_orchestrator()
        button = _button("Btn_SummonAegis", x=500, y=300)
        orchestrator.unity = SimpleNamespace(
            find_exact_button=lambda _name: SimpleNamespace(button=button),
            unity_to_screen_coords=lambda _button, _w, _h: {"x": 500, "y": 2016},
        )
        orchestrator.cache = SimpleNamespace(
            get=lambda *_args: self.fail("cache must not be read on Unity hit")
        )
        orchestrator._resolve_with_vision = (
            lambda *_args: self.fail("Vision must not run on Unity hit")
        )
        orchestrator._verify_find_and_tap_outcome = (
            lambda _step, tap_source: tap_source == "UnityExact"
        )
        step = TestStep(
            action=ActionType.FIND_AND_TAP,
            target="이지스 소환 버튼",
            params={
                "unity_name": "Btn_SummonAegis",
                "expect_visible": "소환된 이지스",
            },
        )

        self.assertTrue(orchestrator._find_and_tap(step))
        self.assertEqual(taps, [(500, 2016)])

    def test_failed_scoped_cache_tap_is_invalidated_without_vision_retap(self):
        orchestrator, taps = self._base_orchestrator()
        invalidations = []
        orchestrator.unity = SimpleNamespace()
        orchestrator.cache = SimpleNamespace(
            get=lambda *_args: CachedElement(
                x=480,
                y=1900,
                source="vision",
                confidence=0.9,
            ),
            invalidate_element=lambda *args: invalidations.append(args),
        )
        orchestrator._resolve_with_vision = (
            lambda *_args: self.fail("Vision must not retap after cache input")
        )
        orchestrator._verify_find_and_tap_outcome = (
            lambda _step, tap_source: False
        )
        step = TestStep(
            action=ActionType.FIND_AND_TAP,
            target="이지스 소환 버튼",
            params={
                "cache_safe": True,
                "cache_scope": "aegis.prepare",
                "expect_visible": "소환된 이지스",
            },
        )

        result = orchestrator._find_and_tap(step)

        self.assertEqual(result, QAOrchestrator._TAP_OK_VERIFY_FAIL)
        self.assertEqual(taps, [(480, 1900)])
        self.assertEqual(
            invalidations,
            [
                (
                    "com.example.game",
                    "aegis.prepare",
                    "이지스 소환 버튼",
                    f"{SCREEN_WIDTH}x{SCREEN_HEIGHT}",
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
