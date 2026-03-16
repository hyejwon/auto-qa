# main.py
import logging
import queue
import threading
import yaml
from datetime import datetime

import gradio as gr

from adb_controller import ADBController
from config import Config
from planner_node import PlannerNode
from qa_orchestrator import QAOrchestrator

_root = logging.getLogger()
_root.setLevel(logging.INFO)
if not _root.handlers:
    _console = logging.StreamHandler()
    _console.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    _root.addHandler(_console)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 샘플 시나리오 (placeholder 예시)
# ─────────────────────────────────────────────
SAMPLE_SCENARIOS = [
    (
        "로그인 이용약관 확인",
        "com.percent.aos.cooptd",
        """\
앱을 실행한다.
→ 구글 로그인 버튼을 클릭한다.
→ 이용약관 버튼을 클릭한다.
→ 슈퍼매직 이용약관이 화면에 나오는지 확인.
→ 뒤로가기를 누른다.
→ 개인정보 처리방침 버튼을 클릭한다.
→ 화면에서 개인정보 처리방침 텍스트를 확인한다.
→ 앱을 종료한다.""",
    ),
    (
        "진동 설정 ON 테스트",
        "com.percent.aos.cooptd",
        """\
앱을 실행한다.
→ 햄버거 메뉴를 클릭한다.
→ 설정 메뉴로 진입한다.
→ 진동 ON 버튼을 클릭한다.
→ 앱을 재실행한다.
→ 진동 OFF 표시가 보이는지 확인한다.
→ 앱을 종료한다.""",
    ),
    (
        "게스트 계정 삭제",
        "com.percent.aos.cooptd",
        """\
앱을 실행한다.
→ 햄버거 메뉴를 클릭한다.
→ 설정 메뉴로 진입한다.
→ 계정 연동을 클릭한다.
→ 계정 삭제 버튼을 클릭한다.
→ 앱을 재실행한다.
→ 게스트 로그인 버튼이 보이는지 확인한다.
→ 앱을 종료한다.""",
    ),
    (
        "스태미너 충전 구매",
        "com.percent.aos.cooptd",
        """\
앱을 실행한다.
→ 10초 대기한다.
→ 상단 번개 모양의 스태미너 충전 버튼을 클릭한다.
→ 구매하기 버튼을 클릭한다.
→ 구매 완료까지 대기한다.
→ 스태미너가 정상 지급됐는지 확인한다.
→ 다이아가 차감됐는지 확인한다.
→ 앱을 종료한다.""",
    ),
]

SCENARIO_PLACEHOLDER = """\
예시:
앱을 실행한다.
→ 구글 로그인 버튼을 클릭한다.
→ 이용약관 버튼을 클릭한다.
→ UI에서 'terms-of-service' 텍스트를 확인한다.
→ 뒤로가기를 누른다.
→ 앱을 종료한다.

위처럼 단계별로 자연어로 작성하거나,
위의 샘플 버튼을 클릭하여 예시를 불러올 수 있습니다."""


# ─────────────────────────────────────────────
# 헬퍼 함수
# ─────────────────────────────────────────────

def _config() -> Config:
    return Config()


def _get_testcase_choices() -> list[str]:
    try:
        tc_dir = _config().paths.testcases_dir
        files = sorted(tc_dir.glob("*.yaml"))
        return [f.stem for f in files]
    except Exception:
        return []


# ─────────────────────────────────────────────
# 템플릿 편집 헬퍼
# ─────────────────────────────────────────────

MAX_TEMPLATE_STEPS = 20  # UI에 미리 생성할 최대 스텝 수

# 사용 가능한 액션 목록
ACTION_CHOICES = [
    "find_and_tap", "verify", "read_text",
    "wait", "back", "home",
    "launch_app", "close_app", "skip_tutorial", "swipe",
]

TARGET_ACTIONS = {"find_and_tap", "verify", "read_text"}


def _get_template_choices() -> list[str]:
    """templates/ 디렉토리의 YAML 파일 목록."""
    try:
        tpl_dir = _config().paths.templates_dir
        files = sorted(tpl_dir.glob("*.yaml"))
        return [f.stem for f in files]
    except Exception:
        return []


def _load_template(name: str) -> dict | None:
    if not name:
        return None
    try:
        path = _config().paths.templates_dir / f"{name}.yaml"
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception:
        return None


def _steps_to_yaml_state(steps: list[dict]) -> str:
    """스텝 리스트를 YAML 문자열로 직렬화 (hidden state 용)."""
    return yaml.dump(steps, allow_unicode=True, sort_keys=False)


def _yaml_state_to_steps(yaml_str: str) -> list[dict]:
    """hidden state YAML → 스텝 리스트."""
    if not yaml_str.strip():
        return []
    try:
        data = yaml.safe_load(yaml_str)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _build_step_ui_outputs(steps: list[dict]) -> list:
    """스텝 리스트 → Gradio UI 업데이트 값 생성.

    Returns: *(visible, action, target, description) × MAX_TEMPLATE_STEPS
    """
    rows: list = []
    for i in range(MAX_TEMPLATE_STEPS):
        if i < len(steps):
            s = steps[i]
            action = s.get("action", "")
            target = s.get("target") or ""
            desc = s.get("description", "")
            has_target = action in TARGET_ACTIONS
            rows.extend([
                gr.update(visible=True),
                gr.update(value=action),
                gr.update(value=target, interactive=has_target),
                gr.update(value=desc),
            ])
        else:
            rows.extend([
                gr.update(visible=False),
                gr.update(value=""),
                gr.update(value="", interactive=False),
                gr.update(value=""),
            ])
    return rows


def load_template_for_edit(name: str):
    """템플릿 로드.

    Returns: (status, steps_yaml, title, package,
              *(visible, action, target, description) × MAX)
    """
    empty_rows = _build_step_ui_outputs([])

    if not name:
        return ("⚠️ 템플릿을 선택해주세요.", "", "", "", *empty_rows)

    data = _load_template(name)
    if not data:
        return (f"❌ {name} 로드 실패", "", "", "", *empty_rows)

    steps = data.get("steps", [])
    title = data.get("title", "")
    package = data.get("package", "")
    steps_yaml = _steps_to_yaml_state(steps)
    ui_rows = _build_step_ui_outputs(steps)

    return (
        f"✅ 로드 완료 — {title} ({len(steps)}스텝)",
        steps_yaml,
        title,
        package,
        *ui_rows,
    )


def append_step(steps_yaml: str, action: str):
    """맨 뒤에 새 스텝 추가. Returns: (info, steps_yaml, *ui_rows)"""
    steps = _yaml_state_to_steps(steps_yaml)
    new_step = {
        "action": action or "find_and_tap",
        "target": None,
        "params": {},
        "description": "",
        "timeout": 10,
        "retry": 2,
    }
    steps.append(new_step)
    info = f"✅ Step {len(steps)} 추가됨 (총 {len(steps)}스텝)"
    return (info, _steps_to_yaml_state(steps), *_build_step_ui_outputs(steps))


def insert_step(steps_yaml: str, position: int, action: str):
    """지정 위치에 새 스텝 삽입. Returns: (info, steps_yaml, *ui_rows)"""
    steps = _yaml_state_to_steps(steps_yaml)
    new_step = {
        "action": action or "find_and_tap",
        "target": None,
        "params": {},
        "description": "",
        "timeout": 10,
        "retry": 2,
    }
    pos = max(0, min(int(position), len(steps)))
    steps.insert(pos, new_step)
    info = f"✅ Step {pos+1} 위치에 삽입됨 (총 {len(steps)}스텝)"
    return (info, _steps_to_yaml_state(steps), *_build_step_ui_outputs(steps))


def remove_step_at(steps_yaml: str, position: int):
    """지정 위치의 스텝 삭제 (1-based). Returns: (info, steps_yaml, *ui_rows)"""
    steps = _yaml_state_to_steps(steps_yaml)
    idx = int(position) - 1
    if 0 <= idx < len(steps):
        removed = steps.pop(idx)
        info = f"🗑️ Step {idx+1} ({removed.get('action','')}) 삭제됨 (총 {len(steps)}스텝)"
    else:
        info = f"⚠️ 유효하지 않은 위치: {position}"
    return (info, _steps_to_yaml_state(steps), *_build_step_ui_outputs(steps))


def move_step(steps_yaml: str, position: int, direction: int):
    """스텝을 위/아래로 이동 (1-based position, direction: -1=up, +1=down).
    Returns: (info, steps_yaml, *ui_rows)"""
    steps = _yaml_state_to_steps(steps_yaml)
    idx = int(position) - 1
    new_idx = idx + direction
    if 0 <= idx < len(steps) and 0 <= new_idx < len(steps):
        steps[idx], steps[new_idx] = steps[new_idx], steps[idx]
        arrow = "⬆️" if direction == -1 else "⬇️"
        info = f"{arrow} Step {idx+1} → Step {new_idx+1} 이동됨"
    else:
        info = "⚠️ 이동할 수 없습니다."
    return (info, _steps_to_yaml_state(steps), *_build_step_ui_outputs(steps))


def _collect_steps_from_ui(steps_yaml: str, actions, targets, descriptions) -> list[dict]:
    """UI 값을 현재 steps에 반영."""
    steps = _yaml_state_to_steps(steps_yaml)
    for i, s in enumerate(steps):
        if i < len(actions) and actions[i]:
            s["action"] = actions[i]
        if i < len(targets) and s.get("action") in TARGET_ACTIONS:
            s["target"] = targets[i] if targets[i] else s.get("target")
        if i < len(descriptions) and descriptions[i]:
            s["description"] = descriptions[i]
    return steps


def _build_testcase_from_template(steps_yaml: str, title: str, package: str,
                                   actions, targets, descriptions):
    """편집된 값을 반영하여 TestCase 객체를 생성."""
    from test_manager import TestCase

    steps = _collect_steps_from_ui(steps_yaml, actions, targets, descriptions)

    # launch_app 스텝의 패키지 동기화
    if package.strip():
        for step in steps:
            if step.get("action") == "launch_app":
                step.setdefault("params", {})["package"] = package.strip()

    data = {
        "id": "TPL_RUN",
        "title": title.strip() or "템플릿 실행",
        "description": "",
        "package": package.strip(),
        "steps": steps,
        "expected_results": [],
        "preconditions": [],
    }
    return TestCase(**data)


def run_template(steps_yaml: str, title: str, package: str, *field_values):
    """편집된 템플릿을 저장 없이 바로 실행 (스트리밍 제너레이터).

    field_values: (action, target, description) × MAX_TEMPLATE_STEPS
    """
    global _rec_adb, _test_stop_event

    if not steps_yaml.strip():
        yield "⚠️ 먼저 템플릿을 로드해주세요.", ""
        return

    # field_values를 action/target/description 리스트로 분리
    actions = [field_values[i * 3] for i in range(MAX_TEMPLATE_STEPS) if i * 3 < len(field_values)]
    targets = [field_values[i * 3 + 1] for i in range(MAX_TEMPLATE_STEPS) if i * 3 + 1 < len(field_values)]
    descriptions = [field_values[i * 3 + 2] for i in range(MAX_TEMPLATE_STEPS) if i * 3 + 2 < len(field_values)]

    try:
        testcase = _build_testcase_from_template(
            steps_yaml, title, package, actions, targets, descriptions
        )
    except Exception as e:
        yield f"❌ 템플릿 구성 오류: {e}", ""
        return

    _test_stop_event = threading.Event()
    log_q: queue.Queue = queue.Queue()
    result_holder: dict = {}

    _STEP_MARKERS = ("━", "▶ ", "┌─", "│", "└─", "⏹️", "🔴", "  결과:", "  테스트 시작:", "  패키지:")

    class _StreamHandler(logging.Handler):
        def emit(self, record):
            msg = record.getMessage()
            if any(m in msg for m in _STEP_MARKERS):
                log_q.put(msg)

    handler = _StreamHandler()
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)

    def _run():
        global _rec_adb
        try:
            cfg = _config()
            _rec_adb = ADBController()
            session = datetime.now().strftime("%Y%m%d_%H%M%S")
            _rec_adb.start_recording(cfg.paths.recordings_dir, session)
            logger.info(f"🔴 화면 녹화 시작 — 세션: {session}")
        except Exception as e:
            logger.warning(f"⚠️ 녹화 시작 실패: {e}")
            _rec_adb = None

        try:
            orchestrator = QAOrchestrator(_config())
            result_holder["result"] = orchestrator.run_test(
                testcase.id, _test_stop_event, testcase_override=testcase
            )
        except Exception as e:
            logger.exception("run_template thread failed")
            result_holder["error"] = str(e)
        finally:
            if _rec_adb and _rec_adb.is_recording:
                try:
                    files = _rec_adb.stop_recording()
                    if files:
                        logger.info(f"⏹️ 녹화 완료 — {len(files)}개 청크")
                except Exception:
                    pass
            log_q.put(None)

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    log_lines: list[str] = []
    while True:
        try:
            msg = log_q.get(timeout=0.15)
        except queue.Empty:
            if not t.is_alive():
                break
            yield "\n".join(log_lines), ""
            continue
        if msg is None:
            break
        log_lines.append(msg)
        yield "\n".join(log_lines), ""

    root_logger.removeHandler(handler)
    t.join(timeout=5)
    _test_stop_event = None

    if "result" in result_holder:
        summary = _format_summary(result_holder["result"])
    else:
        err = result_holder.get("error", "알 수 없는 오류")
        summary = f"## ❌ 실행 실패\n\n> {err}"

    yield "\n".join(log_lines), summary


# ─────────────────────────────────────────────
# Gradio 이벤트 핸들러
# ─────────────────────────────────────────────

def load_sample(idx: int):
    """샘플 시나리오를 입력 필드에 채운다."""
    label, pkg, scenario = SAMPLE_SCENARIOS[idx]
    return pkg, scenario


def generate_plan(package_name: str, scenario: str):
    """자연어 시나리오 → YAML 테스트 플랜 생성."""
    if not scenario.strip():
        return "⚠️ 시나리오를 입력해주세요.", ""

    try:
        cfg = _config()
        planner = PlannerNode(
            project=cfg.gemini.project,
            location=cfg.gemini.location,
            model=cfg.gemini.model,
        )
        plan = planner.create_test_plan(scenario.strip(), package_name.strip())

        yaml_data = {
            "title": plan.title,
            "description": plan.description,
            "package": plan.package,
            "steps": [s.model_dump() for s in plan.steps],
            "expected_results": plan.expected_results,
        }
        yaml_str = yaml.dump(yaml_data, allow_unicode=True, sort_keys=False)
        status = f"✅ 플랜 생성 완료 — {plan.title}  ({len(plan.steps)}개 스텝)"
        return status, yaml_str

    except Exception as e:
        logger.exception("generate_plan failed")
        return f"❌ 오류: {e}", ""


def save_plan(yaml_preview: str, scenario: str = ""):
    """생성된 YAML을 testcases 디렉토리에 저장."""
    if not yaml_preview.strip():
        return "⚠️ 먼저 플랜을 생성해주세요.", gr.update()

    try:
        cfg = _config()
        tc_dir = cfg.paths.testcases_dir

        existing = sorted(tc_dir.glob("TC_AUTO_*.yaml"))
        next_num = len(existing) + 1
        test_id = f"TC_AUTO_{next_num:03d}"

        data = yaml.safe_load(yaml_preview)
        data["id"] = test_id
        data.setdefault("preconditions", [])
        data["source_scenario"] = scenario.strip()

        out_path = tc_dir / f"{test_id}.yaml"
        with open(out_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, sort_keys=False)

        return (
            f"✅ 저장 완료 → {out_path.name}",
            gr.update(choices=_get_testcase_choices()),
        )

    except Exception as e:
        logger.exception("save_plan failed")
        return f"❌ 오류: {e}", gr.update()


def refresh_testcases():
    return gr.update(choices=_get_testcase_choices())


def _get_recording_choices() -> list[str]:
    """recordings 디렉토리의 mp4 파일 목록 (최신순)."""
    try:
        rec_dir = _config().paths.recordings_dir
        files = sorted(rec_dir.glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)
        return [f.name for f in files]
    except Exception:
        return []


def get_recording_file(filename: str):
    """선택한 녹화 파일의 경로를 반환 (Video + File 컴포넌트 동시 업데이트)."""
    if not filename:
        return None, None
    try:
        path = _config().paths.recordings_dir / filename
        p = str(path) if path.exists() else None
        return p, p
    except Exception:
        return None, None


def refresh_recordings():
    return gr.update(choices=_get_recording_choices(), value=None)


def _get_apk_choices() -> list[str]:
    """apks 디렉토리의 APK 파일 목록 (최신순)."""
    try:
        apks_dir = _config().paths.apks_dir
        files = sorted(apks_dir.glob("*.apk"), key=lambda f: f.stat().st_mtime, reverse=True)
        return [f.name for f in files]
    except Exception:
        return []


def refresh_apks():
    return gr.update(choices=_get_apk_choices(), value=None)


def install_apk(filename: str):
    """선택한 APK를 디바이스에 설치."""
    if not filename:
        yield "⚠️ APK 파일을 선택해주세요."
        return

    apk_path = _config().paths.apks_dir / filename
    yield f"📦 설치 중: {filename} ..."

    try:
        adb = ADBController()
        _, msg = adb.install_apk(apk_path)
        yield msg
    except Exception as e:
        logger.exception("install_apk failed")
        yield f"❌ ADB 연결 오류: {e}"


INSTALLED_PACKAGES = [
    "com.percent.aos.cooptd",
    "com.percent.aos.rollinghero",
    "com.supermagic.aos.statusman",
    "com.percent.aos.luckydefense",
    "com.percent.aos.arenago2",
]


def uninstall_app(package: str):
    """선택한 앱을 디바이스에서 삭제."""
    if not package:
        yield "⚠️ 패키지를 선택해주세요."
        return

    yield f"🗑️ 삭제 중: {package} ..."

    try:
        adb = ADBController()
        _, msg = adb.uninstall_app(package)
        yield msg
    except Exception as e:
        logger.exception("uninstall_app failed")
        yield f"❌ ADB 연결 오류: {e}"


def load_testcase_info(test_id: str) -> str:
    """선택한 테스트 케이스의 원본 자연어 시나리오를 반환."""
    if not test_id:
        return ""
    try:
        cfg = _config()
        yaml_path = cfg.paths.testcases_dir / f"{test_id}.yaml"
        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data.get("source_scenario", "")
    except Exception:
        return ""


# ─────────────────────────────────────────────
# 화면 녹화 핸들러
# ─────────────────────────────────────────────

_rec_adb: ADBController | None = None       # 녹화 전용 ADB 인스턴스
_test_stop_event: threading.Event | None = None  # 실행 중 테스트 중단 이벤트


def start_recording():
    """녹화 시작 — 3분 청크 단위 자동 분할."""
    global _rec_adb
    if _rec_adb and _rec_adb.is_recording:
        return "⚠️ 이미 녹화 중입니다.", gr.update()

    try:
        cfg = _config()
        _rec_adb = ADBController()
        session = datetime.now().strftime("%Y%m%d_%H%M%S")
        _rec_adb.start_recording(cfg.paths.recordings_dir, session)
        return f"🔴 녹화 중...  세션: {session}  (3분 단위 자동 분할)", gr.update(
            value="", interactive=False
        )
    except Exception as e:
        logger.exception("start_recording failed")
        return f"❌ 오류: {e}", gr.update()


def stop_recording():
    """녹화 중지 — 저장된 파일 목록 반환."""
    global _rec_adb
    if not _rec_adb or not _rec_adb.is_recording:
        return "⚠️ 녹화 중이 아닙니다.", gr.update()

    try:
        files = _rec_adb.stop_recording()
        if files:
            file_list = "\n".join(f.name for f in files)
            status = f"⏹️ 녹화 완료 — {len(files)}개 청크 저장"
        else:
            file_list = "(저장된 파일 없음)"
            status = "⏹️ 녹화 중지됨"
        return status, gr.update(value=file_list)
    except Exception as e:
        logger.exception("stop_recording failed")
        return f"❌ 오류: {e}", gr.update()


def _format_summary(result) -> str:
    """TestResult → Markdown 요약 문자열."""
    icon = "✅" if result.status == "PASS" else "❌"
    rows = [
        f"## {icon} {result.status} — {result.title}",
        "",
        "| 항목 | 내용 |",
        "|:---|:---|",
        f"| 테스트 ID | `{result.test_id}` |",
        f"| 스텝 통과 | **{result.steps_passed} / {result.steps_executed}** |",
    ]
    if result.end_time and result.start_time:
        sec = (result.end_time - result.start_time).total_seconds()
        rows.append(f"| 소요 시간 | {sec:.1f}초 |")
    if result.screenshots:
        rows.append(f"| 스크린샷 | {len(result.screenshots)}장 저장 |")
    if result.error_message:
        rows += ["", f"> ⚠️ **오류**: {result.error_message}"]

    if result.step_results:
        rows += ["", "### 스텝별 결과"]
        for sr in result.step_results:
            icon = "✅" if sr["passed"] else "❌"
            rows.append(f"- {icon} **Step {sr['step']}** — {sr['label']}")

    return "\n".join(rows)


def stop_test():
    """실행 중인 테스트에 중단 신호를 보낸다."""
    global _test_stop_event
    if _test_stop_event and not _test_stop_event.is_set():
        _test_stop_event.set()
        return "⏹️ 중단 요청됨 — 현재 스텝 완료 후 중지됩니다."
    return "⚠️ 실행 중인 테스트가 없습니다."


def run_test(test_id: str):
    """스트리밍 제너레이터 — 실시간 로그 + 최종 Markdown 요약.
    테스트 시작 시 화면 녹화를 자동으로 시작하고 종료 시 자동 중지한다.
    """
    global _rec_adb, _test_stop_event

    if not test_id:
        yield "⚠️ 테스트 케이스를 선택해주세요.", "", gr.update()
        return

    _test_stop_event = threading.Event()
    log_q: queue.Queue = queue.Queue()
    result_holder: dict = {}

    _STEP_MARKERS = ("━", "▶ ", "┌─", "│", "└─", "⏹️", "🔴", "  결과:", "  테스트 시작:", "  패키지:")

    class _StreamHandler(logging.Handler):
        def emit(self, record):
            msg = record.getMessage()
            if any(m in msg for m in _STEP_MARKERS):
                log_q.put(msg)

    handler = _StreamHandler()
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)

    def _run():
        global _rec_adb
        # ── 녹화 자동 시작 ────────────────────────────
        try:
            cfg = _config()
            _rec_adb = ADBController()
            session = datetime.now().strftime("%Y%m%d_%H%M%S")
            _rec_adb.start_recording(cfg.paths.recordings_dir, session)
            logger.info(f"🔴 화면 녹화 시작 — 세션: {session}")
        except Exception as e:
            logger.warning(f"⚠️ 녹화 시작 실패 (테스트는 계속 진행): {e}")
            _rec_adb = None

        # ── 테스트 실행 ───────────────────────────────
        try:
            orchestrator = QAOrchestrator(_config())
            result_holder["result"] = orchestrator.run_test(
                test_id, _test_stop_event
            )
        except Exception as e:
            logger.exception("run_test thread failed")
            result_holder["error"] = str(e)
        finally:
            # ── 녹화 자동 중지 ────────────────────────
            if _rec_adb and _rec_adb.is_recording:
                try:
                    files = _rec_adb.stop_recording()
                    if files:
                        names = "  |  ".join(f.name for f in files)
                        logger.info(f"⏹️ 녹화 완료 — {len(files)}개 청크: {names}")
                    else:
                        logger.info("⏹️ 녹화 중지됨 (저장 파일 없음)")
                except Exception as e:
                    logger.warning(f"녹화 중지 실패: {e}")
            log_q.put(None)  # sentinel

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    log_lines: list[str] = []
    while True:
        try:
            msg = log_q.get(timeout=0.15)
        except queue.Empty:
            if not t.is_alive():
                break
            yield "\n".join(log_lines), "", gr.update()
            continue
        if msg is None:
            break
        log_lines.append(msg)
        yield "\n".join(log_lines), "", gr.update()

    root_logger.removeHandler(handler)
    t.join(timeout=5)
    _test_stop_event = None

    # 최종 Markdown 요약
    if "result" in result_holder:
        summary = _format_summary(result_holder["result"])
    else:
        err = result_holder.get("error", "알 수 없는 오류")
        summary = f"## ❌ 실행 실패\n\n> {err}"

    # 테스트 완료 후 녹화 목록 갱신
    yield "\n".join(log_lines), summary, gr.update(choices=_get_recording_choices())


# ──────────────────────────���──────────────────
# Gradio UI 빌드
# ─────────────────────────────────────────────

_CUSTOM_CSS = """
.step-row {
    align-items: flex-end !important;
}
.step-btn {
    margin-bottom: 7px !important;
}
.step-btn button {
    height: 42px !important;
    min-height: 42px !important;
    max-height: 42px !important;
    padding: 0 10px !important;
}
"""


def build_app() -> gr.Blocks:
    with gr.Blocks(title="QA 자동화 테스트", theme=gr.themes.Soft(), css=_CUSTOM_CSS) as app:

        gr.Markdown("# 📱 QA 자동화 테스트 도구")
        gr.Markdown(
            "자연어 시나리오를 입력하면 AI가 테스트 플랜을 생성하고 디바이스에서 자동 실행합니다."
        )

        with gr.Tabs():
                        # ═══════════════════════════════════════
            # Tab 1 — APK 관리 (설치 / 삭제)
            # ═══════════════════════════════════════
            with gr.TabItem("📦 APK 관리"):

                gr.Markdown("### APK 설치")
                gr.Markdown("`apks/` 폴더에 있는 APK 파일을 선택하여 연결된 디바이스에 설치합니다.")

                with gr.Row():
                    apk_dropdown = gr.Dropdown(
                        choices=_get_apk_choices(),
                        label="APK 파일 선택",
                        interactive=True,
                        scale=5,
                    )
                    apk_refresh_btn = gr.Button("🔄 새로고침", scale=1)

                apk_install_btn = gr.Button("📲 설치", variant="primary")
                apk_status = gr.Textbox(label="설치 상태", interactive=False, lines=3)

                apk_refresh_btn.click(fn=refresh_apks, outputs=[apk_dropdown])
                apk_install_btn.click(
                    fn=install_apk,
                    inputs=[apk_dropdown],
                    outputs=[apk_status],
                )

                gr.Markdown("---")
                gr.Markdown("### 앱 삭제")

                with gr.Row():
                    uninstall_dropdown = gr.Dropdown(
                        choices=INSTALLED_PACKAGES,
                        label="패키지 선택",
                        interactive=True,
                        scale=5,
                    )
                uninstall_btn = gr.Button("🗑️ 삭제", variant="stop")
                uninstall_status = gr.Textbox(label="삭제 상태", interactive=False, lines=3)

                uninstall_btn.click(
                    fn=uninstall_app,
                    inputs=[uninstall_dropdown],
                    outputs=[uninstall_status],
                )

            # ═══════════════════════════════════════
            # Tab 2 — 테스트 케이스 작성
            # ═══════════════════════════════════════
            with gr.TabItem("📝 테스트 케이스 작성"):

                gr.Markdown("### 샘플 시나리오")
                gr.Markdown("버튼을 클릭하면 예시 시나리오가 자동으로 입력됩니다.")

                with gr.Row():
                    sample_btns = [
                        gr.Button(label, size="sm", variant="secondary")
                        for label, _, _ in SAMPLE_SCENARIOS
                    ]

                gr.Markdown("---")

                with gr.Row():
                    # ── 왼쪽: 입력 영역 ──────────────────
                    with gr.Column(scale=1):
                        pkg_input = gr.Dropdown(
                            choices=INSTALLED_PACKAGES,
                            label="패키지명",
                            value="com.percent.aos.cooptd",
                            interactive=True,
                        )
                        scenario_input = gr.Textbox(
                            label="테스트 시나리오 (자연어)",
                            placeholder=SCENARIO_PLACEHOLDER,
                            lines=14,
                        )
                        with gr.Row():
                            gen_btn = gr.Button(
                                "🔍 플랜 생성", variant="primary", scale=2
                            )
                            save_btn = gr.Button(
                                "💾 저장", variant="secondary", scale=1
                            )
                        status_box = gr.Textbox(
                            label="상태", interactive=False, lines=1
                        )

                    # ── 오른쪽: YAML 미리보기 ─────────────
                    with gr.Column(scale=1):
                        yaml_box = gr.Code(
                            label="생성된 테스트 플랜 (YAML 미리보기)",
                            language="yaml",
                            lines=22,
                            interactive=True,
                        )

                # 샘플 버튼 이벤트
                for i, btn in enumerate(sample_btns):
                    btn.click(
                        fn=lambda i=i: load_sample(i),
                        outputs=[pkg_input, scenario_input],
                    )

                gen_btn.click(
                    fn=generate_plan,
                    inputs=[pkg_input, scenario_input],
                    outputs=[status_box, yaml_box],
                )

            # ═══════════════════════════════════════
            # Tab 3 — 템플릿 편집
            # ═══════════════════════════════════════
            with gr.TabItem("🧩 템플릿 편집"):

                gr.Markdown("### 템플릿 기반 테스트 실행")
                gr.Markdown(
                    "템플릿을 로드한 후 스텝을 편집·추가·삭제하여 바로 실행할 수 있습니다.  \n"
                    "`find_and_tap`, `verify`, `read_text` 액션의 **target** 값만 수정 가능합니다."
                )

                with gr.Row():
                    tpl_dropdown = gr.Dropdown(
                        choices=_get_template_choices(),
                        label="템플릿 선택",
                        interactive=True,
                        scale=4,
                    )
                    tpl_refresh_btn = gr.Button("🔄 새로고침", scale=1)
                    tpl_load_btn = gr.Button("📂 로드", variant="primary", scale=1)

                tpl_status = gr.Textbox(label="상태", interactive=False, lines=1)

                with gr.Row():
                    tpl_title = gr.Textbox(label="테스트 제목 (수정 가능)", scale=3)
                    tpl_package = gr.Dropdown(
                        choices=INSTALLED_PACKAGES,
                        label="패키지 (수정 가능)",
                        interactive=True,
                        scale=2,
                    )

                gr.Markdown("#### 스텝 목록")

                # 전체 스텝 행 (MAX개 미리 생성, 필요한 만큼만 표시)
                # 각 행: [Step N] [action] [target] [description] [🗑️]
                tpl_step_groups = []  # gr.Group containers
                tpl_actions = []      # action dropdowns
                tpl_targets = []      # target textboxes
                tpl_descs = []        # description textboxes
                tpl_del_btns = []     # per-row delete buttons
                tpl_up_btns = []      # per-row move up buttons
                tpl_down_btns = []    # per-row move down buttons

                for idx in range(MAX_TEMPLATE_STEPS):
                    with gr.Group(visible=False) as grp:
                        with gr.Row(elem_classes=["step-row"]):
                            act = gr.Dropdown(
                                choices=ACTION_CHOICES,
                                label="액션",
                                interactive=False,
                                allow_custom_value=True,
                                scale=2,
                                min_width=120,
                            )
                            tgt = gr.Textbox(
                                label="target",
                                interactive=False,
                                scale=3,
                            )
                            desc = gr.Textbox(
                                label="설명",
                                interactive=False,
                                scale=4,
                            )
                            up_btn = gr.Button(
                                "⬆", scale=0, min_width=42, size="sm",
                                elem_classes=["step-btn"],
                            )
                            down_btn = gr.Button(
                                "⬇", scale=0, min_width=42, size="sm",
                                elem_classes=["step-btn"],
                            )
                            del_btn = gr.Button(
                                "✕", variant="stop", scale=0, min_width=42,
                                size="sm", elem_classes=["step-btn"],
                            )
                    tpl_step_groups.append(grp)
                    tpl_actions.append(act)
                    tpl_targets.append(tgt)
                    tpl_descs.append(desc)
                    tpl_up_btns.append(up_btn)
                    tpl_down_btns.append(down_btn)
                    tpl_del_btns.append(del_btn)

                tpl_steps_yaml = gr.Textbox(visible=False)  # hidden state

                # ── 스텝 추가 컨트롤 ──
                gr.Markdown("#### 스텝 추가")
                with gr.Row():
                    tpl_add_action = gr.Dropdown(
                        choices=ACTION_CHOICES, label="추가할 액션",
                        value="find_and_tap", interactive=True, scale=2,
                    )
                    tpl_append_btn = gr.Button(
                        "➕ 맨 뒤에 추가", variant="primary", scale=1,
                    )

                with gr.Accordion("특정 위치에 삽입", open=False):
                    with gr.Row():
                        tpl_insert_pos = gr.Number(
                            label="삽입 위치 (1 = 맨 앞)",
                            value=1, precision=0, minimum=1, scale=1,
                        )
                        tpl_insert_action = gr.Dropdown(
                            choices=ACTION_CHOICES, label="액션",
                            value="find_and_tap", interactive=True, scale=2,
                        )
                        tpl_insert_btn = gr.Button(
                            "➕ 삽입", scale=1,
                        )

                gr.Markdown("---")

                with gr.Row():
                    tpl_run_btn = gr.Button("▶️ 바로 실행", variant="primary", scale=2)
                    tpl_stop_btn = gr.Button("⏹️ 중단", variant="stop", scale=1)

                tpl_log_box = gr.Textbox(
                    label="실시간 실행 로그",
                    lines=15,
                    interactive=False,
                    autoscroll=True,
                )
                tpl_summary_md = gr.Markdown(value="", label="실행 결과")

                # ── 이벤트 연결 ──

                # 공통 output 리스트: status + steps_yaml + *(group, action, target, desc) × MAX
                _step_ui_outputs_with_status = [tpl_status, tpl_steps_yaml]
                for i in range(MAX_TEMPLATE_STEPS):
                    _step_ui_outputs_with_status.extend([
                        tpl_step_groups[i], tpl_actions[i],
                        tpl_targets[i], tpl_descs[i],
                    ])

                # 로드 outputs: status, steps_yaml, title, package, *(group, action, target, desc) × MAX
                tpl_load_outputs = [tpl_status, tpl_steps_yaml, tpl_title, tpl_package]
                for i in range(MAX_TEMPLATE_STEPS):
                    tpl_load_outputs.extend([
                        tpl_step_groups[i], tpl_actions[i],
                        tpl_targets[i], tpl_descs[i],
                    ])

                tpl_refresh_btn.click(
                    fn=lambda: gr.update(choices=_get_template_choices()),
                    outputs=[tpl_dropdown],
                )

                tpl_load_btn.click(
                    fn=load_template_for_edit,
                    inputs=[tpl_dropdown],
                    outputs=tpl_load_outputs,
                    show_progress="hidden",
                )

                # 맨 뒤에 추가
                tpl_append_btn.click(
                    fn=append_step,
                    inputs=[tpl_steps_yaml, tpl_add_action],
                    outputs=_step_ui_outputs_with_status,
                    show_progress="hidden",
                )

                # 특정 위치에 삽입
                tpl_insert_btn.click(
                    fn=insert_step,
                    inputs=[tpl_steps_yaml, tpl_insert_pos, tpl_insert_action],
                    outputs=_step_ui_outputs_with_status,
                    show_progress="hidden",
                )

                # 각 행의 ⬆️⬇️🗑️ 버튼
                for i in range(MAX_TEMPLATE_STEPS):
                    tpl_up_btns[i].click(
                        fn=lambda yaml_str, pos=i: move_step(yaml_str, pos + 1, -1),
                        inputs=[tpl_steps_yaml],
                        outputs=_step_ui_outputs_with_status,
                        show_progress="hidden",
                    )
                    tpl_down_btns[i].click(
                        fn=lambda yaml_str, pos=i: move_step(yaml_str, pos + 1, +1),
                        inputs=[tpl_steps_yaml],
                        outputs=_step_ui_outputs_with_status,
                        show_progress="hidden",
                    )
                    tpl_del_btns[i].click(
                        fn=lambda yaml_str, pos=i: remove_step_at(yaml_str, pos + 1),
                        inputs=[tpl_steps_yaml],
                        outputs=_step_ui_outputs_with_status,
                        show_progress="hidden",
                    )

                # 실행 inputs: steps_yaml, title, package, *(action, target, desc) × MAX
                tpl_run_inputs = [tpl_steps_yaml, tpl_title, tpl_package]
                for i in range(MAX_TEMPLATE_STEPS):
                    tpl_run_inputs.extend([
                        tpl_actions[i], tpl_targets[i], tpl_descs[i],
                    ])

                tpl_run_event = tpl_run_btn.click(
                    fn=run_template,
                    inputs=tpl_run_inputs,
                    outputs=[tpl_log_box, tpl_summary_md],
                )

                tpl_stop_btn.click(
                    fn=stop_test,
                    outputs=[tpl_log_box],
                    cancels=[tpl_run_event],
                )

            # ═══════════════════════════════════════
            # Tab 4 — 테스트 실행
            # ═══════════════════════════════════════
            with gr.TabItem("▶️ 테스트 실행"):

                gr.Markdown("### 저장된 테스트 케이스 실행")

                with gr.Row():
                    tc_dropdown = gr.Dropdown(
                        choices=_get_testcase_choices(),
                        label="테스트 케이스 선택",
                        interactive=True,
                        scale=4,
                    )
                    refresh_btn = gr.Button("🔄 새로고침", scale=1)
                    run_btn = gr.Button("▶️ 실행", variant="primary", scale=1)
                    stop_btn = gr.Button("⏹️ 중단", variant="stop", scale=1)

                scenario_display = gr.Textbox(
                    label="원본 시나리오 (자연어)",
                    lines=5,
                    interactive=False,
                    placeholder="테스트 케이스를 선택하면 원본 자연어 시나리오가 표시됩니다.",
                )

                gr.Markdown(
                    "_테스트 실행 시 화면 녹화(3분 청크)가 자동으로 시작·종료됩니다._"
                )

                log_box = gr.Textbox(
                    label="실시간 실행 로그",
                    lines=20,
                    interactive=False,
                    autoscroll=True,
                )
                summary_md = gr.Markdown(value="", label="최종 결과 요약")

                tc_dropdown.change(
                    fn=load_testcase_info,
                    inputs=[tc_dropdown],
                    outputs=[scenario_display],
                )
                refresh_btn.click(fn=refresh_testcases, outputs=[tc_dropdown])

            # ═══════════════════════════════════════
            # Tab 5 — 녹화 영상
            # ═══════════════════════════════════════
            with gr.TabItem("🎬 녹화 영상"):

                gr.Markdown("### 녹화된 테스트 영상 다운로드")
                gr.Markdown(
                    "테스트 실행 시 자동 저장된 화면 녹화 파일을 선택하여 미리보기 및 다운로드할 수 있습니다."
                )

                with gr.Row():
                    rec_dropdown = gr.Dropdown(
                        choices=_get_recording_choices(),
                        label="녹화 파일 선택",
                        interactive=True,
                        scale=5,
                    )
                    rec_refresh_btn = gr.Button("🔄 새로고침", scale=1)

                rec_video = gr.Video(
                    label="녹화 영상 미리보기",
                    interactive=False,
                    height=360,
                )
                rec_file = gr.File(
                    label="다운로드",
                    interactive=False,
                )

                rec_dropdown.change(
                    fn=get_recording_file,
                    inputs=[rec_dropdown],
                    outputs=[rec_video, rec_file],
                )
                rec_refresh_btn.click(
                    fn=refresh_recordings,
                    outputs=[rec_dropdown],
                )

        # run_btn 은 Tab 3 의 rec_dropdown 까지 갱신하므로 탭 블록 바깥에서 연결
        run_event = run_btn.click(
            fn=run_test,
            inputs=[tc_dropdown],
            outputs=[log_box, summary_md, rec_dropdown],
        )

        # stop_btn: cancels 로 run_event 제너레이터를 강제 종료
        stop_btn.click(
            fn=stop_test,
            outputs=[log_box],
            cancels=[run_event],
        )

        # save_btn 은 status_box + tc_dropdown 둘 다 갱신
        save_btn.click(
            fn=save_plan,
            inputs=[yaml_box, scenario_input],
            outputs=[status_box, tc_dropdown],
        )

    return app


if __name__ == "__main__":
    cfg = _config()
    build_app().queue().launch(
        server_name="0.0.0.0",
        server_port=7860,
        share=False,
        allowed_paths=[str(cfg.paths.recordings_dir)],
    )
