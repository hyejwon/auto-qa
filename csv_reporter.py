"""CSV report generation for QA run results."""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\r", " ").replace("\n", " ").strip()


def _score_pct(value: Any) -> str:
    if value is None:
        return ""
    try:
        return f"{round(float(value) * 100)}%"
    except Exception:
        return _safe_text(value)


def _safe_filename(value: str) -> str:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in value)
    return safe.strip("_")[:60] or "qa_report"


def build_test_result_csv(
    result: dict[str, Any],
    output_dir: Path,
    taps: list[dict[str, Any]] | None = None,
) -> Path:
    """Create a CSV file for one QA run result and return its path."""
    output_dir.mkdir(parents=True, exist_ok=True)

    test_id = _safe_text(result.get("test_id")) or "run"
    title = _safe_text(result.get("title")) or "qa_report"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = output_dir / f"{_safe_filename(test_id)}_{_safe_filename(title)}_{timestamp}.csv"

    eval_out = result.get("eval_output") or {}
    flow = eval_out.get("flow") or {}
    vision = eval_out.get("vision") or {}
    steps = result.get("step_results") or []
    tap_rows = taps or []
    screenshots = result.get("screenshots") or []
    screenshots_joined = ";".join(_safe_text(v) for v in screenshots)

    headers = [
        "test_id",
        "title",
        "status",
        "start_time",
        "end_time",
        "steps_passed",
        "steps_executed",
        "error_message",
        "final_score",
        "needs_alert",
        "flow_score",
        "flow_severity",
        "flow_reason",
        "vision_score",
        "low_confidence_steps",
        "step",
        "step_label",
        "step_passed",
        "step_failure_reason",
        "vision_confidence",
        "tap_target",
        "tap_verified",
        "tap_confidence",
        "tap_failure_reason",
        "tap_debug_image",
        "all_screenshots",
    ]

    def base_row() -> dict[str, Any]:
        return {
            "test_id": result.get("test_id"),
            "title": result.get("title"),
            "status": result.get("status"),
            "start_time": result.get("start_time"),
            "end_time": result.get("end_time"),
            "steps_passed": result.get("steps_passed"),
            "steps_executed": result.get("steps_executed"),
            "error_message": result.get("error_message"),
            "final_score": _score_pct(eval_out.get("final_score")),
            "needs_alert": eval_out.get("needs_alert"),
            "flow_score": _score_pct(flow.get("score")),
            "flow_severity": flow.get("severity"),
            "flow_reason": flow.get("reason"),
            "vision_score": _score_pct(vision.get("score")),
            "low_confidence_steps": ";".join(map(str, vision.get("low_confidence_steps") or [])),
            "all_screenshots": screenshots_joined,
        }

    # 탭 디버그 기록을 스텝에 매칭 — 순번 조인은 read_text 등 탭 없는 스텝과
    # 재시도(스텝당 다수 row)에서 어긋나므로, target 기준으로 순서 소비하며 매칭.
    # (같은 target의 연속 row는 마지막 것 = 최종 시도 결과를 사용)
    tap_i = 0

    def _tap_for_step(step: dict) -> dict:
        nonlocal tap_i
        if "action" not in step:  # 구버전 결과 폴백: 기존 순번 조인
            idx = step.get("step", 1) - 1
            return tap_rows[idx] if 0 <= idx < len(tap_rows) else {}
        if step.get("action") != "find_and_tap":
            return {}
        target = step.get("target") or ""
        matched: dict = {}
        j = tap_i
        while j < len(tap_rows):
            if tap_rows[j].get("target") == target:
                matched = tap_rows[j]
                j += 1
                while j < len(tap_rows) and tap_rows[j].get("target") == target:
                    matched = tap_rows[j]
                    j += 1
                tap_i = j
                break
            j += 1
        return matched

    rows: list[dict[str, Any]] = []
    if steps:
        for step in steps:
            tap = _tap_for_step(step)
            row = base_row()
            passed_display: Any = step.get("passed")
            if step.get("skipped"):
                passed_display = "SKIPPED"
            row.update({
                "step": step.get("step"),
                "step_label": step.get("label"),
                "step_passed": passed_display,
                "step_failure_reason": step.get("failure_reason"),
                "vision_confidence": _score_pct(step.get("vision_confidence")),
                "tap_target": tap.get("target"),
                "tap_verified": tap.get("verified"),
                "tap_confidence": _score_pct(tap.get("confidence")),
                "tap_failure_reason": tap.get("failure_reason"),
                "tap_debug_image": tap.get("image"),
            })
            rows.append(row)
    else:
        rows.append(base_row())

    # UTF-8 BOM keeps Korean readable when opened directly in Excel.
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _safe_text(v) for k, v in row.items()})

    return csv_path
