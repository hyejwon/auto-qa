"""Lightweight evaluation platform for auto-qa and future agents.

This module intentionally stays independent from the current UI runner.  It
owns the test case registry, run history, evaluator outputs, and aggregate
reports.  Agent execution is adapter-based so another agent can be evaluated
without changing the storage/evaluation layer.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field


SCHEMA_VERSION = 1


class EvalCaseCreate(BaseModel):
    case_id: str = ""
    title: str
    input: dict[str, Any] = Field(default_factory=dict)
    expected_output: dict[str, Any] = Field(default_factory=dict)
    assertions: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    category: str = ""
    source: str = "manual"


class EvalCsvImportRequest(BaseModel):
    csv_text: str


class EvalRunRequest(BaseModel):
    name: str = ""
    agent_name: str = "auto-qa"
    agent_version: str = ""
    adapter: str = "http_json"
    agent_url: str = ""
    case_ids: list[str] = Field(default_factory=list)
    use_llm_judge: bool = False
    baseline_run_id: str = ""


@dataclass
class AgentExecution:
    output: dict[str, Any]
    latency_ms: int
    cost_usd: float
    error: str = ""


class EvaluationStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        # 병렬 실행 대비: WAL + busy_timeout으로 동시 쓰기 락 에러 방지
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS eval_cases (
                    case_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    expected_output_json TEXT NOT NULL,
                    assertions_json TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    category TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS eval_runs (
                    run_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    agent_version TEXT NOT NULL,
                    adapter TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    summary_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS eval_case_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    task_success INTEGER NOT NULL,
                    format_pass INTEGER NOT NULL,
                    groundedness REAL NOT NULL,
                    hallucination REAL NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    cost_usd REAL NOT NULL,
                    failure_reason TEXT NOT NULL,
                    output_json TEXT NOT NULL,
                    rule_eval_json TEXT NOT NULL,
                    llm_eval_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES eval_runs(run_id),
                    FOREIGN KEY(case_id) REFERENCES eval_cases(case_id)
                )
                """
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def upsert_case(self, case: EvalCaseCreate) -> dict[str, Any]:
        now = datetime.now().isoformat(timespec="seconds")
        case_id = case.case_id.strip() or slugify(case.title)
        payload = (
            case_id,
            case.title,
            json.dumps(case.input, ensure_ascii=False),
            json.dumps(case.expected_output, ensure_ascii=False),
            json.dumps(case.assertions, ensure_ascii=False),
            json.dumps(case.tags, ensure_ascii=False),
            case.category,
            case.source,
            now,
            now,
        )
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO eval_cases (
                    case_id, title, input_json, expected_output_json, assertions_json,
                    tags_json, category, source, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                    title=excluded.title,
                    input_json=excluded.input_json,
                    expected_output_json=excluded.expected_output_json,
                    assertions_json=excluded.assertions_json,
                    tags_json=excluded.tags_json,
                    category=excluded.category,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                payload,
            )
        return self.get_case(case_id)

    def import_csv(self, csv_text: str) -> dict[str, Any]:
        reader = csv.DictReader(io.StringIO(csv_text))
        imported: list[str] = []
        errors: list[dict[str, Any]] = []
        for line_no, row in enumerate(reader, start=2):
            try:
                case = EvalCaseCreate(
                    case_id=(row.get("case_id") or "").strip(),
                    title=(row.get("title") or "").strip(),
                    input=parse_json_cell(row.get("input_json") or row.get("input") or "{}"),
                    expected_output=parse_json_cell(row.get("expected_output_json") or row.get("expected_output") or "{}"),
                    assertions=parse_json_cell(row.get("assertions_json") or row.get("assertions") or "{}"),
                    tags=parse_tags(row.get("tags") or ""),
                    category=(row.get("category") or "").strip(),
                    source=(row.get("source") or "csv").strip(),
                )
                saved = self.upsert_case(case)
                imported.append(saved["case_id"])
            except Exception as exc:
                errors.append({"line": line_no, "error": str(exc)})
        return {"imported": imported, "errors": errors}

    def list_cases(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM eval_cases ORDER BY updated_at DESC").fetchall()
        return [case_row_to_dict(r) for r in rows]

    def get_case(self, case_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM eval_cases WHERE case_id = ?", (case_id,)).fetchone()
        if not row:
            raise KeyError(f"Eval case not found: {case_id}")
        return case_row_to_dict(row)

    def select_cases(self, case_ids: list[str]) -> list[dict[str, Any]]:
        if not case_ids:
            return self.list_cases()
        return [self.get_case(case_id) for case_id in case_ids]

    def create_run(self, req: EvalRunRequest) -> str:
        run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        now = datetime.now().isoformat(timespec="seconds")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO eval_runs (
                    run_id, name, agent_name, agent_version, adapter, status,
                    started_at, ended_at, summary_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    req.name or run_id,
                    req.agent_name,
                    req.agent_version,
                    req.adapter,
                    "RUNNING",
                    now,
                    None,
                    "{}",
                ),
            )
        return run_id

    def add_case_result(self, run_id: str, case_id: str, result: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO eval_case_results (
                    run_id, case_id, status, task_success, format_pass,
                    groundedness, hallucination, latency_ms, cost_usd,
                    failure_reason, output_json, rule_eval_json, llm_eval_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    case_id,
                    result["status"],
                    int(result["task_success"]),
                    int(result["format_pass"]),
                    float(result["groundedness"]),
                    float(result["hallucination"]),
                    int(result["latency_ms"]),
                    float(result["cost_usd"]),
                    result["failure_reason"],
                    json.dumps(result["output"], ensure_ascii=False),
                    json.dumps(result["rule_eval"], ensure_ascii=False),
                    json.dumps(result.get("llm_eval") or {}, ensure_ascii=False),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )

    def finish_run(self, run_id: str, status: str, summary: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE eval_runs
                SET status = ?, ended_at = ?, summary_json = ?
                WHERE run_id = ?
                """,
                (
                    status,
                    datetime.now().isoformat(timespec="seconds"),
                    json.dumps(summary, ensure_ascii=False),
                    run_id,
                ),
            )

    def list_runs(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM eval_runs ORDER BY started_at DESC").fetchall()
        return [run_row_to_dict(r) for r in rows]

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM eval_runs WHERE run_id = ?", (run_id,)).fetchone()
            rows = conn.execute(
                """
                SELECT r.*, c.title, c.category, c.tags_json
                FROM eval_case_results r
                JOIN eval_cases c ON c.case_id = r.case_id
                WHERE r.run_id = ?
                ORDER BY r.id ASC
                """,
                (run_id,),
            ).fetchall()
        if not run:
            raise KeyError(f"Eval run not found: {run_id}")
        data = run_row_to_dict(run)
        data["results"] = [result_row_to_dict(r) for r in rows]
        return data


class HttpJsonAgentAdapter:
    def __init__(self, agent_url: str):
        if not agent_url:
            raise ValueError("agent_url is required for http_json adapter")
        self.agent_url = agent_url

    def execute(self, case: dict[str, Any]) -> AgentExecution:
        started = time.perf_counter()
        try:
            response = httpx.post(self.agent_url, json={"case": case, "input": case["input"]}, timeout=120)
            response.raise_for_status()
            output = response.json()
            error = ""
        except Exception as exc:
            output = {"error": str(exc)}
            error = str(exc)
        latency_ms = int((time.perf_counter() - started) * 1000)
        cost_usd = float(output.get("cost_usd") or output.get("cost") or 0)
        return AgentExecution(output=output, latency_ms=latency_ms, cost_usd=cost_usd, error=error)


class AutoQaLocalAdapter:
    def execute(self, case: dict[str, Any]) -> AgentExecution:
        started = time.perf_counter()
        try:
            from config import Config
            from qa_orchestrator import QAOrchestrator
            from test_manager import TestCase

            case_input = case.get("input") or {}
            steps = case_input.get("steps") or []
            if not steps:
                raise ValueError("auto_qa_local requires input.steps")
            testcase = TestCase(
                id=case["case_id"],
                title=case_input.get("title") or case["title"],
                description=case_input.get("description") or "",
                package=case_input.get("package") or "",
                steps=steps,
                expected_results=case_input.get("expected_results") or [],
                preconditions=case_input.get("preconditions") or [],
                source_scenario=case_input.get("source_scenario") or "",
            )
            result = QAOrchestrator(Config()).run_test(case["case_id"], testcase_override=testcase)
            output = result.model_dump()
            error = ""
        except Exception as exc:
            output = {"error": str(exc)}
            error = str(exc)
        latency_ms = int((time.perf_counter() - started) * 1000)
        return AgentExecution(output=output, latency_ms=latency_ms, cost_usd=0, error=error)


def run_eval_suite(store: EvaluationStore, req: EvalRunRequest) -> dict[str, Any]:
    cases = store.select_cases(req.case_ids)
    if not cases:
        raise ValueError("평가할 테스트 케이스가 없습니다.")

    run_id = store.create_run(req)
    adapter = build_adapter(req)
    completed = 0
    try:
        for case in cases:
            execution = adapter.execute(case)
            rule_eval = evaluate_with_rules(case, execution)
            llm_eval = evaluate_with_llm(case, execution, rule_eval) if req.use_llm_judge else {}
            merged = merge_evaluations(execution, rule_eval, llm_eval)
            store.add_case_result(run_id, case["case_id"], merged)
            completed += 1

        run = store.get_run(run_id)
        summary = build_report(run, baseline=load_baseline(store, req.baseline_run_id))
        store.finish_run(run_id, "COMPLETED", summary)
        return store.get_run(run_id)
    except Exception:
        run = store.get_run(run_id)
        summary = build_report(run)
        summary["completed_cases"] = completed
        store.finish_run(run_id, "FAILED", summary)
        raise


def build_adapter(req: EvalRunRequest) -> HttpJsonAgentAdapter | AutoQaLocalAdapter:
    if req.adapter == "http_json":
        return HttpJsonAgentAdapter(req.agent_url)
    if req.adapter == "auto_qa_local":
        return AutoQaLocalAdapter()
    raise ValueError(f"Unsupported adapter: {req.adapter}")


def evaluate_with_rules(case: dict[str, Any], execution: AgentExecution) -> dict[str, Any]:
    output = execution.output
    assertions = case.get("assertions") or {}
    expected = case.get("expected_output") or {}
    failure_reasons: list[str] = []

    if execution.error:
        failure_reasons.append(execution.error)

    success = output.get("status") == "PASS" or output.get("success") is True
    if "task_success" in output:
        success = bool(output["task_success"])
    if expected:
        success = success or dict_contains(output, expected)
    if not success:
        failure_reasons.append(output.get("error_message") or output.get("failure_reason") or "task_success=false")

    required_fields = assertions.get("required_fields") or []
    format_pass = all(has_path(output, str(field)) for field in required_fields)
    if required_fields and not format_pass:
        missing = [field for field in required_fields if not has_path(output, str(field))]
        failure_reasons.append(f"missing required fields: {', '.join(map(str, missing))}")
    if not required_fields:
        format_pass = isinstance(output, dict) and "error" not in output

    groundedness = calc_groundedness(output, assertions)
    hallucination = calc_hallucination(output, assertions)

    min_groundedness = float(assertions.get("min_groundedness", 0))
    max_hallucination = float(assertions.get("max_hallucination", 1))
    if groundedness < min_groundedness:
        failure_reasons.append(f"groundedness below threshold: {groundedness:.2f} < {min_groundedness:.2f}")
    if hallucination > max_hallucination:
        failure_reasons.append(f"hallucination above threshold: {hallucination:.2f} > {max_hallucination:.2f}")

    passed = success and format_pass and groundedness >= min_groundedness and hallucination <= max_hallucination
    return {
        "task_success": success,
        "format_pass": format_pass,
        "groundedness": groundedness,
        "hallucination": hallucination,
        "passed": passed,
        "failure_reason": "; ".join(unique_nonempty(failure_reasons)),
    }


def evaluate_with_llm(case: dict[str, Any], execution: AgentExecution, rule_eval: dict[str, Any]) -> dict[str, Any]:
    try:
        from llm_client import build_genai_client

        client = build_genai_client()
        prompt = f"""You are evaluating an AI agent result.
Return JSON only with keys: score, groundedness, hallucination, failure_reason.

Test case:
{json.dumps(case, ensure_ascii=False)}

Agent output:
{json.dumps(execution.output, ensure_ascii=False)}

Rule evaluation:
{json.dumps(rule_eval, ensure_ascii=False)}
"""
        response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return parse_json_from_text(response.text)
    except Exception as exc:
        return {"error": str(exc)}


def merge_evaluations(
    execution: AgentExecution,
    rule_eval: dict[str, Any],
    llm_eval: dict[str, Any],
) -> dict[str, Any]:
    groundedness = float(llm_eval.get("groundedness", rule_eval["groundedness"])) if "error" not in llm_eval else rule_eval["groundedness"]
    hallucination = float(llm_eval.get("hallucination", rule_eval["hallucination"])) if "error" not in llm_eval else rule_eval["hallucination"]
    failure_reason = rule_eval.get("failure_reason", "")
    if llm_eval.get("failure_reason"):
        failure_reason = "; ".join(unique_nonempty([failure_reason, str(llm_eval["failure_reason"])]))
    passed = bool(rule_eval["task_success"]) and bool(rule_eval["format_pass"]) and groundedness >= 0.7 and hallucination <= 0.2
    return {
        "status": "PASS" if passed else "FAIL",
        "task_success": bool(rule_eval["task_success"]),
        "format_pass": bool(rule_eval["format_pass"]),
        "groundedness": groundedness,
        "hallucination": hallucination,
        "latency_ms": execution.latency_ms,
        "cost_usd": execution.cost_usd,
        "failure_reason": failure_reason,
        "output": execution.output,
        "rule_eval": rule_eval,
        "llm_eval": llm_eval,
    }


def build_report(run: dict[str, Any], baseline: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    results = run.get("results") or []
    total = len(results)
    if total == 0:
        return {
            "total_cases": 0,
            "task_success_rate": 0,
            "format_pass_rate": 0,
            "groundedness": 0,
            "hallucination_rate": 0,
            "avg_latency_ms": 0,
            "total_cost_usd": 0,
            "regression_cases": [],
            "failure_reasons": {},
        }

    regression_cases = find_regressions(results, baseline.get("results", []) if baseline else [])
    failure_reasons: dict[str, int] = {}
    for result in results:
        reason = result.get("failure_reason") or "unknown"
        if result["status"] == "FAIL":
            failure_reasons[reason] = failure_reasons.get(reason, 0) + 1

    return {
        "total_cases": total,
        "task_success_rate": round(sum(1 for r in results if r["task_success"]) / total, 4),
        "format_pass_rate": round(sum(1 for r in results if r["format_pass"]) / total, 4),
        "groundedness": round(sum(float(r["groundedness"]) for r in results) / total, 4),
        "hallucination_rate": round(sum(float(r["hallucination"]) for r in results) / total, 4),
        "avg_latency_ms": round(sum(int(r["latency_ms"]) for r in results) / total, 1),
        "total_cost_usd": round(sum(float(r["cost_usd"]) for r in results), 6),
        "regression_cases": regression_cases,
        "failure_reasons": failure_reasons,
    }


def load_baseline(store: EvaluationStore, baseline_run_id: str) -> Optional[dict[str, Any]]:
    if not baseline_run_id:
        return None
    return store.get_run(baseline_run_id)


def find_regressions(results: list[dict[str, Any]], baseline_results: list[dict[str, Any]]) -> list[str]:
    baseline_status = {r["case_id"]: r["status"] for r in baseline_results}
    return [
        r["case_id"]
        for r in results
        if baseline_status.get(r["case_id"]) == "PASS" and r["status"] == "FAIL"
    ]


def calc_groundedness(output: dict[str, Any], assertions: dict[str, Any]) -> float:
    required_evidence = assertions.get("required_evidence") or []
    if not required_evidence:
        return 1.0 if "error" not in output else 0.0
    text = json.dumps(output, ensure_ascii=False).lower()
    hits = sum(1 for item in required_evidence if str(item).lower() in text)
    return round(hits / len(required_evidence), 4)


def calc_hallucination(output: dict[str, Any], assertions: dict[str, Any]) -> float:
    forbidden_claims = assertions.get("forbidden_claims") or []
    if not forbidden_claims:
        return 0.0
    text = json.dumps(output, ensure_ascii=False).lower()
    hits = sum(1 for item in forbidden_claims if str(item).lower() in text)
    return round(hits / len(forbidden_claims), 4)


def has_path(data: dict[str, Any], dotted_path: str) -> bool:
    cur: Any = data
    for part in dotted_path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def dict_contains(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    for key, expected_value in expected.items():
        if key not in actual:
            return False
        actual_value = actual[key]
        if isinstance(expected_value, dict):
            if not isinstance(actual_value, dict) or not dict_contains(actual_value, expected_value):
                return False
        elif actual_value != expected_value:
            return False
    return True


def parse_json_cell(value: str) -> dict[str, Any]:
    value = value.strip()
    if not value:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("JSON cell must be an object")
    return parsed


def parse_tags(value: str) -> list[str]:
    if not value.strip():
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def parse_json_from_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.S)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("LLM judge response must be a JSON object")
    return parsed


def case_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "case_id": row["case_id"],
        "title": row["title"],
        "input": json.loads(row["input_json"]),
        "expected_output": json.loads(row["expected_output_json"]),
        "assertions": json.loads(row["assertions_json"]),
        "tags": json.loads(row["tags_json"]),
        "category": row["category"],
        "source": row["source"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def run_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "name": row["name"],
        "agent_name": row["agent_name"],
        "agent_version": row["agent_version"],
        "adapter": row["adapter"],
        "status": row["status"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "summary": json.loads(row["summary_json"] or "{}"),
    }


def result_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "case_id": row["case_id"],
        "title": row["title"],
        "category": row["category"],
        "tags": json.loads(row["tags_json"]),
        "status": row["status"],
        "task_success": bool(row["task_success"]),
        "format_pass": bool(row["format_pass"]),
        "groundedness": row["groundedness"],
        "hallucination": row["hallucination"],
        "latency_ms": row["latency_ms"],
        "cost_usd": row["cost_usd"],
        "failure_reason": row["failure_reason"],
        "output": json.loads(row["output_json"]),
        "rule_eval": json.loads(row["rule_eval_json"]),
        "llm_eval": json.loads(row["llm_eval_json"]),
        "created_at": row["created_at"],
    }


def slugify(value: str) -> str:
    normalized = re.sub(r"[^0-9a-zA-Z가-힣_-]+", "_", value.strip()).strip("_").lower()
    return normalized or f"case_{uuid.uuid4().hex[:8]}"


def unique_nonempty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = value.strip()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result
