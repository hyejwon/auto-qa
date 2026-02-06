# qa_agent/main.py
import asyncio
import json
import logging
import argparse
import os
import yaml
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

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

def load_test_cases(file_path: str, case_types: List[str] = None) -> List[Dict[str, Any]]:
    """
    YAML 또는 JSON 파일에서 테스트 케이스 로드

    Args:
        file_path: 테스트 케이스 파일 경로
        case_types: 필터링할 케이스 타입 리스트 (None이면 전체 로드)
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"테스트 케이스 파일을 찾을 수 없습니다: {file_path}")

    content = path.read_text(encoding="utf-8")

    if path.suffix in (".yaml", ".yml"):
        data = yaml.safe_load(content)
    elif path.suffix == ".json":
        data = json.loads(content)
    else:
        raise ValueError(f"지원하지 않는 파일 형식: {path.suffix} (yaml, yml, json만 지원)")

    # 단일 goal 문자열 리스트도 지원
    if isinstance(data, list):
        if all(isinstance(item, str) for item in data):
            test_cases = [{"name": f"테스트 {i+1}", "goal": goal} for i, goal in enumerate(data)]
        else:
            test_cases = data
    # test_cases 키가 있는 경우
    elif isinstance(data, dict) and "test_cases" in data:
        test_cases = data["test_cases"]
    else:
        raise ValueError("테스트 케이스 형식이 올바르지 않습니다.")

    # case_type 필터링
    if case_types:
        test_cases = [tc for tc in test_cases if tc.get("case_type") in case_types]

    return test_cases


def get_available_case_types(file_path: str) -> List[str]:
    """테스트 케이스 파일에서 사용 가능한 case_type 목록 추출"""
    path = Path(file_path)
    if not path.exists():
        return []

    content = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        data = yaml.safe_load(content)
    elif path.suffix == ".json":
        data = json.loads(content)
    else:
        return []

    test_cases = data.get("test_cases", []) if isinstance(data, dict) else data
    case_types = set()
    for tc in test_cases:
        if isinstance(tc, dict) and tc.get("case_type"):
            case_types.add(tc["case_type"])
    return sorted(list(case_types))


def generate_summary_report(results: List[Dict[str, Any]], output_dir: Path) -> str:
    """여러 테스트 결과를 요약한 리포트 생성"""
    md = []
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    total = len(results)
    passed = sum(1 for r in results if r["status"] == "done")
    failed = total - passed

    md.append("# 📊 QA 자동화 테스트 요약 리포트")
    md.append("")
    md.append(f"> **실행 시간:** {timestamp}")
    md.append("")

    # 전체 결과
    if failed == 0:
        md.append(f"> ### ✅ **전체 통과: {passed}/{total}**")
    else:
        md.append(f"> ### ⚠️ **{failed}개 실패: {passed}/{total} 통과**")
    md.append("")

    # 결과 테이블
    md.append("## 📋 테스트 결과")
    md.append("")
    md.append("| # | 테스트명 | 결과 | 리포트 |")
    md.append("|---|----------|------|--------|")

    for idx, r in enumerate(results, 1):
        status_icon = "✅" if r["status"] == "done" else "❌"
        report_link = f"[상세보기]({r['report_file']})" if r.get("report_file") else "-"
        md.append(f"| {idx} | {r['name']} | {status_icon} | {report_link} |")

    md.append("")

    # 실패한 테스트 상세
    failed_tests = [r for r in results if r["status"] != "done"]
    if failed_tests:
        md.append("## ❌ 실패한 테스트")
        md.append("")
        for r in failed_tests:
            md.append(f"### {r['name']}")
            md.append("")
            md.append(f"**Goal:** {r.get('goal', 'N/A')}")
            md.append("")
            if r.get("error"):
                md.append(f"**에러:** `{r['error'][:200]}`")
            md.append("")

    md.append("---")
    md.append("")
    md.append("*QA 자동화 시스템에서 생성된 요약 리포트입니다.*")

    return "\n".join(md)

async def planner_node(state: AgentState, llm: ChatGoogleGenerativeAI):
    tools_schema = state.get("tools_schema", [])
    plan = await make_plan(llm, state.get("goal",""), tools_schema)
    logging.getLogger("qa_agent.planner.planner").info(
        f"PLANNED: {json.dumps(plan, ensure_ascii=False)[:2000]}"
    )

    return {
        "plan": plan,
        "goal_steps": plan.get("steps", []),
        "current_step_idx": 0,
        "mode": "executing",
        "status": "running",
        "messages": state.get("messages", []) + [{"role":"assistant","content": f"PLANNED: {json.dumps(plan, ensure_ascii=False)[:2000]}"}],
    }

async def replan_node(state: AgentState, llm: ChatGoogleGenerativeAI):
    # 간단히 같은 플래너 재사용 + 실패 힌트 넣고 싶으면 make_plan 프롬프트에 recent trace 요약을 추가하면 됨
    state["mode"] = "planning"
    return await planner_node(state, llm)

async def executor_node(state: AgentState):
    # 실행만 전담
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
) -> Dict[str, Any]:
    """단일 테스트 케이스 실행"""
    print(f"\n{'='*60}")
    print(f"🧪 테스트 시작: {test_name}")
    print(f"📝 Goal: {goal[:80]}...")
    print(f"{'='*60}\n")

    init_state: AgentState = {
        "mode": "planning",
        "goal": goal,
        "plan": {},
        "goal_steps": [],
        "current_step_idx": 0,
        "messages": [{"role": "user", "content": "시작해줘. 필요한 도구를 호출해서 목표를 달성해."}],
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
        final_state = await graph.ainvoke(init_state, config={"recursion_limit": 500})

        # 개별 리포트 저장
        report_filename = None
        if final_state.get("status") in ("done", "error"):
            md = generate_report(final_state)
            safe_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in test_name)
            report_filename = f"qa_report_{safe_name}_{datetime.now().strftime('%H%M%S')}.md"
            report_path = output_dir / report_filename
            report_path.write_text(md, encoding="utf-8")
            print(f"📄 리포트 저장: {report_path}")

        status = final_state.get("status", "unknown")
        status_icon = "✅" if status == "done" else "❌"
        print(f"\n{status_icon} 테스트 완료: {test_name} - {status.upper()}")

        return {
            "name": test_name,
            "goal": goal,
            "status": status,
            "error": final_state.get("error", ""),
            "report_file": report_filename,
        }
    except Exception as e:
        logging.exception(f"테스트 실행 중 예외 발생: {test_name}")
        return {
            "name": test_name,
            "goal": goal,
            "status": "error",
            "error": str(e),
            "report_file": None,
        }


async def main():
    parser = argparse.ArgumentParser(description="QA 자동화 테스트 실행")
    parser.add_argument(
        "-f", "--file",
        help="테스트 케이스 파일 (YAML 또는 JSON). 환경변수 TEST_CASES_FILE로도 지정 가능",
    )
    parser.add_argument(
        "-g", "--goal",
        help="단일 goal 직접 지정 (파일 대신 사용)",
    )
    parser.add_argument(
        "-o", "--output",
        default="./reports",
        help="리포트 저장 디렉토리 (기본: ./reports)",
    )
    parser.add_argument(
        "-t", "--type",
        default=os.environ.get("TEST_CASE_TYPE"),
        help="실행할 테스트 케이스 타입 (콤마로 구분, 예: login,settings). 환경변수 TEST_CASE_TYPE으로도 지정 가능",
    )
    parser.add_argument(
        "--list-types",
        action="store_true",
        help="테스트 케이스 파일에서 사용 가능한 타입 목록 출력 후 종료",
    )
    args = parser.parse_args()

    # --list-types 옵션 처리
    if args.list_types:
        if args.file:
            types = get_available_case_types(args.file)
            if types:
                print(f"📋 사용 가능한 테스트 타입: {', '.join(types)}")
            else:
                print("⚠️ 테스트 케이스에 case_type이 정의되어 있지 않습니다.")
        else:
            print("⚠️ --file 옵션으로 테스트 케이스 파일을 지정해주세요.")
        return

    cfg = QAConfig()

    # Configure logging based on debug flag
    logging.basicConfig(
        level=logging.DEBUG if cfg.debug else logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    if cfg.debug:
        print("🐛 DEBUG mode enabled")

    # case_types 파싱
    case_types = None
    if args.type:
        case_types = [t.strip() for t in args.type.split(",") if t.strip()]

    # 테스트 케이스 준비
    if args.file:
        test_cases = load_test_cases(args.file, case_types=case_types)
        type_info = f" (타입: {', '.join(case_types)})" if case_types else ""
        print(f"📂 테스트 케이스 파일 로드: {args.file} ({len(test_cases)}개){type_info}")
    elif args.goal:
        test_cases = [{"name": "CLI 테스트", "goal": args.goal}]
    else:
        # 기본 테스트 (하드코딩된 goal)
        test_cases = [{
            "name": "게스트 테스트",
            "goal": "package_name: com.percent.aos.cooptd 실행 → 게스트 로그인 버튼 클릭→ 동의합니다 클릭"}]
        

        # test_cases = load_test_cases("test_cases.yaml")
    
    # 출력 디렉토리 생성
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    llm = ChatGoogleGenerativeAI(
        model=cfg.model,
        project=cfg.project,
        location=cfg.location,
    )

    # 환경변수를 MCP 서버에 전달 (ADB_SERVER_SOCKET 등)
    server_params = StdioServerParameters(
        command=cfg.mcp_command,
        args=cfg.mcp_args,
        env=os.environ.copy()
    )
    results = []

    try:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=30.0)
                lc_tools = await asyncio.wait_for(load_mcp_tools(session), timeout=60.0)

                tool_name_map = {t.name: t for t in lc_tools}
                tools_schema = lc_tools_to_openai_schema(lc_tools)

                # set_device 선 호출(옵션)
                set_device_tool = tool_name_map.get("set_device")
                if set_device_tool:
                    if hasattr(set_device_tool, "ainvoke"):
                        await set_device_tool.ainvoke({"device_id": cfg.device_id})
                    else:
                        set_device_tool.invoke({"device_id": cfg.device_id})

                # build graph with closures
                async def planner_wrapper(state: AgentState):
                    return await planner_node(state, llm)

                async def replan_wrapper(state: AgentState):
                    return await replan_node(state, llm)

                graph = build_graph(
                    planner_node=planner_wrapper,
                    replan_node=replan_wrapper,
                    executor_node=executor_node
                )

                # 각 테스트 케이스 실행
                for idx, tc in enumerate(test_cases, 1):
                    test_name = tc.get("name", f"테스트 {idx}")
                    goal = tc.get("goal", "")

                    if not goal:
                        print(f"⚠️ 건너뜀: {test_name} (goal이 비어있음)")
                        continue

                    result = await run_single_test(
                        goal=goal,
                        test_name=test_name,
                        cfg=cfg,
                        tools_schema=tools_schema,
                        tool_name_map=tool_name_map,
                        graph=graph,
                        output_dir=output_dir,
                    )
                    results.append(result)

                # 여러 테스트 실행 시 요약 리포트 생성
                if len(results) > 1:
                    summary_md = generate_summary_report(results, output_dir)
                    summary_path = output_dir / f"qa_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
                    summary_path.write_text(summary_md, encoding="utf-8")
                    print(f"\n📊 요약 리포트 저장: {summary_path}")

                # 최종 결과 출력
                print(f"\n{'='*60}")
                print("🏁 전체 테스트 완료")
                print(f"{'='*60}")
                passed = sum(1 for r in results if r["status"] == "done")
                print(f"✅ 통과: {passed}/{len(results)}")
                if passed < len(results):
                    print(f"❌ 실패: {len(results) - passed}/{len(results)}")
                    for r in results:
                        if r["status"] != "done":
                            print(f"   - {r['name']}: {r.get('error', 'Unknown error')[:50]}")

    except* BrokenResourceError:
        logging.warning("MCP stdio connection closed with BrokenResourceError (ignored).")

if __name__ == "__main__":
    asyncio.run(main())
