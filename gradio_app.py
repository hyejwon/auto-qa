# gradio_app.py
"""
QA 자동화 테스트 - Gradio 인터페이스
사용법: python gradio_app.py
"""
import asyncio
import inspect
import json
import logging
import yaml
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

import gradio as gr

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from anyio import BrokenResourceError
from langchain_mcp_adapters.tools import load_mcp_tools
from langchain_google_genai import ChatGoogleGenerativeAI

from qa_agent.config import QAConfig
from qa_agent.state import AgentState
from qa_agent.tools.schemas import lc_tools_to_openai_schema
from qa_agent.planner.planner import make_plan
from qa_agent.executor.executor import run_current_step
from qa_agent.report.report import generate_report
from qa_agent.graph.build_graph import build_graph

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
REPORTS_DIR = Path("./reports")
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# Running test lock
_running = False


def _run_coroutine_in_new_loop(coro, *, cleanup_timeout: float = 2.0):
    """
    Run an async coroutine in a dedicated event loop.

    Gradio callbacks are sync here, so we create/close our own loop.
    We also drain/cancel pending tasks to avoid:
      "Task was destroyed but it is pending!"
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            if pending:
                loop.run_until_complete(asyncio.wait(pending, timeout=cleanup_timeout))
                pending = [t for t in pending if not t.done()]
                if pending:
                    for task in pending:
                        task.cancel()
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            asyncio.set_event_loop(None)
            loop.close()


async def _maybe_close_llm(llm) -> None:
    """
    Best-effort close for LangChain Google GenAI chat models.

    Newer google-genai clients may spawn async close tasks in __del__ if not closed.
    We try common close hooks to keep shutdown clean.
    """
    if llm is None:
        return

    def _close_call(obj, name: str):
        fn = getattr(obj, name, None)
        return fn() if callable(fn) else None

    for name in ("aclose", "close"):
        try:
            res = _close_call(llm, name)
            if inspect.isawaitable(res):
                await res
        except Exception:
            pass

    for attr in ("client", "_client", "async_client", "_async_client"):
        client = getattr(llm, attr, None)
        if client is None:
            continue
        for name in ("aclose", "close"):
            try:
                res = _close_call(client, name)
                if inspect.isawaitable(res):
                    await res
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Core: reuse logic from main.py
# ---------------------------------------------------------------------------
async def planner_node(state: AgentState, llm):
    tools_schema = state.get("tools_schema", [])
    plan = await make_plan(llm, state.get("goal", ""), tools_schema)
    return {
        "plan": plan,
        "goal_steps": plan.get("steps", []),
        "current_step_idx": 0,
        "mode": "executing",
        "status": "running",
        "messages": state.get("messages", [])
        + [
            {
                "role": "assistant",
                "content": f"PLANNED: {json.dumps(plan, ensure_ascii=False)[:2000]}",
            }
        ],
    }


async def replan_node(state: AgentState, llm):
    state["mode"] = "planning"
    return await planner_node(state, llm)


async def executor_node(state: AgentState):
    max_steps = state.get("max_steps", 40)
    if state.get("step", 0) >= max_steps:
        state["status"] = "error"
        state["error"] = f"max_steps({max_steps}) reached"
        return {"status": "error", "error": state["error"]}
    return await run_current_step(state)


async def run_single_test(
    goal: str,
    test_name: str,
    cfg: QAConfig,
    tools_schema: list,
    tool_name_map: dict,
    graph,
    output_dir: Path,
    log_callback=None,
) -> Dict[str, Any]:
    """단일 테스트 케이스 실행 (로그 콜백 지원)"""

    def _log(msg: str):
        if log_callback:
            log_callback(msg)

    _log(f"{'='*50}")
    _log(f"테스트 시작: {test_name}")
    _log(f"Goal: {goal[:120]}...")
    _log(f"{'='*50}")

    init_state: AgentState = {
        "mode": "planning",
        "goal": goal,
        "plan": {},
        "goal_steps": [],
        "current_step_idx": 0,
        "messages": [
            {
                "role": "user",
                "content": "시작해줘. 필요한 도구를 호출해서 목표를 달성해.",
            }
        ],
        "tools_schema": tools_schema,
        "tool_name_map": tool_name_map,
        "last_tool_results": [],
        "trace": [],
        "step": 0,
        "max_steps": cfg.max_steps,
        "status": "running",
        "error": "",
        "package_name": cfg.package_name,
        "device_id": cfg.device_id,
        "max_find_attempts": cfg.max_find_attempts,
        "find_attempts": 0,
        "repeat_count": 0,
    }

    try:
        final_state = await graph.ainvoke(
            init_state, config={"recursion_limit": 500}
        )

        report_filename = None
        md_content = ""
        if final_state.get("status") in ("done", "error"):
            md_content = generate_report(final_state)
            safe_name = "".join(
                c if c.isalnum() or c in ("-", "_") else "_" for c in test_name
            )
            report_filename = (
                f"qa_report_{safe_name}_{datetime.now().strftime('%H%M%S')}.md"
            )
            report_path = output_dir / report_filename
            report_path.write_text(md_content, encoding="utf-8")
            _log(f"리포트 저장: {report_path}")

        status = final_state.get("status", "unknown")
        icon = "PASS" if status == "done" else "FAIL"
        _log(f"[{icon}] 테스트 완료: {test_name} - {status.upper()}")

        return {
            "name": test_name,
            "goal": goal,
            "status": status,
            "error": final_state.get("error", ""),
            "report_file": report_filename,
            "report_md": md_content,
        }
    except Exception as e:
        logging.exception(f"테스트 실행 중 예외: {test_name}")
        _log(f"[ERROR] {test_name}: {e}")
        return {
            "name": test_name,
            "goal": goal,
            "status": "error",
            "error": str(e),
            "report_file": None,
            "report_md": "",
        }


def generate_summary_report(results: List[Dict[str, Any]]) -> str:
    """여러 테스트 결과 요약 마크다운"""
    md = []
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = len(results)
    passed = sum(1 for r in results if r["status"] == "done")
    failed = total - passed

    md.append("# QA 자동화 테스트 요약 리포트")
    md.append("")
    md.append(f"> **실행 시간:** {timestamp}")
    md.append("")
    if failed == 0:
        md.append(f"> ### **전체 통과: {passed}/{total}**")
    else:
        md.append(f"> ### **{failed}개 실패: {passed}/{total} 통과**")
    md.append("")
    md.append("| # | 테스트명 | 결과 | 에러 |")
    md.append("|---|----------|------|------|")
    for idx, r in enumerate(results, 1):
        status_icon = "PASS" if r["status"] == "done" else "FAIL"
        error_short = (r.get("error", "") or "")[:60]
        md.append(f"| {idx} | {r['name']} | {status_icon} | {error_short} |")
    md.append("")
    return "\n".join(md)


# ---------------------------------------------------------------------------
# Async runner: MCP session + test execution
# ---------------------------------------------------------------------------
async def _run_tests_async(
    test_cases: List[Dict[str, Any]],
    log_callback=None,
) -> List[Dict[str, Any]]:
    """MCP 세션을 열고 테스트 케이스 목록을 실행"""
    cfg = QAConfig()

    logging.basicConfig(
        level=logging.DEBUG if cfg.debug else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    def _log(msg):
        if log_callback:
            log_callback(msg)

    llm = ChatGoogleGenerativeAI(
        model=cfg.model,
        project=cfg.project,
        location=cfg.location,
    )

    server_params = StdioServerParameters(command=cfg.mcp_command, args=cfg.mcp_args)
    results: List[Dict[str, Any]] = []

    try:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=30.0)
                lc_tools = await asyncio.wait_for(
                    load_mcp_tools(session), timeout=60.0
                )

                tool_name_map = {t.name: t for t in lc_tools}
                tools_schema = lc_tools_to_openai_schema(lc_tools)

                # 디바이스 설정
                _log(f"디바이스 설정 중: {cfg.device_id}")
                set_device_tool = tool_name_map.get("set_device")
                if set_device_tool:
                    try:
                        if hasattr(set_device_tool, "ainvoke"):
                            result = await set_device_tool.ainvoke({"device_id": cfg.device_id})
                        else:
                            result = set_device_tool.invoke({"device_id": cfg.device_id})
                        _log(f"디바이스 설정 결과: {result}")
                    except Exception as e:
                        _log(f"디바이스 설정 실패: {e}")
                else:
                    _log("WARNING: set_device 도구를 찾을 수 없음")
                    _log(f"사용 가능한 도구: {list(tool_name_map.keys())[:10]}...")

                async def planner_wrapper(state: AgentState):
                    return await planner_node(state, llm)

                async def replan_wrapper(state: AgentState):
                    return await replan_node(state, llm)

                graph = build_graph(
                    planner_node=planner_wrapper,
                    replan_node=replan_wrapper,
                    executor_node=executor_node,
                )

                for idx, tc in enumerate(test_cases, 1):
                    test_name = tc.get("name", f"테스트 {idx}")
                    goal = tc.get("goal", "")
                    if not goal:
                        _log(f"건너뜀: {test_name} (goal이 비어있음)")
                        continue

                    result = await run_single_test(
                        goal=goal,
                        test_name=test_name,
                        cfg=cfg,
                        tools_schema=tools_schema,
                        tool_name_map=tool_name_map,
                        graph=graph,
                        output_dir=REPORTS_DIR,
                        log_callback=log_callback,
                    )
                    results.append(result)

    except* BrokenResourceError:
        _log("MCP 연결이 종료되었습니다 (BrokenResourceError).")

    finally:
        await _maybe_close_llm(llm)

    return results


# ---------------------------------------------------------------------------
# Helper: Parse test cases from Gradio dataframe
# ---------------------------------------------------------------------------
def _parse_test_cases_from_df(df_data) -> List[Dict[str, Any]]:
    """Gradio Dataframe 값을 test case 리스트로 변환"""
    cases = []
    if df_data is None:
        return cases

    # Gradio 5.x Dataframe은 pandas DataFrame을 반환함
    # for row in DataFrame 은 컬럼명을 순회하므로 .values.tolist()로 변환 필요
    import pandas as pd
    if isinstance(df_data, pd.DataFrame):
        df_data = df_data.values.tolist()

    for row in df_data:
        name = str(row[0]).strip() if row[0] else ""
        goal = str(row[1]).strip() if row[1] else ""
        if name and goal:
            cases.append({"name": name, "goal": goal})
    return cases


# ---------------------------------------------------------------------------
# Gradio event handlers
# ---------------------------------------------------------------------------
def load_yaml_file(file_obj) -> list:
    """업로드된 YAML/JSON 파일에서 테스트 케이스 로드"""
    if file_obj is None:
        return gr.update()

    path = Path(file_obj.name if hasattr(file_obj, "name") else file_obj)
    content = path.read_text(encoding="utf-8")

    if path.suffix in (".yaml", ".yml"):
        data = yaml.safe_load(content)
    elif path.suffix == ".json":
        data = json.loads(content)
    else:
        gr.Warning("YAML 또는 JSON 파일만 지원합니다.")
        return gr.update()

    cases = []
    if isinstance(data, list):
        if all(isinstance(item, str) for item in data):
            cases = [[f"테스트 {i+1}", g] for i, g in enumerate(data)]
        else:
            cases = [[tc.get("name", f"테스트 {i+1}"), tc.get("goal", "")] for i, tc in enumerate(data)]
    elif isinstance(data, dict) and "test_cases" in data:
        cases = [
            [tc.get("name", f"테스트 {i+1}"), tc.get("goal", "")]
            for i, tc in enumerate(data["test_cases"])
        ]
    else:
        gr.Warning("올바른 테스트 케이스 형식이 아닙니다.")
        return gr.update()

    return cases


def export_yaml(df_data) -> Optional[str]:
    """현재 테스트 케이스를 YAML 파일로 내보내기"""
    cases = _parse_test_cases_from_df(df_data)
    if not cases:
        gr.Warning("내보낼 테스트 케이스가 없습니다.")
        return None

    export_path = Path(f"test_cases_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml")
    data = {"test_cases": cases}
    export_path.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )
    return str(export_path)


def list_existing_reports() -> List[List[str]]:
    """reports/ 디렉토리의 기존 리포트 목록"""
    reports = sorted(REPORTS_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    rows = []
    for r in reports:
        mtime = datetime.fromtimestamp(r.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        rows.append([r.name, mtime])
    return rows


def read_report(report_name: str) -> str:
    """리포트 파일 내용 읽기"""
    if not report_name:
        return ""
    path = REPORTS_DIR / report_name
    if path.exists():
        return path.read_text(encoding="utf-8")
    return f"리포트를 찾을 수 없습니다: {report_name}"


def on_report_select(evt: gr.SelectData, report_table):
    """리포트 테이블 클릭 시 내용 표시"""
    if evt.index is None:
        return ""

    import pandas as pd
    if isinstance(report_table, pd.DataFrame):
        report_table = report_table.values.tolist()

    row_idx = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
    if report_table and 0 <= row_idx < len(report_table):
        report_name = report_table[row_idx][0]
        return read_report(report_name)
    return ""


def run_tests(df_data, progress=gr.Progress(track_tqdm=True)):
    """테스트 실행 (Gradio에서 호출)"""
    global _running
    if _running:
        gr.Warning("이미 테스트가 실행 중입니다.")
        return "", "", gr.update()

    cases = _parse_test_cases_from_df(df_data)
    if not cases:
        gr.Warning("실행할 테스트 케이스가 없습니다.")
        return "", "", gr.update()

    _running = True
    log_lines: list[str] = []

    def _log(msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_lines.append(f"[{timestamp}] {msg}")

    _log(f"테스트 {len(cases)}개 실행 시작...")

    try:
        results = _run_coroutine_in_new_loop(
            _run_tests_async(cases, log_callback=_log),
            cleanup_timeout=2.0,
        )
    except Exception as e:
        _log(f"실행 중 오류 발생: {e}")
        _running = False
        return "\n".join(log_lines), f"오류 발생: {e}", list_existing_reports()

    _running = False

    # Build summary
    if len(results) == 1 and results[0].get("report_md"):
        summary_md = results[0]["report_md"]
    elif len(results) > 1:
        summary_md = generate_summary_report(results)
        # Append individual reports
        for r in results:
            if r.get("report_md"):
                summary_md += f"\n\n---\n\n{r['report_md']}"
    else:
        summary_md = "결과 없음"

    _log("모든 테스트 완료!")
    return "\n".join(log_lines), summary_md, list_existing_reports()


# ---------------------------------------------------------------------------
# Default test cases for the table
# ---------------------------------------------------------------------------
def get_default_test_cases() -> list:
    """test_cases.yaml 에서 기본값 로드 (없으면 빈 행)"""
    default_path = Path("test_cases.yaml")
    if default_path.exists():
        try:
            data = yaml.safe_load(default_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "test_cases" in data:
                return [
                    [tc.get("name", ""), tc.get("goal", "")]
                    for tc in data["test_cases"]
                ]
        except Exception:
            pass
    return [["", ""]]


# ---------------------------------------------------------------------------
# Build Gradio UI
# ---------------------------------------------------------------------------
def create_app() -> gr.Blocks:
    with gr.Blocks(
        title="QA 자동화 테스트",
        theme=gr.themes.Soft(),
        css="""
        .report-md { max-height: 700px; overflow-y: auto; }
        .log-area { font-family: monospace; font-size: 13px; }
        """,
    ) as app:
        gr.Markdown("# QA 자동화 테스트 대시보드")

        with gr.Tabs():
            # ============ Tab 1: 테스트 작성 & 실행 ============
            with gr.Tab("테스트 작성 & 실행"):
                gr.Markdown("### 테스트 케이스 편집")
                gr.Markdown(
                    "테스트 이름과 Goal을 입력하세요. "
                    "행을 추가/삭제하여 여러 테스트를 한번에 관리할 수 있습니다."
                )

                with gr.Row():
                    upload_btn = gr.File(
                        label="YAML/JSON 파일 불러오기",
                        file_types=[".yaml", ".yml", ".json"],
                        type="filepath",
                    )
                    export_btn = gr.Button("YAML로 내보내기", variant="secondary")

                test_table = gr.Dataframe(
                    headers=["테스트명", "Goal"],
                    datatype=["str", "str"],
                    value=get_default_test_cases(),
                    col_count=(2, "fixed"),
                    row_count=(1, "dynamic"),
                    interactive=True,
                    wrap=True,
                )

                export_file = gr.File(label="내보낸 파일", visible=False)

                run_btn = gr.Button(
                    "테스트 실행",
                    variant="primary",
                    size="lg",
                )

                gr.Markdown("### 실행 로그")
                log_output = gr.Textbox(
                    label="로그",
                    lines=12,
                    max_lines=30,
                    interactive=False,
                    elem_classes=["log-area"],
                )

                gr.Markdown("### 테스트 결과 리포트")
                result_report = gr.Markdown(
                    value="*테스트를 실행하면 여기에 결과가 표시됩니다.*",
                    elem_classes=["report-md"],
                )

            # ============ Tab 2: 리포트 조회 ============
            with gr.Tab("리포트 조회"):
                gr.Markdown("### 저장된 리포트 목록")
                gr.Markdown("리포트를 클릭하면 내용을 확인할 수 있습니다.")

                refresh_btn = gr.Button("새로고침", variant="secondary")

                report_table = gr.Dataframe(
                    headers=["파일명", "생성 시간"],
                    datatype=["str", "str"],
                    value=list_existing_reports(),
                    col_count=(2, "fixed"),
                    interactive=False,
                    wrap=True,
                )

                saved_report_md = gr.Markdown(
                    value="*리포트를 선택하세요.*",
                    elem_classes=["report-md"],
                )

        # ============ Event bindings ============

        # 파일 업로드 -> 테이블 갱신
        upload_btn.change(
            fn=load_yaml_file,
            inputs=[upload_btn],
            outputs=[test_table],
        )

        # YAML 내보내기
        export_btn.click(
            fn=export_yaml,
            inputs=[test_table],
            outputs=[export_file],
        ).then(
            fn=lambda f: gr.update(visible=True) if f else gr.update(visible=False),
            inputs=[export_file],
            outputs=[export_file],
        )

        # 테스트 실행
        run_btn.click(
            fn=run_tests,
            inputs=[test_table],
            outputs=[log_output, result_report, report_table],
        )

        # 리포트 목록 새로고침
        refresh_btn.click(
            fn=list_existing_reports,
            outputs=[report_table],
        )

        # 리포트 테이블 클릭 -> 내용 표시
        report_table.select(
            fn=on_report_select,
            inputs=[report_table],
            outputs=[saved_report_md],
        )

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app = create_app()
    app.launch(server_name="0.0.0.0", server_port=7860, share=False)
