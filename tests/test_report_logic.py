from __future__ import annotations

import unittest

from report_logic import (
    normalize_result_for_report,
    normalize_shared_report,
    step_counts,
)


def _result_with_skip() -> dict:
    return {
        "status": "PASS",
        "title": "선택 스텝 리포트",
        "steps_passed": 2,
        "steps_executed": 3,
        "step_results": [
            {
                "step": 1,
                "label": "앱 실행",
                "target": "앱",
                "passed": True,
                "skipped": False,
                "vision_confidence": 1.0,
            },
            {
                "step": 2,
                "label": "팝업 닫기",
                "target": "확인 팝업",
                "passed": False,
                "skipped": True,
                "vision_confidence": 0.0,
            },
            {
                "step": 3,
                "label": "최종 확인",
                "target": "로비",
                "passed": True,
                "skipped": False,
                "vision_confidence": 0.8,
            },
        ],
        "eval_output": {
            "flow": {
                "score": 0.7,
                "reason": "2번 실패",
                "failed_steps": [2],
                "severity": "WARNING",
            },
            "vision": {
                "score": 0.6,
                "low_confidence_steps": [{"step": 2, "confidence": 0.0}],
            },
            "final_score": 0.66,
            "needs_alert": True,
        },
    }


class ReportLogicTests(unittest.TestCase):
    def test_step_counts_treats_skip_as_completed_not_failed(self) -> None:
        self.assertEqual(
            step_counts(_result_with_skip()),
            {
                "passed": 2,
                "skipped": 1,
                "failed": 0,
                "completed": 3,
                "total": 3,
            },
        )

    def test_normalize_result_repairs_skip_only_eval_warning(self) -> None:
        normalized = normalize_result_for_report(_result_with_skip())

        self.assertEqual(normalized["steps_passed"], 2)
        self.assertEqual(normalized["steps_skipped"], 1)
        self.assertEqual(normalized["steps_failed"], 0)
        self.assertEqual(normalized["steps_completed"], 3)
        self.assertEqual(normalized["eval_output"]["flow"]["score"], 1.0)
        self.assertEqual(normalized["eval_output"]["flow"]["severity"], "OK")
        self.assertEqual(normalized["eval_output"]["flow"]["failed_steps"], [])
        self.assertEqual(normalized["eval_output"]["vision"]["score"], 0.9)
        self.assertEqual(normalized["eval_output"]["final_score"], 0.96)

    def test_shared_report_marks_only_parent_skipped_evidence_as_skip(self) -> None:
        snapshot = {
            "result": _result_with_skip(),
            "taps": [
                {
                    "timestamp": "1",
                    "step_number": 2,
                    "target": "확인 팝업",
                    "verified": False,
                },
                {
                    "timestamp": "2",
                    "step_number": 3,
                    "target": "로비",
                    "verified": False,
                },
                {
                    "timestamp": "3",
                    "target": "[읽기] 확인 팝업",
                    "verified": False,
                },
            ],
        }

        normalized = normalize_shared_report(snapshot)

        self.assertEqual(normalized["taps"][0]["outcome"], "SKIP")
        self.assertEqual(normalized["taps"][1]["outcome"], "FAIL")
        self.assertEqual(normalized["taps"][2]["outcome"], "SKIP")

    def test_real_failure_is_not_rewritten(self) -> None:
        result = _result_with_skip()
        result["status"] = "FAIL"
        result["step_results"][2].update({"passed": False, "skipped": False})
        result["eval_output"]["flow"]["failed_steps"] = [2, 3]

        normalized = normalize_result_for_report(result)

        self.assertEqual(normalized["steps_failed"], 1)
        self.assertEqual(
            normalized["eval_output"]["flow"]["severity"],
            "WARNING",
        )


if __name__ == "__main__":
    unittest.main()
