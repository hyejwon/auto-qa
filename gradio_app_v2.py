# gradio_app.py
"""
QA 자동화 테스트 - Gradio 인터페이스
사용법: python gradio_app.py

변경사항(요청 반영):
- 타입별로 고정 에뮬레이터(디바이스) 분리:
  - setting -> 127.0.0.1:5555
  - login account -> 127.0.0.1:5565
- 탭 2개로 분리(각 탭에서 실행/로그/결과/녹화목록/비디오 다운로드 가능)
- 리포트 조회 탭 삭제(리포트 파일 저장은 기존대로 reports/에 남김)
- 녹화 파일명에 device_id prefix를 붙여 디바이스별 목록 필터링 가능
"""
import asyncio
import inspect
import json
import logging
import os
import time
import yaml
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

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
# Device reservation (per-session)
# ---------------------------------------------------------------------------
_device_reservations: Dict[str, Dict[str, Any]] = {}
# { device_id: {"session": "<session_hash>", "ts": <epoch>} }

_RESERVATION_TTL_SEC = 60 * 30  # 30 minutes



def get_test_case_name_choices(case_type: str) -> List[str]:
    """타입에 해당하는 테스트 케이스 name 목록"""
    cases = get_test_cases_by_types([] if (not case_type or case_type == "전체") else [case_type])
    names = [tc.get("name", "") for tc in cases if tc.get("name")]
    return names


def get_test_case_by_name(case_type: str, test_name: str) -> Optional[Dict[str, Any]]:
    """타입+이름으로 케이스 1개 찾기"""
    cases = get_test_cases_by_types([] if (not case_type or case_type == "전체") else [case_type])
    for tc in cases:
        if tc.get("name") == test_name:
            return tc
    return None


def update_test_dropdown(case_type: str):
    """타입 변경 시 테스트 케이스 드롭다운 choices 갱신"""
    names = get_test_case_name_choices(case_type)
    return gr.update(choices=names, value=(names[0] if names else None))


async def adb_forward_37772(device_id: str, log_callback=None) -> bool:
    """adb forward tcp:37772 tcp:37772"""
    try:
        proc = await _adb_exec(device_id, "forward", "tcp:37772", "tcp:37772")
        out, err = await proc.communicate()
        if proc.returncode != 0:
            if log_callback:
                log_callback(f"⚠️ adb forward 실패: {err.decode(errors='ignore')[:200]}")
            return False
        if log_callback:
            log_callback("✅ adb forward tcp:37772 설정 완료")
        return True
    except Exception as e:
        if log_callback:
            log_callback(f"⚠️ adb forward 예외: {e}")
        return False
async def adb_uninstall_install_login_app(device_id: str, apk_path: str, package_name: str, log_callback=None) -> bool:
    """login 디바이스에서 앱 삭제 후 재설치"""
    try:
        # uninstall (없어도 실패할 수 있으니 returncode 무시 가능)
        p1 = await _adb_exec(device_id, "uninstall", package_name)
        out1, err1 = await p1.communicate()
        if log_callback:
            log_callback(f"🧹 uninstall 결과: {out1.decode(errors='ignore').strip() or err1.decode(errors='ignore').strip()}")

        # install
        p2 = await _adb_exec(device_id, "install", apk_path)
        out2, err2 = await p2.communicate()
        if p2.returncode != 0:
            if log_callback:
                log_callback(f"⚠️ install 실패: {err2.decode(errors='ignore')[:300]}")
            return False

        if log_callback:
            log_callback("✅ APK 재설치 완료")
        return True

    except Exception as e:
        if log_callback:
            log_callback(f"⚠️ uninstall/install 예외: {e}")
        return False
    
def _cleanup_expired_reservations():
    now = time.time()
    expired = []
    for dev, info in _device_reservations.items():
        if now - info.get("ts", 0) > _RESERVATION_TTL_SEC:
            expired.append(dev)
    for dev in expired:
        _device_reservations.pop(dev, None)


def reserve_device(device_id: str, session_hash: str) -> Tuple[bool, str]:
    _cleanup_expired_reservations()
    info = _device_reservations.get(device_id)
    if info and info.get("session") != session_hash:
        return False, f"❌ {device_id} 는 다른 사용자가 사용 중입니다."
    _device_reservations[device_id] = {"session": session_hash, "ts": time.time()}
    return True, f"✅ {device_id} 예약 완료"


def release_device(device_id: str, session_hash: str) -> str:
    info = _device_reservations.get(device_id)
    if not info:
        return "ℹ️ 예약 없음"
    if info.get("session") != session_hash:
        return "⚠️ 다른 세션이 예약한 디바이스라 해제 불가"
    _device_reservations.pop(device_id, None)
    return f"✅ {device_id} 해제 완료"


# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
REPORTS_DIR = Path("./reports")
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

RECORDINGS_DIR = Path("./recordings")
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

TEST_CASES_FILE = Path(os.environ.get("TEST_CASES_FILE", "test_cases.yaml"))

# 디바이스별 상태 관리 (동시 실행 지원)
# {device_id: {"running": bool, "recording_process": Process | None}}
_device_state: Dict[str, Dict[str, Any]] = {}

_DEVICE_RECORDING_DIR = "/sdcard/qa_recordings"


def _get_device_state(device_id: str) -> Dict[str, Any]:
    """디바이스별 상태 딕셔너리 반환 (없으면 생성)"""
    if device_id not in _device_state:
        _device_state[device_id] = {"running": False, "recording_process": None}
    return _device_state[device_id]


async def _maybe_close_llm(llm) -> None:
    """Best-effort close for LangChain Google GenAI chat models."""
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
# ADB Screen Recording (세그먼트 분할 녹화 - 3분 제한 우회)
# ---------------------------------------------------------------------------
async def _adb_exec(device_id: str, *args) -> asyncio.subprocess.Process:
    """adb -s <device> <args...> 를 실행하는 헬퍼"""
    return await asyncio.create_subprocess_exec(
        "adb",
        "-s",
        device_id,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )


async def start_adb_recording(device_id: str, log_callback=None) -> bool:
    """3분(180초)마다 개별 mp4 저장 방식"""
    state = _get_device_state(device_id)

    if state.get("recording_task") is not None:
        if log_callback:
            log_callback("이미 녹화 중입니다.")
        return False

    state["recording_stop"] = asyncio.Event()

    async def _log(msg):
        if log_callback:
            log_callback(msg)

    async def _record_loop():
        i = 0
        await _log("📹 화면 녹화 시작 (3분 단위 저장)")

        while not state["recording_stop"].is_set():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_dev = "".join(c if c.isalnum() else "_" for c in device_id)

            device_path = f"/sdcard/seg_{i:03d}.mp4"
            local_filename = f"{safe_dev}_recording_{timestamp}.mp4"
            local_path = RECORDINGS_DIR / local_filename

            proc = await asyncio.create_subprocess_exec(
                "adb", "-s", device_id, "shell",
                "screenrecord",
                "--time-limit", "180",
                "--bit-rate", "2000000",
                device_path,
            )

            state["recording_process"] = proc

            try:
                await asyncio.wait_for(proc.wait(), timeout=190.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

            state["recording_process"] = None

            # pull
            pull = await _adb_exec(device_id, "pull", device_path, str(local_path))
            await asyncio.wait_for(pull.wait(), timeout=60.0)

            # 디바이스 파일 삭제
            rm = await _adb_exec(device_id, "shell", "rm", device_path)
            await rm.wait()

            if local_path.exists():
                size_mb = local_path.stat().st_size / (1024 * 1024)
                await _log(f"💾 저장 완료: {local_filename} ({size_mb:.1f}MB)")

            i += 1

    state["recording_task"] = asyncio.create_task(_record_loop())
    return True


async def stop_adb_recording(device_id: str, test_name: str, log_callback=None) -> Optional[str]:
    state = _get_device_state(device_id)

    if not state.get("recording_task"):
        return None

    if log_callback:
        log_callback("⛔ 녹화 중지 중...")

    state["recording_stop"].set()

    # 현재 실행 중인 screenrecord 종료
    try:
        kill = await _adb_exec(
            device_id,
            "shell",
            "pidof screenrecord 2>/dev/null | tr ' ' '\\n' | xargs -r kill -INT; true",
        )
        await kill.wait()
    except Exception:
        pass

    try:
        await asyncio.wait_for(state["recording_task"], timeout=10.0)
    except Exception:
        pass

    state["recording_task"] = None
    state["recording_process"] = None

    if log_callback:
        log_callback("✅ 녹화 중지 완료")

    return None

def get_recent_recordings(limit: int = 20, device_id: Optional[str] = None) -> List[List[str]]:
    """최근 녹화 파일 목록 반환 (device_id 지정 시 해당 디바이스 prefix만)"""
    recordings = sorted(
        RECORDINGS_DIR.glob("*.mp4"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    prefix = None
    if device_id:
        safe_dev = "".join(c if c.isalnum() else "_" for c in device_id)
        prefix = f"{safe_dev}_"

    rows: List[List[str]] = []
    for r in recordings:
        if prefix and not r.name.startswith(prefix):
            continue
        mtime = datetime.fromtimestamp(r.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        size_mb = r.stat().st_size / (1024 * 1024)
        rows.append([r.name, f"{size_mb:.1f} MB", mtime])
        if len(rows) >= limit:
            break
    return rows


# ---------------------------------------------------------------------------
# Test Case Loading
# ---------------------------------------------------------------------------
def load_test_cases_from_file(file_path: Path = None) -> Dict[str, Any]:
    """YAML 파일에서 테스트 케이스 로드 및 타입별 분류"""
    file_path = file_path or TEST_CASES_FILE

    if not file_path.exists():
        return {"all": [], "by_type": {}, "types": []}

    content = file_path.read_text(encoding="utf-8")
    data = yaml.safe_load(content)

    test_cases = []
    if isinstance(data, dict) and "test_cases" in data:
        test_cases = data["test_cases"]
    elif isinstance(data, list):
        test_cases = data

    by_type: Dict[str, List] = {}
    for tc in test_cases:
        case_type = tc.get("case_type", "기타")
        if case_type not in by_type:
            by_type[case_type] = []
        by_type[case_type].append(tc)

    return {"all": test_cases, "by_type": by_type, "types": sorted(by_type.keys())}


def get_test_cases_by_types(selected_types: List[str]) -> List[Dict[str, Any]]:
    """선택된 타입에 해당하는 테스트 케이스 반환"""
    data = load_test_cases_from_file()
    if not selected_types:
        return data["all"]

    cases = []
    for t in selected_types:
        cases.extend(data["by_type"].get(t, []))
    return cases


def on_type_select(selected_type: str) -> List[List[str]]:
    """타입 1개 미리보기 테이블용"""
    if not selected_type:
        cases = get_test_cases_by_types([])
    else:
        cases = get_test_cases_by_types([selected_type])

    # return [
    #     [tc.get("case_type", ""), tc.get("name", ""), (tc.get("goal", "")[:100] + "...")]
    #     for tc in cases
    # ]
    return [
        [tc.get("case_type", ""), tc.get("name", ""), (tc.get("goal", ""))]
        for tc in cases
    ]


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
    """단일 테스트 케이스 실행"""

    def _log(msg: str):
        if log_callback:
            log_callback(msg)

    _log(f"\n🧪 테스트: {test_name}")
    _log(f"   📝 {goal[:80]}...")

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
        final_state = await graph.ainvoke(init_state, config={"recursion_limit": 500})

        report_filename = None
        md_content = ""
        if final_state.get("status") in ("done", "error"):
            md_content = generate_report(final_state)
            safe_name = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in test_name)
            report_filename = f"qa_report_{safe_name}_{datetime.now().strftime('%H%M%S')}.md"
            report_path = output_dir / report_filename
            report_path.write_text(md_content, encoding="utf-8")

        status = final_state.get("status", "unknown")
        if status == "done":
            _log(f"✅ {test_name}: 통과")
        else:
            error_msg = final_state.get("error", "알 수 없는 오류")[:50]
            _log(f"❌ {test_name}: 실패 - {error_msg}")

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
        _log(f"❌ {test_name}: 오류 발생 - {str(e)[:50]}")
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
        md.append(f"> ### ✅ **전체 통과: {passed}/{total}**")
    else:
        md.append(f"> ### ⚠️ **{failed}개 실패: {passed}/{total} 통과**")
    md.append("")
    md.append("| # | 테스트명 | 결과 | 에러 |")
    md.append("|---|----------|------|------|")
    for idx, r in enumerate(results, 1):
        status_icon = "✅" if r["status"] == "done" else "❌"
        error_short = (r.get("error", "") or "")[:60]
        md.append(f"| {idx} | {r['name']} | {status_icon} | {error_short} |")
    md.append("")
    return "\n".join(md)


# ---------------------------------------------------------------------------
# Custom Log Handler for Gradio (User-Friendly)
# ---------------------------------------------------------------------------
class GradioLogHandler(logging.Handler):
    """로그를 사용자 친화적 형식으로 변환하여 Gradio에 전달하는 핸들러"""

    TOOL_NAMES = {
        "mobile_launch_app": "앱 실행",
        "mobile_terminate_app": "앱 종료",
        "take_screenshot": "화면 캡처",
        "smart_click": "버튼 클릭",
        "smart_find": "요소 찾기",
        "adb_press_button": "하드웨어 버튼",
        "unity_click_button": "Unity 버튼 클릭",
        "unity_find_buttons": "Unity 버튼 탐색",
        "unity_scroll": "스크롤",
        "uiauto_click": "UI 요소 클릭",
        "set_device": "디바이스 설정",
        "list_devices": "디바이스 목록",
        "get_foreground_app": "현재 앱 확인",
    }

    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        self.current_step = 0
        self.total_steps = 0

    def _format_user_friendly(self, msg: str) -> Optional[str]:
        import re

        if "PLANNED:" in msg:
            return "🧠 플래너 계획 생성 완료"

        step_match = re.search(r"\[Step (\d+)/(\d+)\] Tool: (\w+)", msg)
        if step_match:
            self.current_step = int(step_match.group(1))
            self.total_steps = int(step_match.group(2))
            tool_name = step_match.group(3)
            friendly_name = self.TOOL_NAMES.get(tool_name, tool_name)
            progress = f"[{self.current_step}/{self.total_steps}]"
            return f"\n{'─'*40}\n📌 {progress} {friendly_name}"

        if "Goal:" in msg:
            goal = msg.split("Goal:")[-1].strip()
            return f"   목표: {goal}"

        if "Invoking" in msg:
            return None

        if "completed with status: success" in msg:
            return "   ✅ 성공"
        if "completed with status: error" in msg:
            return "   ❌ 실패"
        if "Error:" in msg:
            error = msg.split("Error:")[-1].strip()[:100]
            return f"   ⚠️ 오류: {error}"

        if "completed successfully" in msg and "Step" in msg:
            return None

        if "Moving to step" in msg:
            return None

        if "Waiting" in msg:
            wait_match = re.search(r"Waiting ([\d.]+)s", msg)
            if wait_match:
                return f"   ⏳ 대기 중... ({wait_match.group(1)}초)"

        if "All" in msg and "steps completed" in msg:
            return f"\n🎉 모든 단계 완료!"

        if "retrying" in msg.lower():
            retry_match = re.search(r"\((\d+)/(\d+)\)", msg)
            if retry_match:
                return f"   🔄 재시도 중... ({retry_match.group(1)}/{retry_match.group(2)})"

        if "replan" in msg.lower():
            return "   🔄 계획 재수립 중..."

        skip_patterns = [
            "DEBUG",
            "httpx",
            "httpcore",
            "urllib3",
            "Arguments:",
            "sanitize",
            "={'",
            "📋",
        ]
        if any(skip in msg for skip in skip_patterns):
            return None

        return None

    def emit(self, record):
        if self.callback:
            try:
                msg = record.getMessage()
                user_msg = self._format_user_friendly(msg)
                if user_msg:
                    self.callback(user_msg)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Async runner: MCP session + test execution
# ---------------------------------------------------------------------------
async def _run_tests_async(
    test_cases: List[Dict[str, Any]],
    device_id: str,
    log_callback=None,
) -> List[Dict[str, Any]]:
    """MCP 세션을 열고 테스트 케이스 목록을 실행"""
    cfg = QAConfig()
    cfg.device_id = device_id

    logging.basicConfig(
        level=logging.DEBUG if cfg.debug else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    gradio_handler = None
    if log_callback:
        gradio_handler = GradioLogHandler(log_callback)
        gradio_handler.setLevel(logging.INFO)

        executor_logger = logging.getLogger("qa_agent.executor.executor")
        executor_logger.addHandler(gradio_handler)
        executor_logger.setLevel(logging.INFO)

        planner_logger = logging.getLogger("qa_agent.planner.planner")
        planner_logger.addHandler(gradio_handler)
        planner_logger.setLevel(logging.INFO)

    def _log(msg):
        if log_callback:
            log_callback(msg)

    llm = ChatGoogleGenerativeAI(
        model=cfg.model,
        project=cfg.project,
        location=cfg.location,
    )

    server_params = StdioServerParameters(
        command=cfg.mcp_command,
        args=cfg.mcp_args,
        env=os.environ.copy(),
    )

    results: List[Dict[str, Any]] = []

    try:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=30.0)
                lc_tools = await asyncio.wait_for(load_mcp_tools(session), timeout=60.0)

                tool_name_map = {t.name: t for t in lc_tools}
                tools_schema = lc_tools_to_openai_schema(lc_tools)

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

                    await start_adb_recording(cfg.device_id, _log)

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

                    rec_path = await stop_adb_recording(cfg.device_id, test_name, _log)
                    if rec_path:
                        result["recording_file"] = rec_path

                    results.append(result)

    except* BrokenResourceError:
        _log("MCP 연결이 종료되었습니다 (BrokenResourceError).")

    finally:
        await _maybe_close_llm(llm)

        state = _get_device_state(cfg.device_id)
        if state.get("recording_process") is not None:
            await stop_adb_recording(cfg.device_id, "cleanup", _log)

        if gradio_handler:
            executor_logger = logging.getLogger("qa_agent.executor.executor")
            executor_logger.removeHandler(gradio_handler)
            planner_logger = logging.getLogger("qa_agent.planner.planner")
            planner_logger.removeHandler(gradio_handler)

    return results


# ---------------------------------------------------------------------------
# Gradio helpers
# ---------------------------------------------------------------------------
def on_recording_select(evt: gr.SelectData, rec_table):
    """녹화 테이블 클릭 시 파일 경로 반환 (다운로드용)"""
    if evt.index is None:
        return None

    import pandas as pd

    if isinstance(rec_table, pd.DataFrame):
        rec_table = rec_table.values.tolist()

    row_idx = evt.index[0] if isinstance(evt.index, (list, tuple)) else evt.index
    if rec_table and 0 <= row_idx < len(rec_table):
        filename = rec_table[row_idx][0]
        path = RECORDINGS_DIR / filename
        if path.exists():
            return str(path)
    return None


# ---------------------------------------------------------------------------
# NEW: Type-fixed runner (각 탭에서 타입/디바이스 고정 실행)
# ---------------------------------------------------------------------------
async def run_tests_for_type(
    fixed_type: str,
    fixed_device: str,
    selected_test_name: str,
    session_running: bool,
    progress=gr.Progress(track_tqdm=True),
    request: gr.Request = None,
):
    session_hash = getattr(request, "session_hash", None) or "unknown"

    def _log(msg2: str):
        try:
            q.put_nowait(msg2)
        except Exception:
            pass
    if session_running:
        gr.Warning("이미 이 세션에서 테스트가 실행 중입니다.")
        yield "", "*실행 중...*", gr.update(), gr.update(), True
        return
    
    if not selected_test_name:
        gr.Warning("테스트 케이스를 하나 선택해주세요.")
        yield "", "*실행 중...*", gr.update(), gr.update(), False
        return

    tc = get_test_case_by_name(fixed_type, selected_test_name)
    if not tc:
        gr.Warning("선택한 테스트 케이스를 찾을 수 없습니다.")
        yield "", "*실행 중...*", gr.update(), gr.update(), False
        return

    cases = [tc]
        
    ok, msg = reserve_device(fixed_device, session_hash)
    if not ok:
        gr.Warning(msg)
        yield "", "*실행 중...*", gr.update(), gr.update(), False
        return

    # ✅ (1) 디바이스 전환 시 초기 세팅: forward
    await adb_forward_37772(fixed_device, log_callback=_log)

    # ✅ (2) login test(5565)만 uninstall/install
    # fixed_type 문자열은 실제 YAML case_type에 맞춰야 함
    if fixed_type == "login" and fixed_device == "127.0.0.1:5565":
        ok2 = await adb_uninstall_install_login_app(
            device_id=fixed_device,
            apk_path="apks/cooptd.apk",
            package_name="com.percent.aos.cooptd",
            log_callback=_log,
        )
        if not ok2:
            gr.Warning("Login 디바이스 APK 재설치 실패로 테스트를 중단합니다.")
            yield "", "*실행 중...*", gr.update(), gr.update(), False
            release_device(fixed_device, session_hash)
            return

    # cases = get_test_cases_by_types([fixed_type])
    if not cases:
        gr.Warning(f"'{fixed_type}' 타입 테스트 케이스가 없습니다.")
        yield "", "*실행 중...*", gr.update(), gr.update(), False
        release_device(fixed_device, session_hash)
        return

    


    log_lines: list[str] = []
    q: asyncio.Queue[str] = asyncio.Queue()

    
    
    ts = datetime.now().strftime("%H:%M:%S")
    _log(f"[{ts}] 🚀 테스트 시작 ({fixed_type} / {fixed_device})")
    _log(f"{'═'*40}")
    _log(f"📋 실행할 테스트: {len(cases)}개")
    for i, tc in enumerate(cases, 1):
        _log(f"   {i}. {tc.get('name', '이름 없음')}")
    _log(f"{'═'*40}")

    task = asyncio.create_task(_run_tests_async(cases, device_id=fixed_device, log_callback=_log))

    # 초기 UI
    yield (
        "\n".join(log_lines),
        "*실행 중...*",
        gr.update(value=get_recent_recordings(device_id=fixed_device)),
        gr.update(value=None),
        True,
    )

    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=0.2)
                log_lines.append(msg)
                yield "\n".join(log_lines), "*실행 중...*", gr.update(), gr.update(), True
            except asyncio.TimeoutError:
                if task.done():
                    break

        while not q.empty():
            log_lines.append(q.get_nowait())

        results = await task

        if len(results) == 1 and results[0].get("report_md"):
            summary_md = results[0]["report_md"]
        elif len(results) > 1:
            summary_md = generate_summary_report(results)
            for r in results:
                if r.get("report_md"):
                    summary_md += f"\n\n---\n\n{r['report_md']}"
        else:
            summary_md = "결과 없음"

        recording_files = [r.get("recording_file") for r in results if r.get("recording_file")]
        latest_recording = recording_files[-1] if recording_files else None

        yield (
            "\n".join(log_lines),
            summary_md,
            get_recent_recordings(device_id=fixed_device),
            latest_recording,
            False,
        )

    finally:
        release_device(fixed_device, session_hash)


# ---------------------------------------------------------------------------
# Build Gradio UI (tabs split, report tab removed)
# ---------------------------------------------------------------------------
def create_app() -> gr.Blocks:
    css = """
    .report-md { max-height: 600px; overflow-y: auto; }
    .log-area { font-family: monospace; font-size: 12px; }
    .log-area textarea {
        background-color: #1e1e1e !important;
        color: #d4d4d4 !important;
        font-family: 'Consolas', 'Monaco', monospace !important;
    }
    .test-table { max-height: 300px; overflow-y: auto; }
    """

    with gr.Blocks(title="QA 자동화 테스트", theme=gr.themes.Soft(), css=css) as app:
        gr.Markdown("# 🧪 QA 자동화 테스트 대시보드")
        gr.Markdown("타입별 고정 디바이스로 테스트 실행 + 녹화 다운로드")

        with gr.Tabs():
        # ===================== TAB 1: setting -> 5555 =====================
            with gr.Tab("⚙️ Setting 테스트 (5555)"):
                fixed_device_1 = "127.0.0.1:5555"
                session_running_1 = gr.State(False)
                fixed_device_state_1 = gr.State(fixed_device_1)

                # ✅ setting 탭에서 보여줄 타입 목록(원하면 추가 가능)
                type1_choices = ["settings"]
                default_type_1 = type1_choices[0]

                with gr.Row():
                    with gr.Column(scale=1):
                        gr.Markdown("### 실행 대상")

                        type_selector_1 = gr.Dropdown(
                            choices=type1_choices,
                            value=default_type_1,
                            label="타입 선택",
                        )

                        gr.Markdown(f"- 디바이스: **{fixed_device_1}**")

                        release_btn_1 = gr.Button("🔓 디바이스 해제", variant="secondary")
                        reserve_status_1 = gr.Markdown()

                        gr.Markdown("### 선택된 테스트 케이스")
                        test_name_dropdown_1 = gr.Dropdown(
                            choices=get_test_case_name_choices(default_type_1),
                            value=None,
                            label="실행할 테스트 케이스 (1개 선택)",
                        )
                        run_btn_1 = gr.Button("🚀 테스트 실행", variant="primary", size="lg")

                    with gr.Column(scale=2):
                        gr.Markdown("### 실행 로그")
                        log_output_1 = gr.Textbox(
                            label="로그",
                            lines=15,
                            max_lines=50,
                            interactive=False,
                            autoscroll=True,
                            elem_classes=["log-area"],
                        )

                        gr.Markdown("### 테스트 결과")
                        result_report_1 = gr.Markdown(
                            value="*테스트를 실행하면 여기에 결과가 표시됩니다.*",
                            elem_classes=["report-md"],
                        )

                        gr.Markdown("### 📹 녹화 영상")
                        recording_refresh_btn_1 = gr.Button("🔄 녹화 목록 새로고침", variant="secondary")
                        recording_table_1 = gr.Dataframe(
                            headers=["파일명", "크기", "녹화 시간"],
                            datatype=["str", "str", "str"],
                            value=get_recent_recordings(device_id=fixed_device_1),
                            col_count=(3, "fixed"),
                            interactive=False,
                            wrap=True,
                        )
                        video_download_1 = gr.File(label="녹화 파일 다운로드", interactive=False)

                        # 타입 변경 → 미리보기 갱신
                        type_selector_1.change(
                            fn=update_test_dropdown,
                            inputs=[type_selector_1],
                            outputs=[test_name_dropdown_1],
                        )

                        # 실행
                        run_btn_1.click(
                            fn=run_tests_for_type,
                            inputs=[type_selector_1, fixed_device_state_1, test_name_dropdown_1, session_running_1],
                            outputs=[log_output_1, result_report_1, recording_table_1, video_download_1, session_running_1],
                        )
                        def on_release_device_1(request: gr.Request):
                            session_hash = getattr(request, "session_hash", None) or "unknown"
                            return release_device(fixed_device_1, session_hash)

                        release_btn_1.click(fn=on_release_device_1, inputs=[], outputs=[reserve_status_1])

                        recording_refresh_btn_1.click(
                            fn=lambda: get_recent_recordings(device_id=fixed_device_1),
                            outputs=[recording_table_1],
                        )

                        recording_table_1.select(fn=on_recording_select, inputs=[recording_table_1], outputs=[video_download_1])

            # ===================== TAB 2: login account -> 5565 =====================
            with gr.Tab("🔐 Login account 테스트 (5565)"):
                type2_choices = ["login", "account"]  
                default_type_2 = type2_choices[0]
                fixed_device_2 = "127.0.0.1:5565"
                session_running_2 = gr.State(False)
                fixed_device_state_2 = gr.State(fixed_device_2)

                with gr.Row():
                    with gr.Column(scale=1):
                        gr.Markdown("### 실행 대상")
                        type_selector_2 = gr.Dropdown(
                            choices=type2_choices,
                            value=default_type_2,
                            label="타입 선택",
                        )
                        gr.Markdown(f"- 디바이스: **{fixed_device_2}**")
                

                        release_btn_2 = gr.Button("🔓 디바이스 해제", variant="secondary")
                        reserve_status_2 = gr.Markdown()

                        gr.Markdown("### 선택된 테스트 케이스")
                        test_name_dropdown_2 = gr.Dropdown(
                            choices=get_test_case_name_choices(default_type_2),
                            value=None,
                            label="실행할 테스트 케이스 (1개 선택)",
                        )
                        run_btn_2 = gr.Button("🚀 테스트 실행", variant="primary", size="lg")

                    with gr.Column(scale=2):
                        gr.Markdown("### 실행 로그")
                        log_output_2 = gr.Textbox(
                            label="로그",
                            lines=15,
                            max_lines=50,
                            interactive=False,
                            autoscroll=True,
                            elem_classes=["log-area"],
                        )

                        gr.Markdown("### 테스트 결과")
                        result_report_2 = gr.Markdown(
                            value="*테스트를 실행하면 여기에 결과가 표시됩니다.*",
                            elem_classes=["report-md"],
                        )

                        gr.Markdown("### 📹 녹화 영상")
                        recording_refresh_btn_2 = gr.Button("🔄 녹화 목록 새로고침", variant="secondary")
                        recording_table_2 = gr.Dataframe(
                            headers=["파일명", "크기", "녹화 시간"],
                            datatype=["str", "str", "str"],
                            value=get_recent_recordings(device_id=fixed_device_2),
                            col_count=(3, "fixed"),
                            interactive=False,
                            wrap=True,
                        )
                        video_download_2 = gr.File(label="녹화 파일 다운로드", interactive=False)
                
                type_selector_2.change(
                        fn=update_test_dropdown,
                        inputs=[type_selector_2],
                        outputs=[test_name_dropdown_2],
                    )

                run_btn_2.click(
                    fn=run_tests_for_type,
                    inputs=[type_selector_2, fixed_device_state_2, test_name_dropdown_2, session_running_2],
                    outputs=[log_output_2, result_report_2, recording_table_2, video_download_2, session_running_2],
                )


                def on_release_device_2(request: gr.Request):
                    session_hash = getattr(request, "session_hash", None) or "unknown"
                    return release_device(fixed_device_2, session_hash)

                release_btn_2.click(fn=on_release_device_2, inputs=[], outputs=[reserve_status_2])

                recording_refresh_btn_2.click(
                    fn=lambda: get_recent_recordings(device_id=fixed_device_2),
                    outputs=[recording_table_2],
                )

                recording_table_2.select(
                    fn=on_recording_select,
                    inputs=[recording_table_2],
                    outputs=[video_download_2],
                )

            # ===================== TAB 3: 설정(업로드만 유지) =====================
            with gr.Tab("⚙️ 설정"):
                gr.Markdown("### 테스트 환경 설정")
                cfg = QAConfig()

                with gr.Row():
                    with gr.Column():
                        gr.Markdown("**현재 설정**")
                        gr.Textbox(label="디바이스 ID(기본)", value=cfg.device_id, interactive=False)
                        gr.Textbox(label="패키지명", value=cfg.package_name, interactive=False)
                        gr.Textbox(label="LLM 모델", value=cfg.model, interactive=False)

                    with gr.Column():
                        gr.Markdown("**테스트 케이스 파일**")
                        gr.Textbox(label="현재 파일", value=str(TEST_CASES_FILE), interactive=False)
                        upload_yaml = gr.File(
                            label="새 테스트 케이스 파일 업로드 (YAML)",
                            file_types=[".yaml", ".yml"],
                            type="filepath",
                        )

                def on_upload_yaml(file_obj):
                    if file_obj:
                        global TEST_CASES_FILE
                        TEST_CASES_FILE = Path(file_obj)

                        # ✅ 업로드 후, 각 탭의 "선택된 타입" 기준으로 테스트 목록 드롭다운 갱신
                        # type_selector_1 / type_selector_2 는 각 탭에서 만든 Dropdown 컴포넌트 변수여야 함
                        t1 = type_selector_1.value if hasattr(type_selector_1, "value") else None
                        t2 = type_selector_2.value if hasattr(type_selector_2, "value") else None

                        # gradio에서는 컴포넌트.value를 여기서 안정적으로 읽기 어려우니
                        # "고정 타입"이면 그냥 그 문자열을 써도 됩니다.
                        # (TAB1은 보통 "setting", TAB2는 "login" or "account")
                        # 아래는 가장 안전한 버전: 각 탭 default로 리셋
                        upd1 = update_test_dropdown("setting")
                        upd2 = update_test_dropdown("login")  # TAB2는 기본을 login으로

                        return upd1, upd2

                    return gr.update(), gr.update()

                upload_yaml.change(
                    fn=on_upload_yaml,
                    inputs=[upload_yaml],
                    outputs=[test_name_dropdown_1, test_name_dropdown_2],  # ✅ Dataframe 대신 Dropdown
                )

        return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app = create_app()
    app.queue()
    app.launch(server_name="0.0.0.0", server_port=7860, share=True)
