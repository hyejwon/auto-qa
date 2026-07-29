from __future__ import annotations

import unittest
from unittest.mock import patch

from eval_agent import build_steps_summary, calc_vision_score, run_eval


def _eval_result() -> dict:
    return {
        "test_id": "skip-eval",
        "title": "선택 스텝 평가",
        "status": "PASS",
        "steps_executed": 3,
        "steps_passed": 2,
        "step_results": [
            {
                "step": 1,
                "label": "필수 1",
                "passed": True,
                "skipped": False,
                "vision_confidence": 0.8,
            },
            {
                "step": 2,
                "label": "선택 팝업",
                "passed": False,
                "skipped": True,
                "vision_confidence": 0.0,
            },
            {
                "step": 3,
                "label": "필수 2",
                "passed": True,
                "skipped": False,
                "vision_confidence": 1.0,
            },
        ],
    }


class EvalSkipTests(unittest.TestCase):
    def test_steps_summary_uses_skip_instead_of_fail(self) -> None:
        summary = build_steps_summary(_eval_result())

        self.assertIn("스텝 2 (SKIP)", summary)
        self.assertNotIn("스텝 2 (FAIL)", summary)

    def test_vision_score_excludes_skipped_steps(self) -> None:
        vision = calc_vision_score(_eval_result())

        self.assertEqual(vision["score"], 0.9)
        self.assertEqual(vision["low_confidence_steps"], [])

    def test_run_eval_sanitizes_llm_skip_only_failure(self) -> None:
        bad_flow = {
            "score": 0.7,
            "reason": "선택 팝업 실패",
            "failed_steps": [2],
            "severity": "WARNING",
        }
        with patch("eval_agent.judge_flow_completion", return_value=bad_flow):
            output = run_eval(_eval_result())

        self.assertEqual(output["flow"]["score"], 1.0)
        self.assertEqual(output["flow"]["severity"], "OK")
        self.assertEqual(output["flow"]["failed_steps"], [])
        self.assertEqual(output["final_score"], 0.96)


if __name__ == "__main__":
    unittest.main()
