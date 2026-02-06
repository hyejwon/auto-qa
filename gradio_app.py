# gradio_app.py
"""
QA 자동화 테스트 - Gradio 인터페이스
사용법: python gradio_app.py
"""
import asyncio
import inspect
import json
import logging
import signal
import yaml
import os
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
from typing import Tuple
import time

_device_reservations: Dict[str, Dict[str, Any]] = {}
# { device_id: {"session": "<session_hash>", "ts": <epoch>} }

_RESERVATION_TTL_SEC = 60 * 30  # 30분(원하면 조정)


# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
REPORTS_DIR = Path("./reports")
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

RECORDINGS_DIR = Path("./recordings")
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

TEST_CASES_FILE = Path(os.environ.get("TEST_CASES_FILE", "test_cases.yaml"))

# 디바이스 목록 (환경변수 ADB_DEVICES 로 설정, 콤마 구분)
_DEFAULT_DEVICES = ["127.0.0.1:5555", "127.0.0.1:5565"]
ADB_DEVICES: List[str] = [
    d.strip()
    for d in os.environ.get("ADB_DEVICES", ",".join(_DEFAULT_DEVICES)).split(",")
    if d.strip()
]

# 디바이스별 상태 관리 (동시 실행 지원)
# {device_id: {"running": bool, "recording_process": Process | None}}
_device_state: Dict[str, Dict[str, Any]] = {}

_DEVICE_RECORDING_DIR = "/sdcard/qa_recordings"

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
        "adb", "-s", device_id, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )


async def start_adb_recording(device_id: str, log_callback=None) -> bool:
    """디바이스 셸에서 screenrecord 루프를 시작 (180초 세그먼트 자동 체이닝)"""
    state = _get_device_state(device_id)

    if state["recording_process"] is not None:
        if log_callback:
            log_callback("이미 녹화 중입니다.")
        return False

    try:
        # 이전 녹화 디렉토리 정리 및 재생성
        cleanup = await _adb_exec(
            device_id, "shell", "rm", "-rf", _DEVICE_RECORDING_DIR,
        )
        await cleanup.wait()
        mkdir = await _adb_exec(
            device_id, "shell", "mkdir", "-p", _DEVICE_RECORDING_DIR,
        )
        await mkdir.wait()

        # 셸 루프: 180초 세그먼트를 자동 반복 녹화
        # screenrecord 가 180초 후 종료되면 다음 세그먼트를 자동 시작
        loop_cmd = (
            f"i=0; "
            f"while true; do "
            f"  screenrecord --time-limit 180 "
            f"  {_DEVICE_RECORDING_DIR}/seg_$(printf '%03d' $i).mp4; "
            f"  i=$((i+1)); "
            f"done"
        )
        state["recording_process"] = await asyncio.create_subprocess_exec(
            "adb", "-s", device_id, "shell", loop_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        if log_callback:
            log_callback("📹 화면 녹화 시작")
        return True
    except Exception as e:
        if log_callback:
            log_callback(f"⚠️ 녹화 시작 실패: {e}")
        state["recording_process"] = None
        return False


async def stop_adb_recording(
    device_id: str, test_name: str, log_callback=None,
) -> Optional[str]:
    """녹화 중지 → 세그먼트 pull → ffmpeg 병합 → 최종 MP4 반환"""
    state = _get_device_state(device_id)
    rec_proc = state["recording_process"]

    if rec_proc is None:
        return None

    # 1) 로컬 adb shell 프로세스 종료 (셸 루프 중단)
    try:
        rec_proc.terminate()
        try:
            await asyncio.wait_for(rec_proc.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            rec_proc.kill()
            await rec_proc.wait()
    except Exception:
        pass
    finally:
        state["recording_process"] = None

    # 2) 디바이스에서 screenrecord 프로세스도 확실히 종료
    try:
        kill_proc = await _adb_exec(
            device_id, "shell",
            "kill -INT $(pidof screenrecord) 2>/dev/null; sleep 1; "
            "kill $(pidof screenrecord) 2>/dev/null || true",
        )
        await asyncio.wait_for(kill_proc.wait(), timeout=10.0)
    except Exception:
        pass

    # 파일 마무리 대기
    await asyncio.sleep(3.0)

    # 3) 디바이스에서 세그먼트 목록 조회
    ls_proc = await _adb_exec(
        device_id, "shell", "ls", _DEVICE_RECORDING_DIR,
    )
    stdout, _ = await ls_proc.communicate()
    segments = sorted([
        f.strip() for f in stdout.decode().strip().split("\n")
        if f.strip().endswith(".mp4")
    ])

    if not segments:
        if log_callback:
            log_callback("⚠️ 녹화 세그먼트가 없습니다.")
        return None

    # 4) 세그먼트를 로컬로 pull
    local_segments: List[Path] = []
    for seg in segments:
        device_path = f"{_DEVICE_RECORDING_DIR}/{seg}"
        local_path = RECORDINGS_DIR / seg
        try:
            pull = await _adb_exec(device_id, "pull", device_path, str(local_path))
            await asyncio.wait_for(pull.wait(), timeout=30.0)
            if local_path.exists() and local_path.stat().st_size > 0:
                local_segments.append(local_path)
            else:
                local_path.unlink(missing_ok=True)
        except Exception:
            pass

    # 디바이스 녹화 디렉토리 정리
    try:
        rm = await _adb_exec(device_id, "shell", "rm", "-rf", _DEVICE_RECORDING_DIR)
        await rm.wait()
    except Exception:
        pass

    if not local_segments:
        if log_callback:
            log_callback("⚠️ 녹화 파일을 가져오지 못했습니다.")
        return None

    # 5) 최종 파일명 생성
    safe_name = "".join(
        c if c.isalnum() or c in ("-", "_") else "_" for c in test_name
    )
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_filename = f"recording_{safe_name}_{timestamp}.mp4"
    final_path = RECORDINGS_DIR / final_filename

    if len(local_segments) == 1:
        # 세그먼트가 1개면 그냥 이름만 변경
        local_segments[0].rename(final_path)
    else:
        # 6) ffmpeg concat 으로 세그먼트 병합
        concat_list = RECORDINGS_DIR / f"_concat_{timestamp}.txt"
        concat_list.write_text(
            "\n".join(f"file '{seg.absolute()}'" for seg in local_segments),
            encoding="utf-8",
        )
        try:
            ffmpeg = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_list),
                "-c", "copy",
                str(final_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(ffmpeg.wait(), timeout=120.0)
        except Exception as e:
            if log_callback:
                log_callback(f"⚠️ ffmpeg 병합 실패: {e}")
            # 병합 실패 시 첫 번째 세그먼트라도 반환
            if not final_path.exists() and local_segments:
                local_segments[0].rename(final_path)

        # 임시 파일 정리
        concat_list.unlink(missing_ok=True)
        for seg in local_segments:
            seg.unlink(missing_ok=True)

    if final_path.exists() and final_path.stat().st_size > 0:
        size_mb = final_path.stat().st_size / (1024 * 1024)
        seg_count = len(segments)
        if log_callback:
            log_callback(
                f"📹 녹화 저장 완료: {final_filename} "
                f"({size_mb:.1f}MB, {seg_count}개 세그먼트)"
            )
        return str(final_path)

    if log_callback:
        log_callback("⚠️ 최종 녹화 파일 생성 실패")
    return None


def get_recent_recordings(limit: int = 20) -> List[List[str]]:
    """최근 녹화 파일 목록 반환"""
    recordings = sorted(
        RECORDINGS_DIR.glob("*.mp4"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    rows = []
    for r in recordings[:limit]:
        mtime = datetime.fromtimestamp(r.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        size_mb = r.stat().st_size / (1024 * 1024)
        rows.append([r.name, f"{size_mb:.1f} MB", mtime])
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

    # 타입별 분류
    by_type: Dict[str, List] = {}
    for tc in test_cases:
        case_type = tc.get("case_type", "기타")
        if case_type not in by_type:
            by_type[case_type] = []
        by_type[case_type].append(tc)

    return {
        "all": test_cases,
        "by_type": by_type,
        "types": sorted(by_type.keys())
    }


def get_test_case_choices() -> List[str]:
    """테스트 케이스 타입 목록 반환"""
    data = load_test_cases_from_file()
    return data["types"]


def get_test_cases_by_types(selected_types: List[str]) -> List[Dict[str, Any]]:
    """선택된 타입에 해당하는 테스트 케이스 반환"""
    data = load_test_cases_from_file()

    if not selected_types:
        return data["all"]

    cases = []
    for t in selected_types:
        cases.extend(data["by_type"].get(t, []))
    return cases


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

    # 도구 이름 → 사용자 친화적 이름 매핑
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

    def _format_user_friendly(self, msg: str) -> str:
        """개발자 로그를 사용자 친화적 메시지로 변환"""
        import re

        if "PLANNED:" in msg:
            return "🧠 플래너 계획 생성 완료"
        # Step 시작 감지
        step_match = re.search(r"\[Step (\d+)/(\d+)\] Tool: (\w+)", msg)
        if step_match:
            self.current_step = int(step_match.group(1))
            self.total_steps = int(step_match.group(2))
            tool_name = step_match.group(3)
            friendly_name = self.TOOL_NAMES.get(tool_name, tool_name)
            progress = f"[{self.current_step}/{self.total_steps}]"
            return f"\n{'─'*40}\n📌 {progress} {friendly_name}"

        # Goal 표시
        if "Goal:" in msg:
            goal = msg.split("Goal:")[-1].strip()
            return f"   목표: {goal}"

        # 도구 실행 중
        if "Invoking" in msg:
            return None  # 중복이므로 스킵

        # 성공/실패 상태
        if "completed with status: success" in msg:
            return "   ✅ 성공"
        if "completed with status: error" in msg:
            return "   ❌ 실패"
        if "Error:" in msg:
            error = msg.split("Error:")[-1].strip()[:100]
            return f"   ⚠️ 오류: {error}"

        # Step 완료
        if "completed successfully" in msg and "Step" in msg:
            return None  # 이미 성공 표시했으므로 스킵

        # 다음 Step 이동
        if "Moving to step" in msg:
            return None  # 다음 Step에서 표시하므로 스킵

        # 대기 중 (앱 로딩 등)
        if "Waiting" in msg:
            wait_match = re.search(r"Waiting ([\d.]+)s", msg)
            if wait_match:
                return f"   ⏳ 대기 중... ({wait_match.group(1)}초)"

        # 모든 Step 완료
        if "All" in msg and "steps completed" in msg:
            return f"\n🎉 모든 단계 완료!"

        # 재시도
        if "retrying" in msg.lower():
            retry_match = re.search(r"\((\d+)/(\d+)\)", msg)
            if retry_match:
                return f"   🔄 재시도 중... ({retry_match.group(1)}/{retry_match.group(2)})"

        # 리플랜
        if "replan" in msg.lower():
            return "   🔄 계획 재수립 중..."

        # 기타 불필요한 로그 필터링
        skip_patterns = [
            "DEBUG", "httpx", "httpcore", "urllib3",
            "Arguments:", "sanitize", "={'", "📋"
        ]
        if any(skip in msg for skip in skip_patterns):
            return None

        return None  # 기본적으로 표시하지 않음

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

    # 기본 로깅 설정
    logging.basicConfig(
        level=logging.DEBUG if cfg.debug else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Gradio 로그 핸들러 추가
    gradio_handler = None
    if log_callback:
        gradio_handler = GradioLogHandler(log_callback)
        gradio_handler.setLevel(logging.INFO)

        # executor 로거에 핸들러 추가
        executor_logger = logging.getLogger("qa_agent.executor.executor")
        executor_logger.addHandler(gradio_handler)
        executor_logger.setLevel(logging.INFO)

        # planner 로거에도 추가
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
        env=os.environ.copy()
    )
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

                    # 녹화 시작
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

                    # 녹화 중지 및 저장
                    rec_path = await stop_adb_recording(
                        cfg.device_id, test_name, _log,
                    )
                    if rec_path:
                        result["recording_file"] = rec_path

                    results.append(result)

    except* BrokenResourceError:
        _log("MCP 연결이 종료되었습니다 (BrokenResourceError).")

    finally:
        await _maybe_close_llm(llm)

        # 진행 중인 녹화 정리
        state = _get_device_state(cfg.device_id)
        if state.get("recording_process") is not None:
            await stop_adb_recording(cfg.device_id, "cleanup", _log)

        # Gradio 로그 핸들러 정리
        if gradio_handler:
            executor_logger = logging.getLogger("qa_agent.executor.executor")
            executor_logger.removeHandler(gradio_handler)
            planner_logger = logging.getLogger("qa_agent.planner.planner")
            planner_logger.removeHandler(gradio_handler)

    return results


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def list_existing_reports() -> List[List[str]]:
    """reports/ 디렉토리의 기존 리포트 목록"""
    reports = sorted(REPORTS_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    rows = []
    for r in reports:
        mtime = datetime.fromtimestamp(r.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        content = r.read_text(encoding="utf-8")
        if "✅" in content or "PASS" in content[:500]:
            status = "✅ PASS"
        elif "❌" in content or "FAIL" in content[:500]:
            status = "❌ FAIL"
        else:
            status = "⏳"
        rows.append([r.name, status, mtime])
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
# Test Case Selection UI Handlers
# ---------------------------------------------------------------------------
def on_type_select(selected_type: str) -> List[List[str]]:
    if not selected_type or selected_type == "전체":
        cases = get_test_cases_by_types([])
    else:
        cases = get_test_cases_by_types([selected_type])

    return [
        [tc.get("case_type", ""), tc.get("name", ""), tc.get("goal", "")[:100] + "..."]
        for tc in cases
    ]


async def run_selected_tests(selected_type: str,selected_device: str,
    session_running: bool,
progress=gr.Progress(track_tqdm=True),request: gr.Request = None):
    session_hash = getattr(request, "session_hash", None) or "unknown"
    if session_running:
        gr.Warning("이미 이 세션에서 테스트가 실행 중입니다.")
        yield "", "", gr.update(), gr.update(), gr.update(), True
        return

    if not selected_device:
        gr.Warning("디바이스를 선택해주세요.")
        yield "", "", gr.update(), gr.update(), gr.update(), False
        return

    ok, msg = reserve_device(selected_device, session_hash)
    if not ok:
        gr.Warning(msg)
        yield "", "", gr.update(), gr.update(), gr.update(), False
        return
    type_info = selected_type if (selected_type and selected_type != "전체") else "전체"
    cases = get_test_cases_by_types([] if type_info == "전체" else [selected_type])

    if not cases:
        gr.Warning("실행할 테스트 케이스가 없습니다. 타입을 선택해주세요.")
        yield "", "", gr.update(), gr.update(), gr.update()
        return

    log_lines: list[str] = []
    q: asyncio.Queue[str] = asyncio.Queue()

    def _log(msg: str):
        try:
            q.put_nowait(msg)
        except Exception:
            pass

    # 시작 로그
    ts = datetime.now().strftime("%H:%M:%S")
    _log(f"[{ts}] 🚀 테스트 시작")
    _log(f"{'═'*40}")
    _log(f"📋 실행할 테스트: {len(cases)}개 ({type_info})")
    for i, tc in enumerate(cases, 1):
        _log(f"   {i}. {tc.get('name', '이름 없음')}")
    _log(f"{'═'*40}")

    task = asyncio.create_task(_run_tests_async(cases, device_id=selected_device, log_callback=_log))

        # 초기 UI
    yield "\n".join(log_lines), "*실행 중...*", gr.update(value=list_existing_reports()), gr.update(value=get_recent_recordings()), gr.update(value=None), True

    try:
        while True:
            try:
                msg2 = await asyncio.wait_for(q.get(), timeout=0.2)
                log_lines.append(msg2)
                yield "\n".join(log_lines), "*실행 중...*", gr.update(), gr.update(), gr.update(), True
            except asyncio.TimeoutError:
                if task.done():
                    break

        while not q.empty():
            log_lines.append(q.get_nowait())

        results = await task

        # (기존 summary 구성 그대로)
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

        yield "\n".join(log_lines), summary_md, list_existing_reports(), get_recent_recordings(), latest_recording, False

    finally:
        # ✅ 실행 끝나면 자동 해제
        release_device(selected_device, session_hash)


# ---------------------------------------------------------------------------
# Build Gradio UI
# ---------------------------------------------------------------------------
def create_app() -> gr.Blocks:
    with gr.Blocks(
        title="QA 자동화 테스트",
        theme=gr.themes.Soft(),
        css="""
        .report-md { max-height: 600px; overflow-y: auto; }
        .log-area { font-family: monospace; font-size: 12px; }
        .log-area textarea {
            background-color: #1e1e1e !important;
            color: #d4d4d4 !important;
            font-family: 'Consolas', 'Monaco', monospace !important;
        }
        .test-table { max-height: 300px; overflow-y: auto; }
        """,
    ) as app:
        gr.Markdown("# 🧪 QA 자동화 테스트 대시보드")
        gr.Markdown("외부 팀원용 테스트 실행 및 결과 확인 인터페이스")

        with gr.Tabs():
            # ============ Tab 1: 테스트 실행 ============
            with gr.Tab("🚀 테스트 실행"):
                with gr.Row():
                    with gr.Column(scale=1):
                        gr.Markdown("### 테스트 타입 선택")
                        session_running = gr.State(False) 
                        device_selector = gr.Radio(
                            choices=ADB_DEVICES,
                            value=ADB_DEVICES[0] if ADB_DEVICES else None,
                            label="테스트에 사용할 BlueStacks(ADB) 디바이스",
                            info="각 사용자는 서로 다른 디바이스를 선택해야 합니다"
                        )
                        
                        release_btn = gr.Button("🔓 디바이스 해제", variant="secondary")
                        reserve_status = gr.Markdown()
                        
                        
                        type_choices = ["전체"] + get_test_case_choices()
                        type_selector = gr.Radio(
                            choices=type_choices,
                            value="전체",
                            label="실행할 테스트 타입",
                            info="하나만 선택할 수 있습니다"
                        )

                        run_btn = gr.Button(
                            "🚀 테스트 실행",
                            variant="primary",
                            size="lg",
                        )

                        gr.Markdown("### 선택된 테스트 케이스")
                        test_preview = gr.Dataframe(
                            headers=["타입", "테스트명", "Goal (요약)"],
                            datatype=["str", "str", "str"],
                            value=on_type_select("전체"),
                            col_count=(3, "fixed"),
                            interactive=False,
                            wrap=True,
                            elem_classes=["test-table"],
                        )

                    with gr.Column(scale=2):
                        gr.Markdown("### 실행 로그 (Step별 진행 상황)")
                        log_output = gr.Textbox(
                            label="로그",
                            lines=15,
                            max_lines=50,
                            interactive=False,
                            autoscroll=True,
                            elem_classes=["log-area"],
                        )

                        gr.Markdown("### 테스트 결과")
                        result_report = gr.Markdown(
                            value="*테스트를 실행하면 여기에 결과가 표시됩니다.*",
                            elem_classes=["report-md"],
                        )

            # ============ Tab 2: 녹화 영상 ============
            with gr.Tab("📹 녹화 영상"):
                gr.Markdown("### 테스트 녹화 영상")
                gr.Markdown("테스트 실행 시 ADB screenrecord로 자동 녹화됩니다. (180초 단위 세그먼트 자동 연결)")

                with gr.Row():
                    recording_refresh_btn = gr.Button(
                        "🔄 새로고침", variant="secondary"
                    )

                recording_table = gr.Dataframe(
                    headers=["파일명", "크기", "녹화 시간"],
                    datatype=["str", "str", "str"],
                    value=get_recent_recordings(),
                    col_count=(3, "fixed"),
                    interactive=False,
                    wrap=True,
                )

                video_download = gr.File(
                    label="녹화 파일 다운로드 (위 목록에서 클릭하세요)",
                    interactive=False,
                )

            # ============ Tab 3: 리포트 조회 ============
            with gr.Tab("📊 리포트 조회"):
                gr.Markdown("### 저장된 테스트 리포트")
                gr.Markdown("리포트를 클릭하면 상세 내용을 확인할 수 있습니다.")

                report_refresh_btn = gr.Button("🔄 새로고침", variant="secondary")

                report_table = gr.Dataframe(
                    headers=["파일명", "결과", "생성 시간"],
                    datatype=["str", "str", "str"],
                    value=list_existing_reports(),
                    col_count=(3, "fixed"),
                    interactive=False,
                    wrap=True,
                )

                saved_report_md = gr.Markdown(
                    value="*리포트를 선택하세요.*",
                    elem_classes=["report-md"],
                )

            # ============ Tab 4: 설정 ============
            with gr.Tab("⚙️ 설정"):
                gr.Markdown("### 테스트 환경 설정")

                cfg = QAConfig()
                with gr.Row():
                    with gr.Column():
                        gr.Markdown("**현재 설정**")
                        gr.Textbox(label="디바이스 ID", value=cfg.device_id, interactive=False)
                        gr.Textbox(label="패키지명", value=cfg.package_name, interactive=False)
                        gr.Textbox(label="LLM 모델", value=cfg.model, interactive=False)

                    with gr.Column():
                        gr.Markdown("**테스트 케이스 파일**")
                        gr.Textbox(
                            label="현재 파일",
                            value=str(TEST_CASES_FILE),
                            interactive=False
                        )

                        # 파일 업로드로 테스트 케이스 변경
                        upload_yaml = gr.File(
                            label="새 테스트 케이스 파일 업로드 (YAML)",
                            file_types=[".yaml", ".yml"],
                            type="filepath",
                        )

        # ============ Event bindings ============

        # 타입 선택 시 테스트 목록 업데이트
        type_selector.change(
            fn=on_type_select,
            inputs=[type_selector],
            outputs=[test_preview],
        )

        # 테스트 실행 (5개 출력: 로그, 리포트, 리포트목록, 녹화목록, 녹화파일)
        run_btn.click(
    fn=run_selected_tests,
    inputs=[type_selector, device_selector, session_running],
    outputs=[log_output, result_report, report_table, recording_table, video_download, session_running],
)
        def on_release_device(selected_device: str, request: gr.Request):
            session_hash = getattr(request, "session_hash", None) or "unknown"
            return release_device(selected_device, session_hash)

        release_btn.click(
            fn=on_release_device,
            inputs=[device_selector],
            outputs=[reserve_status],
)
        # 녹화 목록 새로고침
        recording_refresh_btn.click(
            fn=get_recent_recordings,
            outputs=[recording_table],
        )

        # 녹화 테이블 클릭 → 다운로드 파일 설정
        recording_table.select(
            fn=on_recording_select,
            inputs=[recording_table],
            outputs=[video_download],
        )

        # 리포트 목록 새로고침
        report_refresh_btn.click(
            fn=list_existing_reports,
            outputs=[report_table],
        )

        # 리포트 테이블 클릭 -> 내용 표시
        report_table.select(
            fn=on_report_select,
            inputs=[report_table],
            outputs=[saved_report_md],
        )

        # 새 테스트 케이스 파일 업로드
        def on_upload_yaml(file_obj):
            if file_obj:
                global TEST_CASES_FILE
                TEST_CASES_FILE = Path(file_obj)
                new_types = ["전체"] + get_test_case_choices()
                return gr.update(choices=new_types, value="전체"), on_type_select("전체")
            return gr.update(), gr.update()

        upload_yaml.change(
            fn=on_upload_yaml,
            inputs=[upload_yaml],
            outputs=[type_selector, test_preview],
        )

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app = create_app()
    app.queue()
    app.launch(server_name="0.0.0.0", server_port=7860, share=True)
