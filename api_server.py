"""FastAPI 백엔드 서버 — auto-qa React 프론트엔드용 REST + WebSocket API"""
import asyncio
import logging
import os
import queue
import subprocess
import sys
import threading
import yaml
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from dotenv import load_dotenv
# 1. 기존 코드 (유지): 실행 파일 옆 .env 우선 로드
_exe_dir = Path(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__)).parent
load_dotenv(_exe_dir / ".env")
load_dotenv()  # 일반 실행 시 fallback

# 2. [핵심 추가]: 구글 인증 파일 환경 변수 강제 설정 ⭐
# .env에 GOOGLE_APPLICATION_CREDENTIALS="credentials.json" 이라고 되어 있어도 
# 라이브러리가 못 읽는 경우가 많아 아래처럼 직접 꽂아줘야 합니다.
_cred_path = _exe_dir / "credentials.json"
if _cred_path.exists():
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(_cred_path)
    print(f"[*] 구글 인증 파일 로드 성공: {_cred_path}")
else:
    print(f"[!] 경고: {_cred_path} 파일을 찾을 수 없습니다. (Vision API 에러 가능성)")
    
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from opentelemetry.sdk.trace import TracerProvider
from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor
_otel_provider = TracerProvider()
GoogleGenAIInstrumentor().instrument(tracer_provider=_otel_provider)
logging.getLogger("openinference.instrumentation.google_genai").setLevel(logging.CRITICAL)

from langfuse import Langfuse
_langfuse = Langfuse(tracer_provider=_otel_provider)

from config import Config
from adb_controller import ADBController
from planner_node import PlannerNode
from qa_orchestrator import QAOrchestrator
from test_manager import TestCase
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
cfg = Config()

apk_directory = cfg.paths.apks_dir 

logger.info(f"[*] 현재 베이스 경로 (EXE 위치): {cfg.paths.project_root}")
logger.info(f"[*] APK 폴더 경로: {apk_directory}")


app = FastAPI(title="auto-qa API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/recordings", StaticFiles(directory=str(cfg.paths.recordings_dir)), name="recordings")

# ─────────────────────────────────────────────
# 상수
# ─────────────────────────────────────────────
INSTALLED_PACKAGES = [
    "com.percent.aos.cooptd",
    "com.percent.aos.rollinghero",
    "com.supermagic.aos.statusman",
    "com.percent.aos.luckydefense",
    "com.percent.aos.arenago2",
]

STEP_MARKERS = ("━", "▶ ", "┌─", "│", "└─", "⏹️", "🔴", "  결과:", "  테스트 시작:", "  패키지:")

# WebSocket 세션 관리
_ws_queues: dict[str, asyncio.Queue] = {}
_test_stop_events: dict[str, threading.Event] = {}
_active_adb: dict[str, ADBController] = {}


# ─────────────────────────────────────────────
# Pydantic 모델
# ─────────────────────────────────────────────
class InstallApkRequest(BaseModel):
    filename: str

class UninstallRequest(BaseModel):
    package: str

class GeneratePlanRequest(BaseModel):
    scenario: str
    package: str = ""

class SaveTemplateRequest(BaseModel):
    name: str
    content: str  # YAML string
    scenario: str = ""

class RunTestRequest(BaseModel):
    title: str
    package: str
    steps: list[dict]
    session_id: str
    record: bool = False

class StopTestRequest(BaseModel):
    session_id: str


# ─────────────────────────────────────────────
# 디바이스 API
# ─────────────────────────────────────────────
def _get_all_devices() -> list[dict]:
    result = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=5)
    devices = []
    for ln in result.stdout.split("\n")[1:]:
        if "\t" not in ln:
            continue
        device_id, status = ln.split("\t")[0].strip(), ln.split("\t")[1].strip()
        model = subprocess.run(
            ["adb", "-s", device_id, "shell", "getprop", "ro.product.model"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip() or device_id
        devices.append({"device_id": device_id, "model": model, "status": status})
    return devices

@app.get("/api/device")
def get_device():
    try:
        devices = [d for d in _get_all_devices() if d["status"] == "device"]
        if not devices:
            return {"status": "disconnected", "device_id": None, "model": None}
        preferred = os.getenv("ADB_DEVICE", "").strip()
        device = next((d for d in devices if d["device_id"] == preferred), devices[0])
        return {"status": "connected", "device_id": device["device_id"], "model": device["model"]}
    except Exception as e:
        return {"status": "error", "error": str(e), "device_id": None, "model": None}

@app.get("/api/devices")
def list_devices():
    """연결된 모든 디바이스 목록"""
    try:
        devices = [d for d in _get_all_devices() if d["status"] == "device"]
        return {"devices": devices}
    except Exception as e:
        return {"devices": [], "error": str(e)}

@app.post("/api/devices/connect")
def connect_device(body: dict):
    """ADB WiFi 디바이스 연결 — body: {address: '192.168.1.10:5555'}"""
    address = body.get("address", "").strip()
    if not address:
        raise HTTPException(status_code=400, detail="address가 필요합니다.")
    try:
        result = subprocess.run(["adb", "connect", address], capture_output=True, text=True, timeout=10)
        output = result.stdout.strip()
        success = "connected" in output.lower() or "already connected" in output.lower()
        return {"success": success, "message": output}
    except Exception as e:
        return {"success": False, "message": str(e)}

@app.post("/api/devices/disconnect")
def disconnect_device(body: dict):
    """ADB WiFi 디바이스 연결 해제 — body: {address: '192.168.1.10:5555'}"""
    address = body.get("address", "").strip()
    if not address:
        raise HTTPException(status_code=400, detail="address가 필요합니다.")
    try:
        result = subprocess.run(["adb", "disconnect", address], capture_output=True, text=True, timeout=10)
        return {"success": True, "message": result.stdout.strip()}
    except Exception as e:
        return {"success": False, "message": str(e)}


# ─────────────────────────────────────────────
# APK API
# ─────────────────────────────────────────────
@app.get("/api/apks")
def list_apks():
    try:
        files = sorted(cfg.paths.apks_dir.glob("*.apk"), key=lambda f: f.stat().st_mtime, reverse=True)
        return {"apks": [f.name for f in files]}
    except Exception as e:
        return {"apks": [], "error": str(e)}

@app.post("/api/apk/install")
def install_apk(req: InstallApkRequest):
    # [수정] 전역 변수 apk_directory(Path객체) 사용
    apk_path = apk_directory / req.filename
    if not apk_path.exists():
        raise HTTPException(status_code=404, detail=f"APK not found: {apk_path}")
    try:
        adb = ADBController()
        # Path 객체를 문자열로 변환하여 전달
        _, msg = adb.install_apk(str(apk_path))
        return {"success": True, "message": msg}
    except Exception as e:
        return {"success": False, "message": str(e)}

# ─────────────────────────────────────────────
# 패키지 API
# ─────────────────────────────────────────────
@app.get("/api/packages")
def list_packages():
    return {"packages": INSTALLED_PACKAGES}

@app.get("/api/package-apk-map")
def get_package_apk_map():
    # [수정] .exe 내부(bundle_root)에 포함된 json 파일을 찾도록 수정
    # 만약 빌드 시 --add-data에 포함시키지 않았다면 exe 옆(project_root)을 보게 하세요.
    map_path = cfg.paths.bundle_root / "package_apk_map.json"
    try:
        import json
        with open(map_path, encoding="utf-8") as f:
            return {"map": json.load(f)}
    except Exception:
        return {"map": {}}
    


@app.post("/api/app/uninstall")
def uninstall_app(req: UninstallRequest):
    try:
        adb = ADBController()
        _, msg = adb.uninstall_app(req.package)
        return {"success": True, "message": msg}
    except Exception as e:
        return {"success": False, "message": str(e)}


# ─────────────────────────────────────────────
# 템플릿 API
# ─────────────────────────────────────────────
@app.get("/api/templates")
def list_templates():
    try:
        files = sorted(cfg.paths.templates_dir.glob("*.yaml"))
        return {"templates": [f.stem for f in files]}
    except Exception as e:
        return {"templates": [], "error": str(e)}

@app.get("/api/templates/{name}")
def get_template(name: str):
    path = cfg.paths.templates_dir / f"{name}.yaml"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Template not found: {name}")
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return {"template": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/templates")
def save_template(req: SaveTemplateRequest):
    try:
        data = yaml.safe_load(req.content)
        if req.scenario:
            data["source_scenario"] = req.scenario
        data.setdefault("preconditions", [])
        safe_name = req.name.replace(" ", "_")
        out_path = cfg.paths.templates_dir / f"{safe_name}.yaml"
        with open(out_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, sort_keys=False)
        return {"success": True, "filename": out_path.name}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.delete("/api/templates/{name}")
def delete_template(name: str):
    path = cfg.paths.templates_dir / f"{name}.yaml"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Template not found: {name}")
    path.unlink()
    return {"success": True}


# ─────────────────────────────────────────────
# 플랜 생성 API
# ─────────────────────────────────────────────
@app.post("/api/plan/generate")
def generate_plan(req: GeneratePlanRequest):
    if not req.scenario.strip():
        raise HTTPException(status_code=400, detail="시나리오를 입력해주세요.")
    try:
        planner = PlannerNode(
            project=cfg.gemini.project,
            location=cfg.gemini.location,
            model=cfg.gemini.model,
        )
        plan = planner.create_test_plan(req.scenario.strip(), req.package.strip())
        yaml_data = {
            "title": plan.title,
            "description": plan.description,
            "package": plan.package,
            "steps": [s.model_dump() for s in plan.steps],
            "expected_results": plan.expected_results,
        }
        yaml_str = yaml.dump(yaml_data, allow_unicode=True, sort_keys=False)
        return {
            "title": plan.title,
            "steps_count": len(plan.steps),
            "yaml": yaml_str,
        }
    except Exception as e:
        logger.exception("generate_plan failed")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────
# 테스트 실행 API
# ─────────────────────────────────────────────
@app.post("/api/test/run")
async def run_test(req: RunTestRequest):
    session_id = req.session_id
    if session_id in _test_stop_events and not _test_stop_events[session_id].is_set():
        raise HTTPException(status_code=409, detail="테스트가 이미 실행 중입니다.")

    stop_event = threading.Event()
    _test_stop_events[session_id] = stop_event

    loop = asyncio.get_event_loop()
    q: asyncio.Queue = asyncio.Queue()
    _ws_queues[session_id] = q

    steps = req.steps
    for step in steps:
        if step.get("action") in ("launch_app", "uninstall_app") and step.get("target"):
            step.setdefault("params", {})["package"] = step["target"]

    pkg = req.package or next((s.get("target") for s in steps if s.get("action") == "launch_app" and s.get("target")), "")

    tc_data = {
        "id": session_id,
        "title": req.title or "React 실행",
        "description": "",
        "package": pkg,
        "steps": steps,
        "expected_results": [],
        "preconditions": [],
    }
    testcase = TestCase(**tc_data)

    class _WSLogHandler(logging.Handler):
        def emit(self, record):
            msg = record.getMessage()
            if any(m in msg for m in STEP_MARKERS):
                asyncio.run_coroutine_threadsafe(q.put({"type": "log", "message": msg}), loop)

    handler = _WSLogHandler()

    should_record = req.record

    def _run_thread():
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        adb_rec = None
        if should_record:
            try:
                adb_rec = ADBController()
                _active_adb[session_id] = adb_rec
                session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                adb_rec.start_recording(cfg.paths.recordings_dir, session_ts)
                asyncio.run_coroutine_threadsafe(
                    q.put({"type": "log", "message": f"🔴 화면 녹화 시작 — 세션: {session_ts}"}), loop
                )
            except Exception as e:
                asyncio.run_coroutine_threadsafe(
                    q.put({"type": "log", "message": f"⚠️ 녹화 시작 실패: {e}"}), loop
                )
                adb_rec = None

        try:
            orchestrator = QAOrchestrator(cfg)
            result = orchestrator.run_test(session_id, stop_event, testcase_override=testcase)
            summary = {
                "status": result.status,
                "title": result.title,
                "steps_passed": result.steps_passed,
                "steps_executed": result.steps_executed,
                "error_message": result.error_message,
                "step_results": result.step_results or [],
            }
            asyncio.run_coroutine_threadsafe(
                q.put({"type": "result", "data": summary}), loop
            )
        except Exception as e:
            logger.exception("run_test thread failed")
            asyncio.run_coroutine_threadsafe(
                q.put({"type": "error", "message": str(e)}), loop
            )
        finally:
            if adb_rec and adb_rec.is_recording:
                try:
                    files = adb_rec.stop_recording()
                    asyncio.run_coroutine_threadsafe(
                        q.put({"type": "log", "message": f"⏹️ 녹화 완료 — {len(files or [])}개 청크"}), loop
                    )
                except Exception:
                    pass
            _active_adb.pop(session_id, None)
            _test_stop_events.pop(session_id, None)  # 스레드 종료 후 정리
            root_logger.removeHandler(handler)
            asyncio.run_coroutine_threadsafe(q.put({"type": "done"}), loop)

    threading.Thread(target=_run_thread, daemon=True).start()
    return {"session_id": session_id, "status": "started"}


@app.post("/api/test/stop")
def stop_test(req: StopTestRequest):
    event = _test_stop_events.get(req.session_id)
    if event and not event.is_set():
        event.set()
        return {"success": True, "message": "중단 요청됨"}
    return {"success": False, "message": "실행 중인 테스트가 없습니다."}


# ─────────────────────────────────────────────
# 녹화 영상 API
# ─────────────────────────────────────────────
@app.get("/api/recordings")
def list_recordings():
    try:
        files = sorted(
            cfg.paths.recordings_dir.glob("*.mp4"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        return {"recordings": [f.name for f in files]}
    except Exception as e:
        return {"recordings": [], "error": str(e)}


# ─────────────────────────────────────────────
# WebSocket — 실시간 로그 스트리밍
# ─────────────────────────────────────────────
@app.websocket("/ws/logs/{session_id}")
async def ws_logs(websocket: WebSocket, session_id: str):
    await websocket.accept()
    # 큐가 생성될 때까지 최대 5초 대기
    for _ in range(50):
        if session_id in _ws_queues:
            break
        await asyncio.sleep(0.1)

    q = _ws_queues.get(session_id)
    if not q:
        await websocket.send_json({"type": "error", "message": "세션을 찾을 수 없습니다."})
        await websocket.close()
        return

    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=30.0)
                await websocket.send_json(msg)
                if msg.get("type") in ("done", "error"):
                    break
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        pass
    finally:
        _ws_queues.pop(session_id, None)
        # _test_stop_events는 스레드 finally에서 정리 — 여기서 지우면 stop API가 동작 안 함


# ─────────────────────────────────────────────
# 4. SPA 정적 파일 서빙 (가장 중요 ⭐)
# ─────────────────────────────────────────────
# [수정] frontend/dist는 빌드 시 내부에 포함되므로 bundle_root를 참조해야 함
_dist = cfg.paths.bundle_root / "frontend" / "dist"

if _dist.exists():
    app.mount("/assets", StaticFiles(directory=str(_dist / "assets")), name="assets")

    @app.get("/")
    async def serve_root():
        return FileResponse(str(_dist / "index.html"))

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        candidate = _dist / full_path
        if candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(_dist / "index.html"))
else:
    logger.error(f"Frontend dist not found at: {_dist}")
# ─────────────────────────────────────────────
# 엔트리포인트
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
