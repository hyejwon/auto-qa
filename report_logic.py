"""리포트 표시용 PASS/SKIP/FAIL 정규화.

실행 엔진은 선택 스텝 미노출을 ``passed=False, skipped=True``로 저장한다.
리포트 계층이 이를 ``passed`` 하나만 보고 FAIL로 해석하지 않도록, 집계와
공유 스냅샷의 증거 상태를 한 곳에서 삼분화한다.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


FLOW_WEIGHT = 0.6
VISION_WEIGHT = 0.4


def step_counts(result: dict[str, Any]) -> dict[str, int]:
    """스텝 결과를 PASS/SKIP/FAIL로 상호 배타적으로 집계한다."""
    steps = result.get("step_results")
    if not isinstance(steps, list) or not steps:
        passed = int(result.get("steps_passed") or 0)
        skipped = int(result.get("steps_skipped") or 0)
        total = int(result.get("steps_executed") or 0)
        return {
            "passed": passed,
            "skipped": skipped,
            "failed": max(0, total - passed - skipped),
            "completed": passed + skipped,
            "total": total,
        }

    skipped = sum(bool(step.get("skipped")) for step in steps if isinstance(step, dict))
    passed = sum(
        bool(step.get("passed")) and not bool(step.get("skipped"))
        for step in steps
        if isinstance(step, dict)
    )
    failed = sum(
        not bool(step.get("passed")) and not bool(step.get("skipped"))
        for step in steps
        if isinstance(step, dict)
    )
    return {
        "passed": passed,
        "skipped": skipped,
        "failed": failed,
        "completed": passed + skipped,
        "total": len(steps),
    }


def _step_number_set(steps: list[dict[str, Any]], key: str) -> set[int]:
    numbers: set[int] = set()
    for step in steps:
        if not step.get(key):
            continue
        try:
            numbers.add(int(step.get("step")))
        except (TypeError, ValueError):
            continue
    return numbers


def _vision_summary(steps: list[dict[str, Any]]) -> tuple[float, list[dict[str, Any]]]:
    scores: list[float] = []
    low_confidence: list[dict[str, Any]] = []
    for step in steps:
        if step.get("skipped"):
            continue
        confidence = step.get("vision_confidence", 1.0)
        if not isinstance(confidence, (int, float)):
            confidence = 1.0
        score = float(confidence)
        scores.append(score)
        if score < 0.6:
            low_confidence.append(
                {
                    "step": step.get("step"),
                    "label": step.get("label", ""),
                    "confidence": score,
                }
            )
    average = round(sum(scores) / len(scores), 3) if scores else 1.0
    return average, low_confidence


def normalize_result_for_report(result: dict[str, Any]) -> dict[str, Any]:
    """새 실행과 과거 공유 스냅샷을 동일한 리포트 집계 규칙으로 맞춘다."""
    normalized = deepcopy(result)
    steps = [
        step
        for step in normalized.get("step_results", [])
        if isinstance(step, dict)
    ]
    counts = step_counts(normalized)
    normalized["steps_passed"] = counts["passed"]
    normalized["steps_skipped"] = counts["skipped"]
    normalized["steps_failed"] = counts["failed"]
    normalized["steps_completed"] = counts["completed"]

    for step in steps:
        if step.get("skipped") and not step.get("skip_reason"):
            step["skip_reason"] = "조건부 스텝 건너뜀 (정상)"

    eval_output = normalized.get("eval_output")
    if not isinstance(eval_output, dict):
        return normalized
    flow = eval_output.get("flow")
    vision = eval_output.get("vision")
    if not isinstance(flow, dict) or not isinstance(vision, dict):
        return normalized

    skipped_steps = _step_number_set(steps, "skipped")
    actual_failed_steps = {
        int(step["step"])
        for step in steps
        if not step.get("passed")
        and not step.get("skipped")
        and isinstance(step.get("step"), int)
    }
    reported_failed_steps: set[int] = set()
    for value in flow.get("failed_steps") or []:
        try:
            reported_failed_steps.add(int(value))
        except (TypeError, ValueError):
            continue

    # 과거 Eval이 SKIP만 failed_steps로 기록한 스냅샷을 읽을 때 바로 교정한다.
    skip_only_misclassification = (
        normalized.get("status") == "PASS"
        and not actual_failed_steps
        and bool(reported_failed_steps)
        and reported_failed_steps.issubset(skipped_steps)
    )
    if skip_only_misclassification:
        flow["score"] = 1.0
        flow["reason"] = (
            f"필수 스텝 모두 통과, 선택·조건부 스텝 {counts['skipped']}건 정상 건너뜀"
        )
        flow["failed_steps"] = []
        flow["severity"] = "OK"

        vision_score, low_confidence_steps = _vision_summary(steps)
        vision["score"] = vision_score
        vision["low_confidence_steps"] = low_confidence_steps
        eval_output["final_score"] = round(
            float(flow["score"]) * FLOW_WEIGHT
            + vision_score * VISION_WEIGHT,
            3,
        )
        eval_output["needs_alert"] = False

    return normalized


def _normalized_target(value: Any) -> str:
    target = str(value or "").strip()
    if target.startswith("[읽기]"):
        target = target[len("[읽기]") :].strip()
    return target


def normalize_shared_report(snapshot: dict[str, Any]) -> dict[str, Any]:
    """공유 리포트와 증거를 부모 스텝의 최종 상태에 맞게 정규화한다."""
    normalized = deepcopy(snapshot)
    result = normalize_result_for_report(normalized.get("result") or {})
    normalized["result"] = result

    steps = [
        step for step in result.get("step_results", []) if isinstance(step, dict)
    ]
    by_number = {
        int(step["step"]): step
        for step in steps
        if isinstance(step.get("step"), int)
    }
    by_target: dict[str, list[dict[str, Any]]] = {}
    for step in steps:
        for value in (step.get("target"), step.get("label")):
            target = _normalized_target(value)
            if target:
                by_target.setdefault(target, []).append(step)

    taps = normalized.get("taps")
    if not isinstance(taps, list):
        return normalized
    for tap in taps:
        if not isinstance(tap, dict):
            continue
        parent = None
        try:
            parent = by_number.get(int(tap.get("step_number")))
        except (TypeError, ValueError):
            pass
        if parent is None:
            candidates = by_target.get(_normalized_target(tap.get("target")), [])
            if len(candidates) == 1:
                parent = candidates[0]

        if tap.get("verified") is True:
            tap["outcome"] = "PASS"
        elif parent and parent.get("skipped"):
            tap["outcome"] = "SKIP"
            tap["skip_reason"] = parent.get("skip_reason") or "조건부 스텝 건너뜀 (정상)"
        else:
            tap["outcome"] = "FAIL"

    return normalized
