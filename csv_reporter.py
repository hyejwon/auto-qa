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


def _duration_seconds(start: Any, end: Any) -> str:
    if not start or not end:
        return ""
    try:
        def parse(value: Any) -> datetime:
            if isinstance(value, datetime):
                return value
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

        return f"{(parse(end) - parse(start)).total_seconds():.3f}"
    except Exception:
        return ""


def _safe_filename(value: str) -> str:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in value)
    return safe.strip("_")[:60] or "qa_report"


def build_test_result_csv(
    result: dict[str, Any],
    output_dir: Path,
    taps: list[dict[str, Any]] | None = None,
) -> Path:
    """Create an audit-oriented CSV with one row per step evidence item."""
    output_dir.mkdir(parents=True, exist_ok=True)

    test_id = _safe_text(result.get("test_id")) or "run"
    title = _safe_text(result.get("title")) or "qa_report"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = output_dir / f"{_safe_filename(test_id)}_{_safe_filename(title)}_{timestamp}.csv"

    steps = result.get("step_results") or []
    evidence_rows = sorted(taps or [], key=lambda item: _safe_text(item.get("timestamp")))
    pipeline_templates = ((result.get("pipeline") or {}).get("templates") or [])

    headers = [
        "test_id",
        "title",
        "run_status",
        "start_time",
        "end_time",
        "duration_seconds",
        "run_error",
        "template_name",
        "step",
        "step_action",
        "step_description",
        "step_target",
        "step_status",
        "result_reason",
        "vision_confidence",
        "evidence_index",
        "evidence_phase",
        "evidence_time",
        "evidence_target",
        "evidence_result",
        "evidence_confidence",
        "evidence_reason",
        "evidence_image",
    ]

    def base_row() -> dict[str, Any]:
        return {
            "test_id": result.get("test_id"),
            "title": result.get("title"),
            "run_status": result.get("status"),
            "start_time": result.get("start_time"),
            "end_time": result.get("end_time"),
            "duration_seconds": _duration_seconds(result.get("start_time"), result.get("end_time")),
            "run_error": result.get("error_message"),
        }

    def template_for_step(step_number: Any) -> str:
        try:
            number = int(step_number)
        except (TypeError, ValueError):
            return ""
        names = [
            _safe_text(template.get("name"))
            for template in pipeline_templates
            if int(template.get("start_step", 0)) <= number <= int(template.get("end_step", -1))
        ]
        return " + ".join(name for name in names if name)

    used_evidence: set[int] = set()

    def evidence_for_step(step: dict[str, Any]) -> list[dict[str, Any]]:
        step_number = step.get("step")
        direct = [
            (index, evidence)
            for index, evidence in enumerate(evidence_rows)
            if evidence.get("step_number") is not None
            and str(evidence.get("step_number")) == str(step_number)
        ]
        if direct:
            used_evidence.update(index for index, _ in direct)
            return [evidence for _, evidence in direct]

        action = _safe_text(step.get("action"))
        target = _safe_text(step.get("target"))
        matched: list[dict[str, Any]] = []
        for index, evidence in enumerate(evidence_rows):
            if index in used_evidence or evidence.get("step_number") is not None:
                continue
            evidence_action = _safe_text(evidence.get("action"))
            evidence_target = _safe_text(evidence.get("target"))
            if action == "dismiss_popups":
                is_match = evidence_action == "dismiss_popups"
            elif action == "find_and_tap":
                is_match = evidence_action in ("", "find_and_tap") and evidence_target == target
            else:
                is_match = False
            if not is_match:
                continue
            used_evidence.add(index)
            matched.append(evidence)
            if action == "find_and_tap" and step.get("passed") and evidence.get("verified") is True:
                break
        return matched

    rows: list[dict[str, Any]] = []
    if steps:
        for step in steps:
            action = _safe_text(step.get("action"))
            step_status = "SKIPPED" if step.get("skipped") else ("PASS" if step.get("passed") else "FAIL")
            reason = step.get("pass_reason") if step.get("passed") else step.get("failure_reason")
            vision_confidence = (
                _score_pct(step.get("vision_confidence"))
                if action in {"find_and_tap", "verify", "read_text"}
                else ""
            )
            matched_evidence = evidence_for_step(step) or [{}]
            for evidence_index, evidence in enumerate(matched_evidence, start=1):
                row = base_row()
                verified = evidence.get("verified")
                evidence_result = "PASS" if verified is True else ("FAIL" if verified is False else "")
                evidence_reason = evidence.get("pass_reason") if verified is True else evidence.get("failure_reason")
                row.update({
                    "template_name": template_for_step(step.get("step")),
                    "step": step.get("step"),
                    "step_action": action,
                    "step_description": step.get("label"),
                    "step_target": step.get("target"),
                    "step_status": step_status,
                    "result_reason": reason,
                    "vision_confidence": vision_confidence,
                    "evidence_index": evidence_index if evidence else "",
                    "evidence_phase": evidence.get("evidence_phase"),
                    "evidence_time": evidence.get("evidence_captured_at") or evidence.get("timestamp"),
                    "evidence_target": evidence.get("target"),
                    "evidence_result": evidence_result,
                    "evidence_confidence": _score_pct(evidence.get("confidence")),
                    "evidence_reason": evidence_reason,
                    "evidence_image": evidence.get("image"),
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
