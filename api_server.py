"""FastAPI 백엔드 서버 — auto-qa React 프론트엔드용 REST + WebSocket API"""
import asyncio
import json
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

# 2. 로컬 Google credentials가 있으면 직접 지정한다.
# 사내 LLM Gateway 사용 시에는 credentials.json이 없어도 정상 경로이므로 경고하지 않는다.
_cred_path = _exe_dir / "credentials.json"
if _cred_path.exists():
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(_cred_path)
    print(f"[*] 구글 인증 파일 로드 성공: {_cred_path}")
    
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import Config
from adb_controller import ADBController
from planner_node import PlannerNode
from qa_orchestrator import QAOrchestrator
from test_manager import TestCase
from unity_api_client import UnityAPIClient
# 어댑티브 QA는 현재 실행 안정성이 낮아 일시 비활성화한다.
# from adaptive_qa_agent import AdaptiveQARunner, AdaptiveRunRequest
from csv_reporter import build_test_result_csv
from sr_debugger import SRDebuggerController, SRDebuggerEnterRequest
from eval_platform import (
    EvalCaseCreate,
    EvalCsvImportRequest,
    EvalRunRequest,
    EvaluationStore,
    build_report,
    run_eval_suite,
)
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
cfg = Config()

apk_directory = cfg.paths.apks_dir
_pipelines_dir = cfg.paths.project_root / "pipelines"
_pipelines_dir.mkdir(parents=True, exist_ok=True)
_eval_store = EvaluationStore(cfg.paths.project_root / "eval_platform" / "eval_platform.db")
# _adaptive_runner = AdaptiveQARunner(cfg)

logger.info(f"[*] 현재 베이스 경로 (EXE 위치): {cfg.paths.project_root}")
logger.info(f"[*] APK 폴더 경로: {apk_directory}")


app = FastAPI(title="auto-qa API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _no_cache_api(request, call_next):
    """/api/* 응답은 절대 캐시하지 않도록 강제.

    프록시/전환/에러 상황에서 API 경로가 HTML(index.html) 등으로 응답된 게
    브라우저에 캐시되면, 이후 정상 JSON 대신 캐시된 HTML이 반환되어
    프론트가 깨지는(검정 화면) 문제가 생긴다. 이를 원천 차단한다.
    """
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


app.mount("/recordings", StaticFiles(directory=str(cfg.paths.recordings_dir)), name="recordings")
app.mount("/reports", StaticFiles(directory=str(cfg.paths.reports_dir)), name="reports")

# 디버그 탭 검증 스크린샷 (녹화 대신 리소스 간소화용)
cfg.paths.debug_dir.mkdir(parents=True, exist_ok=True)
app.mount("/debug", StaticFiles(directory=str(cfg.paths.debug_dir)), name="debug")

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
# 디바이스 실행 락 — 같은 폰에 테스트 2개가 동시에 붙는 것 방지
# ─────────────────────────────────────────────
_device_locks: dict[str, str] = {}  # device_id → session_id
_device_locks_guard = threading.Lock()


def _acquire_device_lock(device_id: str, session_id: str) -> Optional[str]:
    """락 획득. 성공 시 None, 실패 시 점유 중인 session_id 반환."""
    with _device_locks_guard:
        holder = _device_locks.get(device_id)
        if holder and holder != session_id:
            return holder
        _device_locks[device_id] = session_id
        return None


def _release_device_lock(device_id: str, session_id: str) -> None:
    with _device_locks_guard:
        if _device_locks.get(device_id) == session_id:
            _device_locks.pop(device_id, None)


# ─────────────────────────────────────────────
# 무선 디바이스 레지스트리 + 자동 재연결
# 등록된 주소(IP:PORT)를 파일로 영속화하고, 끊기면 백그라운드에서 재연결 시도
# ─────────────────────────────────────────────
_DEVICE_REGISTRY_PATH = Path(
    os.getenv("DEVICE_REGISTRY_PATH", str(cfg.paths.project_root / "state" / "devices.json"))
)
_registry_guard = threading.Lock()
RECONNECT_INTERVAL_SEC = int(os.getenv("DEVICE_RECONNECT_INTERVAL", "30"))


def _load_device_registry() -> list[str]:
    try:
        with _registry_guard:
            if _DEVICE_REGISTRY_PATH.exists():
                data = json.loads(_DEVICE_REGISTRY_PATH.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return [str(a) for a in data]
    except Exception as e:
        logger.warning("디바이스 레지스트리 로드 실패: %s", e)
    return []


def _save_device_registry(addresses: list[str]) -> None:
    try:
        with _registry_guard:
            _DEVICE_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
            _DEVICE_REGISTRY_PATH.write_text(
                json.dumps(sorted(set(addresses)), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    except Exception as e:
        logger.warning("디바이스 레지스트리 저장 실패: %s", e)


def _registry_add(address: str) -> None:
    addrs = _load_device_registry()
    if address not in addrs:
        addrs.append(address)
        _save_device_registry(addrs)


def _registry_remove(address: str) -> None:
    addrs = _load_device_registry()
    if address in addrs:
        addrs.remove(address)
        _save_device_registry(addrs)


def _adb_connect(address: str, timeout: int = 10) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["adb", "connect", address],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        output = (result.stdout or "").strip()
        ok = "connected" in output.lower() and "cannot" not in output.lower()
        return ok, output
    except Exception as e:
        return False, str(e)


def _device_reconnect_loop():
    """등록된 무선 디바이스가 끊기면 주기적으로 adb connect 재시도."""
    import time as _time
    while True:
        _time.sleep(RECONNECT_INTERVAL_SEC)
        try:
            registered = _load_device_registry()
            if not registered:
                continue
            connected = {d["device_id"] for d in _get_all_devices() if d["status"] == "device"}
            for addr in registered:
                if addr not in connected:
                    ok, msg = _adb_connect(addr, timeout=5)
                    if ok:
                        logger.info("무선 디바이스 자동 재연결 성공: %s", addr)
                    else:
                        logger.debug("무선 디바이스 재연결 실패 (%s): %s", addr, msg)
        except Exception as e:
            logger.warning("디바이스 재연결 루프 오류: %s", e)


@app.on_event("startup")
def _startup_device_manager():
    try:
        subprocess.run(["adb", "start-server"], capture_output=True, timeout=15)
    except Exception as e:
        logger.warning("adb start-server 실패: %s", e)
    # 서버 재시작 시 등록된 디바이스 일괄 재연결
    for addr in _load_device_registry():
        _adb_connect(addr, timeout=5)
    threading.Thread(target=_device_reconnect_loop, daemon=True).start()
    logger.info("디바이스 자동 재연결 루프 시작 (interval=%ds)", RECONNECT_INTERVAL_SEC)


def _test_result_summary(result) -> dict:
    return {
        "test_id": result.test_id,
        "status": result.status,
        "title": result.title,
        "start_time": result.start_time.isoformat() if result.start_time else None,
        "end_time": result.end_time.isoformat() if result.end_time else None,
        "steps_passed": result.steps_passed,
        "steps_executed": result.steps_executed,
        "error_message": result.error_message,
        "screenshots": result.screenshots or [],
        "step_results": result.step_results or [],
        "eval_output": result.eval_output,
    }


# ─────────────────────────────────────────────
# Pydantic 모델
# ─────────────────────────────────────────────
class InstallApkRequest(BaseModel):
    filename: str
    device: str = ""

class UninstallRequest(BaseModel):
    package: str
    device: str = ""

class PackageApkMapEntry(BaseModel):
    package: str
    apk: str

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
    device: str = ""  # 대상 디바이스 시리얼 (미지정 시 기본 디바이스)

class StopTestRequest(BaseModel):
    session_id: str

class TutorialPassRequest(BaseModel):
    package: str

class ReportExportRequest(BaseModel):
    result: dict
    taps: list[dict] = []

class SavePipelineRequest(BaseModel):
    name: str
    steps: list[dict] = []
    nodes: list[dict] = []
    edges: list[dict] = []

class RunPipelineRequest(BaseModel):
    steps: list[dict] = []      # flat step list (신형)
    nodes: list[dict] = []      # 구형 호환용
    edges: list[dict] = []
    session_id: str
    record: bool = False
    device: str = ""  # 대상 디바이스 시리얼 (미지정 시 기본 디바이스)


# ─────────────────────────────────────────────
# 디바이스 API
# ─────────────────────────────────────────────
def _get_all_devices() -> list[dict]:
    result = subprocess.run(["adb", "devices"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
    devices = []
    for ln in result.stdout.split("\n")[1:]:
        if "\t" not in ln:
            continue
        device_id, status = ln.split("\t")[0].strip(), ln.split("\t")[1].strip()
        model = subprocess.run(
            ["adb", "-s", device_id, "shell", "getprop", "ro.product.model"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
        ).stdout.strip() or device_id
        devices.append({"device_id": device_id, "model": model, "status": status})
    return devices

@app.get("/api/device")
def get_device(device: str = ""):
    """디바이스 상태. device 지정 시 해당 디바이스, 미지정 시 기본 디바이스."""
    try:
        devices = [d for d in _get_all_devices() if d["status"] == "device"]
        if not devices:
            return {"status": "disconnected", "device_id": None, "model": None}
        preferred = device.strip() or os.getenv("ADB_DEVICE", "").strip()
        found = next((d for d in devices if d["device_id"] == preferred), None)
        if device.strip() and not found:
            return {"status": "disconnected", "device_id": device.strip(), "model": None}
        target = found or devices[0]
        return {"status": "connected", "device_id": target["device_id"], "model": target["model"]}
    except Exception as e:
        return {"status": "error", "error": str(e), "device_id": None, "model": None}

@app.get("/api/devices")
def list_devices():
    """연결된 디바이스 + 등록됐지만 오프라인인 무선 디바이스 목록 (busy 상태 포함)"""
    try:
        devices = [d for d in _get_all_devices() if d["status"] == "device"]
        with _device_locks_guard:
            locks = dict(_device_locks)
        connected_ids = {d["device_id"] for d in devices}
        registered = _load_device_registry()
        for d in devices:
            d["busy"] = d["device_id"] in locks
            d["registered"] = d["device_id"] in registered
        # 등록됐지만 현재 끊긴 무선 디바이스도 목록에 노출 (자동 재연결 대상)
        for addr in registered:
            if addr not in connected_ids:
                devices.append({
                    "device_id": addr, "model": addr, "status": "offline",
                    "busy": False, "registered": True,
                })
        return {"devices": devices}
    except Exception as e:
        return {"devices": [], "error": str(e)}

@app.post("/api/devices/connect")
def connect_device(body: dict):
    """ADB WiFi 디바이스 연결 + 자동 재연결 레지스트리 등록 — body: {address: '192.168.1.10:5555'}"""
    address = body.get("address", "").strip()
    if not address:
        raise HTTPException(status_code=400, detail="address가 필요합니다.")
    if ":" not in address:
        address = f"{address}:5555"  # 포트 생략 시 기본 5555
    success, output = _adb_connect(address)
    if success:
        _registry_add(address)
    return {"success": success, "message": output, "address": address}

@app.post("/api/devices/disconnect")
def disconnect_device(body: dict):
    """ADB WiFi 디바이스 연결 해제 + 레지스트리에서 제거 — body: {address: '192.168.1.10:5555'}"""
    address = body.get("address", "").strip()
    if not address:
        raise HTTPException(status_code=400, detail="address가 필요합니다.")
    try:
        result = subprocess.run(["adb", "disconnect", address], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        _registry_remove(address)
        return {"success": True, "message": result.stdout.strip()}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.post("/api/device/reconnect")
def device_reconnect():
    """기기 재부팅 후 ADB 자동 복구: start-server → connect(무선) → 연결 확인"""
    import time as _time
    log: list[str] = []

    # 1. ADB 서버 재시작
    try:
        r = subprocess.run(["adb", "start-server"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
        msg = r.stdout.strip() or r.stderr.strip() or "OK"
        log.append(f"✅ adb start-server: {msg}")
    except Exception as e:
        log.append(f"❌ adb start-server 실패: {e}")

    # 2. 무선 연결 — 레지스트리에 등록된 모든 디바이스 + ADB_DEVICE(IP:PORT 형식)
    targets = _load_device_registry()
    preferred = os.getenv("ADB_DEVICE", "").strip()
    if preferred and ":" in preferred and preferred not in targets:
        targets.append(preferred)
    if targets:
        for addr in targets:
            success, out = _adb_connect(addr, timeout=15)
            log.append(f"{'✅' if success else '⚠️'} adb connect {addr}: {out}")
    else:
        log.append("ℹ️ 등록된 무선 디바이스 없음 (USB 연결 모드)")

    # 3. 잠시 대기 후 연결 확인
    _time.sleep(2)
    device = get_device()
    connected = device["status"] == "connected"
    if connected:
        log.append(f"✅ 연결 성공: {device.get('model')} ({device.get('device_id')})")
    else:
        log.append("❌ 연결 실패 — 기기 상태 및 USB 디버깅 설정을 확인하세요.")

    return {"success": connected, "device": device, "log": log}


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
        adb = ADBController(req.device or None)
        ok, msg = adb.install_apk(apk_path)
        return {"success": ok, "message": msg}
    except Exception as e:
        return {"success": False, "message": str(e)}

# ─────────────────────────────────────────────
# 수동 디바이스 조작 — 테스트 스텝/보고서와 무관한 즉석 adb 입력
# ─────────────────────────────────────────────
class DeviceControlRequest(BaseModel):
    action: str
    device: str = ""


_CONTROL_KEYMAP = {
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "recents": "KEYCODE_APP_SWITCH",
    "wake": "KEYCODE_WAKEUP",
    "enter": "KEYCODE_ENTER",
}


@app.post("/api/device/control")
def device_control(req: DeviceControlRequest):
    """수동 조작: back/home/recents/wake/enter/scroll_up/scroll_down"""
    device_info = get_device(req.device)
    if device_info["status"] != "connected":
        raise HTTPException(status_code=503, detail="디바이스 미연결")
    did = device_info["device_id"]
    try:
        if req.action in _CONTROL_KEYMAP:
            _adb_shell(did, ["shell", "input", "keyevent", _CONTROL_KEYMAP[req.action]])
        elif req.action in ("scroll_up", "scroll_down"):
            out = _adb_shell(did, ["shell", "wm", "size"])
            try:
                w, h = (int(v) for v in out.split()[-1].split("x"))
            except Exception:
                w, h = 1080, 1920
            x = w // 2
            y1, y2 = (int(h * 0.35), int(h * 0.70)) if req.action == "scroll_up" \
                else (int(h * 0.70), int(h * 0.35))
            _adb_shell(did, ["shell", "input", "swipe", str(x), str(y1), str(x), str(y2), "400"])
        else:
            raise HTTPException(status_code=400, detail=f"지원하지 않는 action: {req.action}")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        return {"success": False, "message": str(e)}


# ─────────────────────────────────────────────
# Firebase App Distribution — 빌드 조회/설치
# ─────────────────────────────────────────────
from app_distribution import AppDistributionClient

_appdist = AppDistributionClient(
    config_path=cfg.paths.project_root / "firebase_apps.json",
    cache_dir=cfg.paths.apks_dir / "appdist",
)


class AppDistInstallRequest(BaseModel):
    app: str            # firebase_apps.json의 키 (패키지명)
    release_name: str   # projects/.../apps/.../releases/... 전체 리소스 이름
    device: str = ""


@app.get("/api/appdist/apps")
def appdist_apps():
    """App Distribution 연동 설정된 앱 목록 + 인증 가능 여부"""
    return {"apps": list(_appdist.apps().keys()), "configured": _appdist.configured()}


@app.get("/api/appdist/releases")
def appdist_releases(package: str):
    """해당 앱의 빌드(릴리스) 목록 — 최신순"""
    try:
        return {"releases": _appdist.list_releases(package)}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("AppDist 릴리스 조회 실패 (%s): %s", package, e)
        raise HTTPException(status_code=502, detail=f"App Distribution 조회 실패: {e}")


@app.post("/api/appdist/install")
def appdist_install(req: AppDistInstallRequest):
    """빌드 다운로드(캐시) 후 디바이스에 설치"""
    try:
        apk_path = _appdist.download_release(req.app, req.release_name)
    except Exception as e:
        logger.error("AppDist 다운로드 실패: %s", e)
        return {"success": False, "message": f"다운로드 실패: {e}"}
    try:
        adb = ADBController(req.device or None)
        ok, msg = adb.install_apk(apk_path)
        return {"success": ok, "message": msg, "apk": apk_path.name}
    except Exception as e:
        return {"success": False, "message": str(e)}


# ─────────────────────────────────────────────
# 패키지 API
# ─────────────────────────────────────────────
@app.get("/api/packages")
def list_packages(device: str = ""):
    # APK 파일명에서 키워드 추출 (확장자 제거, 소문자)
    apk_keywords = [
        f.stem.lower()
        for f in apk_directory.glob("*.apk")
    ]
    try:
        device_info = get_device(device)
        if device_info["status"] == "connected":
            did = device_info["device_id"]
            out = _adb_shell(did, ["shell", "pm", "list", "packages"])
            all_pkgs = [
                line.replace("package:", "").strip()
                for line in out.splitlines()
                if line.startswith("package:")
            ]
            if apk_keywords:
                filtered = sorted(
                    p for p in all_pkgs
                    if any(kw in p.lower() for kw in apk_keywords)
                )
                if filtered:
                    return {"packages": filtered}
            elif all_pkgs:
                return {"packages": sorted(all_pkgs)}
    except Exception:
        pass
    # 폴백: apk 키워드로 하드코딩 목록 필터링
    if apk_keywords:
        return {"packages": [p for p in INSTALLED_PACKAGES if any(kw in p.lower() for kw in apk_keywords)]}
    return {"packages": INSTALLED_PACKAGES}

@app.get("/api/package-apk-map")
def get_package_apk_map():
    import json
    for map_path in [
        cfg.paths.project_root / "package_apk_map.json",
        cfg.paths.bundle_root / "package_apk_map.json",
    ]:
        try:
            if map_path.exists():
                with open(map_path, encoding="utf-8") as f:
                    return {"map": json.load(f)}
        except Exception:
            pass
    return {"map": {}}

@app.post("/api/package-apk-map")
def add_package_apk_map(entry: PackageApkMapEntry):
    import json
    map_path = cfg.paths.project_root / "package_apk_map.json"
    try:
        data = json.loads(map_path.read_text(encoding="utf-8")) if map_path.exists() else {}
        data[entry.package] = entry.apk
        map_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"success": True, "map": data}
    except Exception as e:
        return {"success": False, "message": str(e)}

@app.delete("/api/package-apk-map/{package:path}")
def delete_package_apk_map(package: str):
    import json
    map_path = cfg.paths.project_root / "package_apk_map.json"
    try:
        data = json.loads(map_path.read_text(encoding="utf-8")) if map_path.exists() else {}
        data.pop(package, None)
        map_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"success": True, "map": data}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.post("/api/app/uninstall")
def uninstall_app(req: UninstallRequest):
    try:
        adb = ADBController(req.device or None)
        _, msg = adb.uninstall_app(req.package)
        return {"success": True, "message": msg}
    except Exception as e:
        return {"success": False, "message": str(e)}


# ─────────────────────────────────────────────
# 템플릿 API
# ─────────────────────────────────────────────
def _template_roots() -> dict[str, Path]:
    return {
        "templates": cfg.paths.templates_dir,
        "game_testcases": cfg.paths.project_root / "game_testcases",
        "testcases": cfg.paths.testcases_dir,
    }


def _resolve_template_path(name: str) -> Path | None:
    clean = name.strip().strip("/")
    roots = _template_roots()
    if "/" in clean:
        prefix, stem = clean.split("/", 1)
        root = roots.get(prefix)
        if root:
            path = root / f"{Path(stem).stem}.yaml"
            return path if path.exists() else None
    for root in roots.values():
        path = root / f"{Path(clean).stem}.yaml"
        if path.exists():
            return path
    return None


@app.get("/api/templates")
def list_templates():
    try:
        names: list[str] = []
        for prefix, root in _template_roots().items():
            if not root.exists():
                continue
            for f in sorted(root.glob("*.yaml")):
                names.append(f"{prefix}/{f.stem}")
        return {"templates": names}
    except Exception as e:
        return {"templates": [], "error": str(e)}

@app.get("/api/templates/{name:path}")
def get_template(name: str):
    path = _resolve_template_path(name)
    if not path:
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

@app.delete("/api/templates/{name:path}")
def delete_template(name: str):
    path = _resolve_template_path(name)
    if not path:
        raise HTTPException(status_code=404, detail=f"Template not found: {name}")
    path.unlink()
    return {"success": True}


# ─────────────────────────────────────────────
# 사전 점검 API
# ─────────────────────────────────────────────
def _adb_shell(device_id: str, args: list[str], timeout: int = 8) -> str:
    try:
        result = subprocess.run(
            ["adb", "-s", device_id] + args,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        return result.stdout + result.stderr
    except Exception:
        return ""


@app.get("/api/preflight")
def preflight_check(device: str = ""):
    checks = []

    # 1. ADB 연결
    device_info = get_device(device)
    connected = device_info["status"] == "connected"
    checks.append({
        "name": "ADB 연결",
        "status": "ok" if connected else "fail",
        "detail": f"{device_info.get('model')} ({device_info.get('device_id')})" if connected else "디바이스 없음",
    })

    if not connected:
        for name in ["Google 계정", "네트워크 연결", "화면 잠금 해제"]:
            checks.append({"name": name, "status": "unknown", "detail": "ADB 연결 필요"})
        return {"checks": checks}

    did = device_info["device_id"]

    # 2. Google 계정 (Play Store 로그인)
    try:
        import re as _re2
        out = _adb_shell(did, ["shell", "dumpsys", "account"])
        # name=xxx@xxx 형식에서 이메일만 추출
        emails = []
        for ln in out.splitlines():
            if "Account {" in ln and "google" in ln.lower():
                m = _re2.search(r'name=([^\s,}]+)', ln)
                if m:
                    emails.append(m.group(1))
        has_google = len(emails) > 0
        if emails:
            detail = ", ".join(emails) + f" ({len(emails)}개)"
        else:
            detail = "Google 계정 없음 — Play Store 결제 불가"
        checks.append({"name": "Google 계정", "status": "ok" if has_google else "warn", "detail": detail})
    except Exception as e:
        checks.append({"name": "Google 계정", "status": "warn", "detail": f"확인 실패: {e}"})

    # 3. WiFi 연결 여부 + SSID
    try:
        import re as _re

        def _extract_ssid(text: str) -> str:
            for pattern in [
                r'Wifi is connected to "([^"]+)"',
                r'WifiInfo:.*?SSID:\s*"([^"]+)"',
                r'mWifiInfo.*?SSID:\s*"([^"]+)"',
                r'\bSSID:\s*"([^"]+)"',
                r'\bSSID:\s*([^\s,\n]+)',
            ]:
                m = _re.search(pattern, text)
                if m:
                    val = m.group(1).strip().strip('"')
                    if val and val not in ("<unknown ssid>", "0x", ""):
                        return val
            return ""

        # 에뮬레이터 감지 (BlueStacks / AOSP emulator / Genymotion)
        manufacturer = _adb_shell(did, ["shell", "getprop", "ro.product.manufacturer"], timeout=4).strip().lower()
        hardware = _adb_shell(did, ["shell", "getprop", "ro.hardware"], timeout=4).strip().lower()
        is_emulator = any(k in manufacturer for k in ("bluestack", "genymotion")) or \
                      any(k in hardware for k in ("ranchu", "goldfish", "vbox"))

        if is_emulator:
            # 에뮬레이터는 가상 WiFi → 호스트 머신 네트워크 연결만 확인
            try:
                if sys.platform == "darwin":
                    r = subprocess.run(["networksetup", "-getairportnetwork", "en0"],
                                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
                    host_ssid_match = _re.search(r'Current Wi-Fi Network:\s*(.+)', r.stdout)
                    host_ssid = host_ssid_match.group(1).strip() if host_ssid_match else ""
                    if host_ssid:
                        checks.append({"name": "WiFi 연결", "status": "ok",
                                       "detail": f"에뮬레이터 — 호스트 WiFi: {host_ssid}"})
                    else:
                        checks.append({"name": "WiFi 연결", "status": "warn",
                                       "detail": "에뮬레이터 — 호스트 WiFi 미연결"})
                else:
                    checks.append({"name": "WiFi 연결", "status": "warn",
                                   "detail": "에뮬레이터 — 호스트 네트워크 상태 직접 확인 필요"})
            except Exception:
                checks.append({"name": "WiFi 연결", "status": "warn",
                               "detail": "에뮬레이터 — 호스트 네트워크 확인 불가"})
        else:
            # 실기기: SSID 있으면 WiFi, 없으면 유선
            ssid = ""
            wifi_out = _adb_shell(did, ["shell", "cmd", "wifi", "status"], timeout=6)
            ssid = _extract_ssid(wifi_out)
            if not ssid:
                wifi_out2 = _adb_shell(did, ["shell", "dumpsys", "wifi"], timeout=8)
                ssid = _extract_ssid(wifi_out2)

            wifi_radio_out = _adb_shell(did, ["shell", "settings", "get", "global", "wifi_on"], timeout=4)
            wifi_radio_on = wifi_radio_out.strip() == "1"

            if ssid:
                checks.append({"name": "WiFi 연결", "status": "ok", "detail": f"연결됨: {ssid}"})
            elif wifi_radio_on:
                checks.append({"name": "WiFi 연결", "status": "warn", "detail": "WiFi 켜져 있으나 미연결"})
            else:
                route_out = _adb_shell(did, ["shell", "ip", "route", "show", "default"], timeout=4)
                iface_match = _re.search(r'dev\s+(\S+)', route_out)
                iface = iface_match.group(1) if iface_match else ""
                if iface:
                    checks.append({"name": "WiFi 연결", "status": "warn",
                                   "detail": f"유선 네트워크 연결됨 ({iface}) — WiFi 꺼짐"})
                else:
                    checks.append({"name": "WiFi 연결", "status": "fail",
                                   "detail": "WiFi 꺼짐 — 테스트 전 WiFi를 연결해주세요"})
    except Exception as e:
        checks.append({"name": "WiFi 연결", "status": "warn", "detail": f"확인 실패: {e}"})

    # 4. 화면 잠금 해제 여부
    try:
        out = _adb_shell(did, ["shell", "dumpsys", "power"])
        awake = "mWakefulness=Awake" in out or "Display Power: state=ON" in out
        out_win = _adb_shell(did, ["shell", "dumpsys", "window", "policy"])
        locked = "isStatusBarKeyguard=true" in out_win or "mKeyguardShowing=true" in out_win.lower()
        if awake and not locked:
            checks.append({"name": "화면 잠금 해제", "status": "ok", "detail": "화면 켜짐 / 잠금 해제됨"})
        elif not awake:
            checks.append({"name": "화면 잠금 해제", "status": "warn", "detail": "화면 꺼짐 — 테스트 전 화면을 켜주세요"})
        else:
            checks.append({"name": "화면 잠금 해제", "status": "warn", "detail": "잠금 화면 상태 — 잠금 해제 필요"})
    except Exception as e:
        checks.append({"name": "화면 잠금 해제", "status": "warn", "detail": f"확인 실패: {e}"})

    return {"checks": checks}


# ─────────────────────────────────────────────
# 화면 미리보기 API
# ─────────────────────────────────────────────
from fastapi.responses import Response as _Response
from PIL import Image as _Image
import io as _io

def _png_to_jpeg(png_bytes: bytes, quality: int = 60) -> bytes:
    """PNG → JPEG 변환 (용량 최소화)"""
    try:
        img = _Image.open(_io.BytesIO(png_bytes)).convert("RGB")
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except Exception:
        return png_bytes


@app.get("/api/screen/latest")
def screen_latest(device: str = ""):
    """테스트 실행 중 가장 최근 스크린샷 반환 (추가 ADB 호출 없음).
    device 지정 시 해당 디바이스 태그가 포함된 파일만 검색."""
    try:
        if device.strip():
            tag = "".join(c if c.isalnum() or c in "._-" else "_" for c in device.strip())
            pattern = f"*{tag}*.png"
        else:
            pattern = "*.png"
        files = sorted(
            cfg.paths.screenshots_dir.glob(pattern),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if not files:
            raise HTTPException(status_code=404, detail="스크린샷 없음")
        jpeg = _png_to_jpeg(files[0].read_bytes())
        return _Response(content=jpeg, media_type="image/jpeg",
                        headers={"Cache-Control": "no-cache, no-store"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/screen/snapshot")
def screen_snapshot(device: str = ""):
    """온디맨드 ADB 스크린캡처 (유휴 상태 미러링용)"""
    try:
        device_info = get_device(device)
        if device_info["status"] != "connected":
            raise HTTPException(status_code=503, detail="디바이스 미연결")
        did = device_info["device_id"]
        result = subprocess.run(
            ["adb", "-s", did, "exec-out", "screencap", "-p"],
            capture_output=True, timeout=8,
        )
        if result.returncode != 0 or not result.stdout:
            raise HTTPException(status_code=500, detail="스크린캡처 실패")
        jpeg = _png_to_jpeg(result.stdout, quality=55)
        return _Response(content=jpeg, media_type="image/jpeg",
                        headers={"Cache-Control": "no-cache, no-store"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────
# 디버그 탭 스크린샷 API
# ─────────────────────────────────────────────
@app.get("/api/debug/taps")
def list_tap_debug(since: str = "", limit: int = 100):
    """find_and_tap 탭 검증 디버그 이미지 목록 (최근순).

    타임스탬프는 'YYYYMMDD_HHMMSS_mmm' 형식이라 문자열 비교로 정렬/필터가 가능하다.
    since 이후 기록만 반환하면 방금 실행한 세션의 스샷만 리포트에 표시할 수 있다.
    """
    import json as _json
    jsonl = cfg.paths.debug_dir / "find_and_tap_debug.jsonl"
    if not jsonl.exists():
        return {"taps": []}
    taps: list[dict] = []
    try:
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = _json.loads(line)
            except Exception:
                continue
            ts = rec.get("timestamp", "")
            if since and ts < since:
                continue
            name = Path(rec.get("debug_image", "")).name
            taps.append({
                "timestamp": ts,
                "target": rec.get("target"),
                "confidence": rec.get("confidence"),
                "verified": rec.get("verified"),
                "failure_reason": rec.get("failure_reason", ""),
                "image": f"/debug/taps/{name}" if name else "",
            })
    except Exception as e:
        return {"taps": [], "error": str(e)}
    taps = taps[-limit:]
    taps.reverse()
    return {"taps": taps}


@app.post("/api/sr-debugger/enter")
def enter_sr_debugger(req: SRDebuggerEnterRequest):
    try:
        adb = ADBController()
        controller = SRDebuggerController(adb=adb, config=cfg)
        return controller.enter(
            package=req.package,
            strategies=req.strategies,
            verify_target=req.verify_target,
            max_attempts=req.max_attempts,
        )
    except Exception as e:
        logger.exception("enter_sr_debugger failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/unity/tutorial-pass")
def unity_tutorial_pass(req: TutorialPassRequest):
    if not req.package.strip():
        raise HTTPException(status_code=400, detail="package가 필요합니다.")
    try:
        adb = ADBController()
        client = UnityAPIClient(
            adb_controller=adb,
            project=cfg.gemini.project,
            location=cfg.gemini.location,
            model=cfg.gemini.model,
            temperature=cfg.gemini.temperature,
        )
        success = client.skip_tutorial(req.package.strip())
        return {"success": success, "package": req.package.strip()}
    except Exception as e:
        logger.exception("unity_tutorial_pass failed")
        raise HTTPException(status_code=500, detail=str(e))


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
# Evaluation Platform API
# ─────────────────────────────────────────────
@app.get("/api/eval/cases")
def eval_list_cases():
    return {"cases": _eval_store.list_cases()}


@app.post("/api/eval/cases")
def eval_upsert_case(req: EvalCaseCreate):
    try:
        return {"case": _eval_store.upsert_case(req)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/eval/cases/import-csv")
def eval_import_csv(req: EvalCsvImportRequest):
    try:
        return _eval_store.import_csv(req.csv_text)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/eval/runs")
def eval_list_runs():
    return {"runs": _eval_store.list_runs()}


@app.post("/api/eval/runs")
def eval_run(req: EvalRunRequest):
    try:
        return {"run": run_eval_suite(_eval_store, req)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("eval_run failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/eval/runs/{run_id}")
def eval_get_run(run_id: str):
    try:
        return {"run": _eval_store.get_run(run_id)}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/eval/runs/{run_id}/report")
def eval_get_report(run_id: str, baseline_run_id: str = ""):
    try:
        run = _eval_store.get_run(run_id)
        baseline = _eval_store.get_run(baseline_run_id) if baseline_run_id else None
        return {"run_id": run_id, "report": build_report(run, baseline=baseline)}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/reports/csv")
def create_result_csv(req: ReportExportRequest):
    try:
        csv_path = build_test_result_csv(
            result=req.result,
            taps=req.taps,
            output_dir=cfg.paths.reports_dir,
        )
        return FileResponse(
            path=str(csv_path),
            media_type="text/csv; charset=utf-8",
            filename=csv_path.name,
        )
    except Exception as e:
        logger.exception("create_result_csv failed")
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────
# 테스트 실행 API
# ─────────────────────────────────────────────
# 어댑티브 QA는 현재 실행 안정성이 낮아 일시 비활성화한다.
# @app.post("/api/adaptive/test/run")
# def run_adaptive_test(req: AdaptiveRunRequest):
#     if not req.steps:
#         raise HTTPException(status_code=400, detail="steps가 필요합니다.")
#     if not req.package:
#         raise HTTPException(status_code=400, detail="package가 필요합니다.")
#     try:
#         return {"run": _adaptive_runner.run(req)}
#     except Exception as e:
#         logger.exception("run_adaptive_test failed")
#         raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/test/run")
async def run_test(req: RunTestRequest):
    session_id = req.session_id
    if session_id in _test_stop_events and not _test_stop_events[session_id].is_set():
        raise HTTPException(status_code=409, detail="테스트가 이미 실행 중입니다.")

    # 대상 디바이스 결정 + 실행 락 (같은 폰에 동시 테스트 방지)
    target_device = req.device.strip() or (get_device().get("device_id") or "")
    if not target_device:
        raise HTTPException(status_code=503, detail="연결된 디바이스가 없습니다.")
    holder = _acquire_device_lock(target_device, session_id)
    if holder:
        raise HTTPException(
            status_code=409,
            detail=f"디바이스 {target_device}는 다른 테스트가 사용 중입니다 (세션: {holder})",
        )

    try:
        return _start_test_run(req, target_device)
    except Exception:
        # 실행 스레드 시작 전에 실패하면 락이 새지 않도록 해제
        _release_device_lock(target_device, req.session_id)
        raise


def _start_test_run(req: RunTestRequest, target_device: str):
    session_id = req.session_id
    stop_event = threading.Event()
    _test_stop_events[session_id] = stop_event

    loop = asyncio.get_event_loop()
    q = _ws_queues.get(session_id)
    if q is None:
        q = asyncio.Queue()
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
                adb_rec = ADBController(target_device)
                _active_adb[session_id] = adb_rec
                # 병렬 세션 파일명 충돌 방지 — 세션 ID를 녹화 이름에 포함
                _ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                _sid = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(session_id))[:24]
                session_ts = f"{_ts}_{_sid}"
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
            orchestrator = QAOrchestrator(cfg, device_id=target_device)
            result = orchestrator.run_test(session_id, stop_event, testcase_override=testcase)
            summary = _test_result_summary(result)
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
            _release_device_lock(target_device, session_id)
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
# 파이프라인 API
# ─────────────────────────────────────────────
def _topo_sort(node_ids: list[str], edges: list[dict]) -> list[str]:
    """위상 정렬 — 연결된 순서대로 node id 반환"""
    in_deg = {nid: 0 for nid in node_ids}
    adj: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for e in edges:
        src, tgt = e.get("source", ""), e.get("target", "")
        if src in adj and tgt in in_deg:
            adj[src].append(tgt)
            in_deg[tgt] += 1
    queue = [nid for nid in node_ids if in_deg[nid] == 0]
    result: list[str] = []
    while queue:
        nid = queue.pop(0)
        result.append(nid)
        for nb in adj[nid]:
            in_deg[nb] -= 1
            if in_deg[nb] == 0:
                queue.append(nb)
    # 연결 안 된 고립 노드 뒤에 추가
    for nid in node_ids:
        if nid not in result:
            result.append(nid)
    return result


@app.get("/api/pipelines")
def list_pipelines():
    files = sorted(_pipelines_dir.glob("*.json"))
    return {"pipelines": [f.stem for f in files]}


@app.get("/api/pipelines/{name}")
def get_pipeline(name: str):
    import json as _json
    path = _pipelines_dir / f"{name}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Pipeline not found: {name}")
    with open(path, encoding="utf-8") as f:
        return _json.load(f)


@app.post("/api/pipelines")
def save_pipeline(req: SavePipelineRequest):
    import json as _json
    safe_name = req.name.strip().replace(" ", "_")
    if not safe_name:
        raise HTTPException(status_code=400, detail="이름을 입력해주세요.")
    path = _pipelines_dir / f"{safe_name}.json"
    with open(path, "w", encoding="utf-8") as f:
        _json.dump({"name": req.name, "steps": req.steps, "nodes": req.nodes, "edges": req.edges}, f, ensure_ascii=False, indent=2)
    return {"success": True, "filename": path.name}


@app.delete("/api/pipelines/{name}")
def delete_pipeline(name: str):
    path = _pipelines_dir / f"{name}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Pipeline not found: {name}")
    path.unlink()
    return {"success": True}


@app.post("/api/pipeline/run")
async def run_pipeline(req: RunPipelineRequest):
    session_id = req.session_id
    if session_id in _test_stop_events and not _test_stop_events[session_id].is_set():
        raise HTTPException(status_code=409, detail="파이프라인이 이미 실행 중입니다.")

    # 대상 디바이스 결정 + 실행 락 (같은 폰에 동시 테스트 방지)
    target_device = req.device.strip() or (get_device().get("device_id") or "")
    if not target_device:
        raise HTTPException(status_code=503, detail="연결된 디바이스가 없습니다.")
    holder = _acquire_device_lock(target_device, session_id)
    if holder:
        raise HTTPException(
            status_code=409,
            detail=f"디바이스 {target_device}는 다른 테스트가 사용 중입니다 (세션: {holder})",
        )

    try:
        return _start_pipeline_run(req, target_device)
    except Exception:
        # 실행 스레드 시작 전에 실패하면 락이 새지 않도록 해제
        _release_device_lock(target_device, req.session_id)
        raise


def _start_pipeline_run(req: RunPipelineRequest, target_device: str):
    session_id = req.session_id
    stop_event = threading.Event()
    _test_stop_events[session_id] = stop_event

    loop = asyncio.get_event_loop()
    q = _ws_queues.get(session_id)
    if q is None:
        q = asyncio.Queue()
        _ws_queues[session_id] = q

    # 플랫 스텝 리스트가 직접 제공된 경우 바로 사용
    if req.steps:
        all_steps = req.steps
        # launch_app / uninstall_app: target → params.package 정규화
        for s in all_steps:
            if s.get("action") in ("launch_app", "uninstall_app") and s.get("target"):
                s.setdefault("params", {})["package"] = s["target"]
        phase_labels: list[str] = []
    else:
        # 구형: 위상 정렬 후 템플릿 스텝 병합
        node_ids = [n["id"] for n in req.nodes]
        ordered_ids = _topo_sort(node_ids, req.edges)
        node_map = {n["id"]: n for n in req.nodes}

        all_steps = []
        phase_labels = []
        for nid in ordered_ids:
            node = node_map.get(nid)
            if not node:
                continue
            node_data = node.get("data") or {}
            node_type = node_data.get("node_type", "template")

            if node_type == "step":
                # 단일 스텝 노드
                action = node_data.get("action") or ""
                if not action:
                    continue
                step: dict = {
                    "action": action,
                    "target": node_data.get("target") or None,
                    "description": node_data.get("description") or node_data.get("label") or "",
                }
                if action in ("launch_app", "uninstall_app") and step.get("target"):
                    step.setdefault("params", {})["package"] = step["target"]
                if action == "wait" and node_data.get("seconds") is not None:
                    step.setdefault("params", {})["seconds"] = node_data["seconds"]
                all_steps.append(step)
                phase_labels.append(f"[{action}]")
            else:
                # 템플릿 노드
                label = node_data.get("label") or node.get("template", "")
                if not label:
                    continue
                steps = node_data.get("steps") or []
                if not steps:
                    tpl_path = cfg.paths.templates_dir / f"{label}.yaml"
                    if not tpl_path.exists():
                        logger.warning("Pipeline: template not found: %s", label)
                        continue
                    with open(tpl_path, encoding="utf-8") as f:
                        tmpl = yaml.safe_load(f)
                    steps = tmpl.get("steps", [])
                for s in steps:
                    if s.get("action") in ("launch_app", "uninstall_app") and s.get("target"):
                        s.setdefault("params", {})["package"] = s["target"]
                all_steps.extend(steps)
                phase_labels.append(label)

    if not all_steps:
        raise HTTPException(status_code=400, detail="실행할 스텝이 없습니다. 스텝을 추가해주세요.")

    pkg = next((s.get("target") for s in all_steps if s.get("action") == "launch_app" and s.get("target")), "")
    testcase = TestCase(**{
        "id": session_id,
        "title": " → ".join(phase_labels) if phase_labels else "파이프라인 실행",
        "description": "",
        "package": pkg,
        "steps": all_steps,
        "expected_results": [],
        "preconditions": [],
    })

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
                adb_rec = ADBController(target_device)
                _active_adb[session_id] = adb_rec
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                _sid = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(session_id))[:24]
                adb_rec.start_recording(cfg.paths.recordings_dir, f"{ts}_{_sid}")
                asyncio.run_coroutine_threadsafe(q.put({"type": "log", "message": f"🔴 녹화 시작"}), loop)
            except Exception as e:
                asyncio.run_coroutine_threadsafe(q.put({"type": "log", "message": f"⚠️ 녹화 시작 실패: {e}"}), loop)
                adb_rec = None
        try:
            asyncio.run_coroutine_threadsafe(
                q.put({"type": "log", "message": f"━ 파이프라인: {' → '.join(phase_labels) if phase_labels else testcase.title}"}), loop
            )
            orchestrator = QAOrchestrator(cfg, device_id=target_device)
            result = orchestrator.run_test(session_id, stop_event, testcase_override=testcase)
            summary = _test_result_summary(result)
            asyncio.run_coroutine_threadsafe(q.put({"type": "result", "data": summary}), loop)
        except Exception as e:
            logger.exception("run_pipeline thread failed")
            asyncio.run_coroutine_threadsafe(q.put({"type": "error", "message": str(e)}), loop)
        finally:
            if adb_rec and adb_rec.is_recording:
                try:
                    files = adb_rec.stop_recording()
                    asyncio.run_coroutine_threadsafe(
                        q.put({"type": "log", "message": f"⏹️ 녹화 완료 — {len(files or [])}개 청크"}), loop
                    )
                except Exception:
                    pass
            _release_device_lock(target_device, session_id)
            _active_adb.pop(session_id, None)
            _test_stop_events.pop(session_id, None)
            root_logger.removeHandler(handler)
            asyncio.run_coroutine_threadsafe(q.put({"type": "done"}), loop)

    threading.Thread(target=_run_thread, daemon=True).start()
    return {"session_id": session_id, "status": "started"}


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
    q = _ws_queues.get(session_id)
    if q is None:
        q = asyncio.Queue()
        _ws_queues[session_id] = q
        await websocket.send_json({"type": "waiting", "message": "세션 시작을 기다리는 중입니다."})

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
# ─────────────────────────────────────────────
# 헬스체크 (Docker healthcheck용 — SPA 캐치올보다 먼저 등록되어야 함)
# ─────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "server": "qa-auto-api", "time": datetime.now().isoformat()}


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
