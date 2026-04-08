"""qa-auto Agent — 각 PC에서 실행되는 로컬 FastAPI 서버"""
import asyncio
import logging
import os
import queue
import socket
import subprocess
import sys
import threading
import yaml
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

_exe_dir = Path(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__)).parent
load_dotenv(_exe_dir / ".env")
load_dotenv()

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response as _Response
from pydantic import BaseModel
from PIL import Image as _Image
import io as _io
import httpx

from config import Config
from adb_controller import ADBController
from qa_orchestrator import QAOrchestrator
from test_manager import TestCase

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
cfg = Config()

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "").rstrip("/")
AGENT_NAME = os.getenv("AGENT_NAME", socket.gethostname())
AGENT_PORT = int(os.getenv("AGENT_PORT", "8000"))

apk_directory = cfg.paths.apks_dir

app = FastAPI(title="qa-auto Agent", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

STEP_MARKERS = ("━", "▶ ", "┌─", "│", "└─", "⏹️", "🔴", "  결과:", "  테스트 시작:", "  패키지:")

_ws_queues: dict[str, asyncio.Queue] = {}
_test_stop_events: dict[str, threading.Event] = {}
_active_adb: dict[str, ADBController] = {}

INSTALLED_PACKAGES = [
    "com.percent.aos.cooptd",
    "com.percent.aos.rollinghero",
    "com.supermagic.aos.statusman",
    "com.percent.aos.luckydefense",
    "com.percent.aos.arenago2",
]


# ─────────────────────────────────────────────
# Pydantic 모델
# ─────────────────────────────────────────────
class InstallApkRequest(BaseModel):
    filename: str

class UninstallRequest(BaseModel):
    package: str

class RunTestRequest(BaseModel):
    title: str
    package: str
    steps: list[dict]
    session_id: str
    record: bool = False

class StopTestRequest(BaseModel):
    session_id: str

class RunPipelineRequest(BaseModel):
    steps: list[dict] = []
    nodes: list[dict] = []
    edges: list[dict] = []
    session_id: str
    record: bool = False


# ─────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────
def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def _adb_shell(device_id: str, args: list[str], timeout: int = 8) -> str:
    try:
        result = subprocess.run(
            ["adb", "-s", device_id] + args,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        return result.stdout + result.stderr
    except Exception:
        return ""

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

def _png_to_jpeg(png_bytes: bytes, quality: int = 60) -> bytes:
    try:
        img = _Image.open(_io.BytesIO(png_bytes)).convert("RGB")
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except Exception:
        return png_bytes


# ─────────────────────────────────────────────
# 오케스트레이터 등록
# ─────────────────────────────────────────────
def _register_to_orchestrator():
    if not ORCHESTRATOR_URL:
        logger.info("[Agent] ORCHESTRATOR_URL 미설정 — 단독 실행 모드")
        return
    local_ip = _get_local_ip()
    payload = {
        "name": AGENT_NAME,
        "ip": local_ip,
        "port": AGENT_PORT,
    }
    try:
        r = httpx.post(f"{ORCHESTRATOR_URL}/api/agents/register", json=payload, timeout=5)
        r.raise_for_status()
        logger.info(f"[Agent] 오케스트레이터 등록 완료: {ORCHESTRATOR_URL} ({local_ip}:{AGENT_PORT})")
    except Exception as e:
        logger.warning(f"[Agent] 오케스트레이터 등록 실패 (단독 실행 모드): {e}")

def _heartbeat_loop():
    """30초마다 오케스트레이터에 heartbeat 전송"""
    if not ORCHESTRATOR_URL:
        return
    import time
    local_ip = _get_local_ip()
    while True:
        time.sleep(30)
        try:
            httpx.post(f"{ORCHESTRATOR_URL}/api/agents/heartbeat",
                       json={"name": AGENT_NAME, "ip": local_ip, "port": AGENT_PORT},
                       timeout=5)
        except Exception:
            pass


# ─────────────────────────────────────────────
# 디바이스 API
# ─────────────────────────────────────────────
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
    try:
        devices = [d for d in _get_all_devices() if d["status"] == "device"]
        return {"devices": devices}
    except Exception as e:
        return {"devices": [], "error": str(e)}

@app.post("/api/devices/connect")
def connect_device(body: dict):
    address = body.get("address", "").strip()
    if not address:
        raise HTTPException(status_code=400, detail="address가 필요합니다.")
    try:
        result = subprocess.run(["adb", "connect", address], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        output = result.stdout.strip()
        success = "connected" in output.lower() or "already connected" in output.lower()
        return {"success": success, "message": output}
    except Exception as e:
        return {"success": False, "message": str(e)}

@app.post("/api/devices/disconnect")
def disconnect_device(body: dict):
    address = body.get("address", "").strip()
    if not address:
        raise HTTPException(status_code=400, detail="address가 필요합니다.")
    try:
        result = subprocess.run(["adb", "disconnect", address], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        return {"success": True, "message": result.stdout.strip()}
    except Exception as e:
        return {"success": False, "message": str(e)}

@app.post("/api/device/reconnect")
def device_reconnect():
    import time as _time
    log: list[str] = []
    try:
        r = subprocess.run(["adb", "start-server"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
        msg = r.stdout.strip() or r.stderr.strip() or "OK"
        log.append(f"✅ adb start-server: {msg}")
    except Exception as e:
        log.append(f"❌ adb start-server 실패: {e}")

    preferred = os.getenv("ADB_DEVICE", "").strip()
    if preferred and ":" in preferred:
        try:
            r = subprocess.run(["adb", "connect", preferred], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
            out = r.stdout.strip()
            success = "connected" in out.lower() or "already connected" in out.lower()
            log.append(f"{'✅' if success else '⚠️'} adb connect {preferred}: {out}")
        except Exception as e:
            log.append(f"❌ adb connect {preferred} 실패: {e}")

    _time.sleep(2)
    device = get_device()
    connected = device["status"] == "connected"
    log.append(f"✅ 연결 성공: {device.get('model')} ({device.get('device_id')})" if connected else "❌ 연결 실패")
    return {"success": connected, "device": device, "log": log}


# ─────────────────────────────────────────────
# APK / 패키지 API
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
    apk_path = apk_directory / req.filename
    if not apk_path.exists():
        raise HTTPException(status_code=404, detail=f"APK not found: {apk_path}")
    try:
        adb = ADBController()
        _, msg = adb.install_apk(str(apk_path))
        return {"success": True, "message": msg}
    except Exception as e:
        return {"success": False, "message": str(e)}

@app.get("/api/packages")
def list_packages():
    apk_keywords = [f.stem.lower() for f in apk_directory.glob("*.apk")]
    try:
        device_info = get_device()
        if device_info["status"] == "connected":
            did = device_info["device_id"]
            out = _adb_shell(did, ["shell", "pm", "list", "packages"])
            all_pkgs = [line.replace("package:", "").strip() for line in out.splitlines() if line.startswith("package:")]
            if apk_keywords:
                filtered = sorted(p for p in all_pkgs if any(kw in p.lower() for kw in apk_keywords))
                if filtered:
                    return {"packages": filtered}
            elif all_pkgs:
                return {"packages": sorted(all_pkgs)}
    except Exception:
        pass
    if apk_keywords:
        return {"packages": [p for p in INSTALLED_PACKAGES if any(kw in p.lower() for kw in apk_keywords)]}
    return {"packages": INSTALLED_PACKAGES}

@app.get("/api/package-apk-map")
def get_package_apk_map():
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
# 사전 점검 API
# ─────────────────────────────────────────────
@app.get("/api/preflight")
def preflight_check():
    import re as _re
    checks = []

    device_info = get_device()
    connected = device_info["status"] == "connected"
    checks.append({
        "name": "ADB 연결",
        "status": "ok" if connected else "fail",
        "detail": f"{device_info.get('model')} ({device_info.get('device_id')})" if connected else "디바이스 없음",
    })

    if not connected:
        for name in ["Google 계정", "WiFi 연결", "화면 잠금 해제"]:
            checks.append({"name": name, "status": "unknown", "detail": "ADB 연결 필요"})
        return {"checks": checks}

    did = device_info["device_id"]

    # Google 계정
    try:
        out = _adb_shell(did, ["shell", "dumpsys", "account"])
        emails = []
        for ln in out.splitlines():
            if "Account {" in ln and "google" in ln.lower():
                m = _re.search(r'name=([^\s,}]+)', ln)
                if m:
                    emails.append(m.group(1))
        checks.append({
            "name": "Google 계정",
            "status": "ok" if emails else "warn",
            "detail": ", ".join(emails) + f" ({len(emails)}개)" if emails else "Google 계정 없음",
        })
    except Exception as e:
        checks.append({"name": "Google 계정", "status": "warn", "detail": f"확인 실패: {e}"})

    # WiFi 연결
    try:
        def _extract_ssid(text: str) -> str:
            for pattern in [
                r'Wifi is connected to "([^"]+)"',
                r'WifiInfo:.*?SSID:\s*"([^"]+)"',
                r'\bSSID:\s*"([^"]+)"',
                r'\bSSID:\s*([^\s,\n]+)',
            ]:
                m = _re.search(pattern, text)
                if m:
                    val = m.group(1).strip().strip('"')
                    if val and val not in ("<unknown ssid>", "0x", ""):
                        return val
            return ""

        manufacturer = _adb_shell(did, ["shell", "getprop", "ro.product.manufacturer"], timeout=4).strip().lower()
        hardware = _adb_shell(did, ["shell", "getprop", "ro.hardware"], timeout=4).strip().lower()
        is_emulator = any(k in manufacturer for k in ("bluestack", "genymotion")) or \
                      any(k in hardware for k in ("ranchu", "goldfish", "vbox"))

        if is_emulator:
            try:
                if sys.platform == "darwin":
                    r = subprocess.run(["networksetup", "-getairportnetwork", "en0"],
                                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
                    m = _re.search(r'Current Wi-Fi Network:\s*(.+)', r.stdout)
                    ssid = m.group(1).strip() if m else ""
                    checks.append({"name": "WiFi 연결",
                                   "status": "ok" if ssid else "warn",
                                   "detail": f"에뮬레이터 — 호스트 WiFi: {ssid}" if ssid else "에뮬레이터 — 호스트 WiFi 미연결"})
                elif sys.platform == "win32":
                    r = subprocess.run(["netsh", "wlan", "show", "interfaces"],
                                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
                    m = _re.search(r'SSID\s*:\s(?!BSSID)(.+)', r.stdout)
                    ssid = m.group(1).strip() if m else ""
                    if ssid:
                        checks.append({"name": "WiFi 연결", "status": "ok", "detail": f"에뮬레이터 — 호스트 WiFi: {ssid}"})
                    else:
                        r2 = subprocess.run(["ping", "-n", "1", "-w", "1000", "8.8.8.8"], capture_output=True, timeout=5)
                        checks.append({"name": "WiFi 연결",
                                       "status": "ok" if r2.returncode == 0 else "warn",
                                       "detail": "에뮬레이터 — 호스트 네트워크 연결됨" if r2.returncode == 0 else "에뮬레이터 — 호스트 WiFi 미연결"})
                else:
                    checks.append({"name": "WiFi 연결", "status": "warn", "detail": "에뮬레이터 — 호스트 네트워크 상태 직접 확인 필요"})
            except Exception:
                checks.append({"name": "WiFi 연결", "status": "warn", "detail": "에뮬레이터 — 호스트 네트워크 확인 불가"})
        else:
            ssid = _extract_ssid(_adb_shell(did, ["shell", "cmd", "wifi", "status"], timeout=6))
            if not ssid:
                ssid = _extract_ssid(_adb_shell(did, ["shell", "dumpsys", "wifi"], timeout=8))
            wifi_on = _adb_shell(did, ["shell", "settings", "get", "global", "wifi_on"], timeout=4).strip() == "1"
            if ssid:
                checks.append({"name": "WiFi 연결", "status": "ok", "detail": f"연결됨: {ssid}"})
            elif wifi_on:
                checks.append({"name": "WiFi 연결", "status": "warn", "detail": "WiFi 켜져 있으나 미연결"})
            else:
                checks.append({"name": "WiFi 연결", "status": "fail", "detail": "WiFi 꺼짐 — 테스트 전 WiFi를 연결해주세요"})
    except Exception as e:
        checks.append({"name": "WiFi 연결", "status": "warn", "detail": f"확인 실패: {e}"})

    # 화면 잠금
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
@app.get("/api/screen/latest")
def screen_latest():
    try:
        files = sorted(cfg.paths.screenshots_dir.glob("*.png"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not files:
            raise HTTPException(status_code=404, detail="스크린샷 없음")
        jpeg = _png_to_jpeg(files[0].read_bytes())
        return _Response(content=jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-cache, no-store"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/screen/snapshot")
def screen_snapshot():
    try:
        device_info = get_device()
        if device_info["status"] != "connected":
            raise HTTPException(status_code=503, detail="디바이스 미연결")
        did = device_info["device_id"]
        result = subprocess.run(["adb", "-s", did, "exec-out", "screencap", "-p"], capture_output=True, timeout=8)
        if result.returncode != 0 or not result.stdout:
            raise HTTPException(status_code=500, detail="스크린캡처 실패")
        jpeg = _png_to_jpeg(result.stdout, quality=55)
        return _Response(content=jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-cache, no-store"})
    except HTTPException:
        raise
    except Exception as e:
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
    testcase = TestCase(**{
        "id": session_id, "title": req.title or "테스트 실행", "description": "",
        "package": pkg, "steps": steps, "expected_results": [], "preconditions": [],
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
                adb_rec = ADBController()
                _active_adb[session_id] = adb_rec
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                adb_rec.start_recording(cfg.paths.recordings_dir, ts)
                asyncio.run_coroutine_threadsafe(q.put({"type": "log", "message": f"🔴 화면 녹화 시작 — {ts}"}), loop)
            except Exception as e:
                asyncio.run_coroutine_threadsafe(q.put({"type": "log", "message": f"⚠️ 녹화 시작 실패: {e}"}), loop)
                adb_rec = None
        try:
            orchestrator = QAOrchestrator(cfg)
            result = orchestrator.run_test(session_id, stop_event, testcase_override=testcase)
            summary = {
                "status": result.status, "title": result.title,
                "steps_passed": result.steps_passed, "steps_executed": result.steps_executed,
                "error_message": result.error_message, "step_results": result.step_results or [],
            }
            asyncio.run_coroutine_threadsafe(q.put({"type": "result", "data": summary}), loop)
        except Exception as e:
            logger.exception("run_test thread failed")
            asyncio.run_coroutine_threadsafe(q.put({"type": "error", "message": str(e)}), loop)
        finally:
            if adb_rec and adb_rec.is_recording:
                try:
                    files = adb_rec.stop_recording()
                    asyncio.run_coroutine_threadsafe(q.put({"type": "log", "message": f"⏹️ 녹화 완료 — {len(files or [])}개 청크"}), loop)
                except Exception:
                    pass
            _active_adb.pop(session_id, None)
            _test_stop_events.pop(session_id, None)
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
# 파이프라인 실행 API
# ─────────────────────────────────────────────
def _topo_sort(node_ids: list[str], edges: list[dict]) -> list[str]:
    in_deg = {nid: 0 for nid in node_ids}
    adj: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for e in edges:
        src, tgt = e.get("source", ""), e.get("target", "")
        if src in adj and tgt in in_deg:
            adj[src].append(tgt)
            in_deg[tgt] += 1
    q = [nid for nid in node_ids if in_deg[nid] == 0]
    result: list[str] = []
    while q:
        nid = q.pop(0)
        result.append(nid)
        for nb in adj[nid]:
            in_deg[nb] -= 1
            if in_deg[nb] == 0:
                q.append(nb)
    for nid in node_ids:
        if nid not in result:
            result.append(nid)
    return result


@app.post("/api/pipeline/run")
async def run_pipeline(req: RunPipelineRequest):
    session_id = req.session_id
    if session_id in _test_stop_events and not _test_stop_events[session_id].is_set():
        raise HTTPException(status_code=409, detail="파이프라인이 이미 실행 중입니다.")

    stop_event = threading.Event()
    _test_stop_events[session_id] = stop_event
    loop = asyncio.get_event_loop()
    q: asyncio.Queue = asyncio.Queue()
    _ws_queues[session_id] = q

    if req.steps:
        all_steps = req.steps
        for s in all_steps:
            if s.get("action") in ("launch_app", "uninstall_app") and s.get("target"):
                s.setdefault("params", {})["package"] = s["target"]
        phase_labels: list[str] = []
    else:
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
                action = node_data.get("action") or ""
                if not action:
                    continue
                step: dict = {"action": action, "target": node_data.get("target"), "description": node_data.get("description") or ""}
                if action in ("launch_app", "uninstall_app") and step.get("target"):
                    step.setdefault("params", {})["package"] = step["target"]
                if action == "wait" and node_data.get("seconds") is not None:
                    step.setdefault("params", {})["seconds"] = node_data["seconds"]
                all_steps.append(step)
                phase_labels.append(f"[{action}]")
            else:
                label = node_data.get("label") or node.get("template", "")
                if not label:
                    continue
                steps = node_data.get("steps") or []
                if not steps:
                    tpl_path = cfg.paths.templates_dir / f"{label}.yaml"
                    if not tpl_path.exists():
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
        raise HTTPException(status_code=400, detail="실행할 스텝이 없습니다.")

    pkg = next((s.get("target") for s in all_steps if s.get("action") == "launch_app" and s.get("target")), "")
    testcase = TestCase(**{
        "id": session_id,
        "title": " → ".join(phase_labels) if phase_labels else "파이프라인 실행",
        "description": "", "package": pkg, "steps": all_steps,
        "expected_results": [], "preconditions": [],
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
                adb_rec = ADBController()
                _active_adb[session_id] = adb_rec
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                adb_rec.start_recording(cfg.paths.recordings_dir, ts)
                asyncio.run_coroutine_threadsafe(q.put({"type": "log", "message": "🔴 녹화 시작"}), loop)
            except Exception as e:
                asyncio.run_coroutine_threadsafe(q.put({"type": "log", "message": f"⚠️ 녹화 시작 실패: {e}"}), loop)
                adb_rec = None
        try:
            asyncio.run_coroutine_threadsafe(
                q.put({"type": "log", "message": f"━ 파이프라인: {' → '.join(phase_labels) if phase_labels else testcase.title}"}), loop)
            orchestrator = QAOrchestrator(cfg)
            result = orchestrator.run_test(session_id, stop_event, testcase_override=testcase)
            summary = {
                "status": result.status, "title": result.title,
                "steps_passed": result.steps_passed, "steps_executed": result.steps_executed,
                "error_message": result.error_message, "step_results": result.step_results or [],
            }
            asyncio.run_coroutine_threadsafe(q.put({"type": "result", "data": summary}), loop)
        except Exception as e:
            logger.exception("run_pipeline thread failed")
            asyncio.run_coroutine_threadsafe(q.put({"type": "error", "message": str(e)}), loop)
        finally:
            if adb_rec and adb_rec.is_recording:
                try:
                    files = adb_rec.stop_recording()
                    asyncio.run_coroutine_threadsafe(q.put({"type": "log", "message": f"⏹️ 녹화 완료 — {len(files or [])}개 청크"}), loop)
                except Exception:
                    pass
            _active_adb.pop(session_id, None)
            _test_stop_events.pop(session_id, None)
            root_logger.removeHandler(handler)
            asyncio.run_coroutine_threadsafe(q.put({"type": "done"}), loop)

    threading.Thread(target=_run_thread, daemon=True).start()
    return {"session_id": session_id, "status": "started"}


# ─────────────────────────────────────────────
# 녹화 API
# ─────────────────────────────────────────────
@app.get("/api/recordings")
def list_recordings():
    try:
        files = sorted(cfg.paths.recordings_dir.glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)
        return {"recordings": [f.name for f in files]}
    except Exception as e:
        return {"recordings": [], "error": str(e)}


# ─────────────────────────────────────────────
# WebSocket — 실시간 로그 스트리밍
# ─────────────────────────────────────────────
@app.websocket("/ws/logs/{session_id}")
async def ws_logs(websocket: WebSocket, session_id: str):
    await websocket.accept()
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


# ─────────────────────────────────────────────
# 헬스체크
# ─────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "agent": AGENT_NAME}


# 단독 실행 모드 — 자기 자신을 에이전트로 반환
@app.get("/api/agents")
def list_agents_self():
    return {
        "agents": [{"name": AGENT_NAME, "ip": _get_local_ip(), "port": AGENT_PORT, "online": True}]
    }


# ─────────────────────────────────────────────
# React SPA 서빙 (단독 실행 모드)
# ─────────────────────────────────────────────
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

_dist = cfg.paths.bundle_root / "frontend" / "dist"

if _dist.exists():
    app.mount("/assets", StaticFiles(directory=str(_dist / "assets")), name="assets")
    app.mount("/recordings", StaticFiles(directory=str(cfg.paths.recordings_dir)), name="recordings")

    @app.get("/")
    async def serve_root():
        return FileResponse(str(_dist / "index.html"))

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        candidate = _dist / full_path
        if candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(_dist / "index.html"))


# 단독 실행 모드 호환 — 오케스트레이터 없을 때 자기 자신을 에이전트로 반환
@app.get("/api/agents")
def list_agents_self():
    local_ip = _get_local_ip()
    return {
        "agents": [{"name": AGENT_NAME, "ip": local_ip, "port": AGENT_PORT, "online": True}]
    }


# ─────────────────────────────────────────────
# React SPA 서빙 (오케스트레이터 없이 단독 실행 시)
# ─────────────────────────────────────────────
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

_dist = cfg.paths.bundle_root / "frontend" / "dist"

if _dist.exists():
    app.mount("/assets", StaticFiles(directory=str(_dist / "assets")), name="assets")
    app.mount("/recordings", StaticFiles(directory=str(cfg.paths.recordings_dir)), name="recordings")

    @app.get("/")
    async def serve_root():
        return FileResponse(str(_dist / "index.html"))

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        candidate = _dist / full_path
        if candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(_dist / "index.html"))


# ─────────────────────────────────────────────
# 엔트리포인트
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    # 오케스트레이터 등록
    _register_to_orchestrator()
    threading.Thread(target=_heartbeat_loop, daemon=True).start()

    uvicorn.run(app, host="0.0.0.0", port=AGENT_PORT, reload=False)
