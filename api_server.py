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
_pipelines_dir = cfg.paths.project_root / "pipelines"
_pipelines_dir.mkdir(parents=True, exist_ok=True)

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

class StopTestRequest(BaseModel):
    session_id: str

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
        result = subprocess.run(["adb", "connect", address], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
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
        result = subprocess.run(["adb", "disconnect", address], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
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

    # 2. 무선 연결 (ADB_DEVICE가 IP:PORT 형식이면)
    preferred = os.getenv("ADB_DEVICE", "").strip()
    if preferred and ":" in preferred:
        try:
            r = subprocess.run(["adb", "connect", preferred], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
            out = r.stdout.strip()
            success = "connected" in out.lower() or "already connected" in out.lower()
            log.append(f"{'✅' if success else '⚠️'} adb connect {preferred}: {out}")
        except Exception as e:
            log.append(f"❌ adb connect {preferred} 실패: {e}")
    else:
        log.append(f"ℹ️ USB 연결 모드 (ADB_DEVICE={preferred or '미설정'})")

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
        adb = ADBController()
        ok, msg = adb.install_apk(apk_path)
        return {"success": ok, "message": msg}
    except Exception as e:
        return {"success": False, "message": str(e)}

# ─────────────────────────────────────────────
# 패키지 API
# ─────────────────────────────────────────────
@app.get("/api/packages")
def list_packages():
    # APK 파일명에서 키워드 추출 (확장자 제거, 소문자)
    apk_keywords = [
        f.stem.lower()
        for f in apk_directory.glob("*.apk")
    ]
    try:
        device_info = get_device()
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
def preflight_check():
    checks = []

    # 1. ADB 연결
    device_info = get_device()
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
def screen_latest():
    """테스트 실행 중 가장 최근 스크린샷 반환 (추가 ADB 호출 없음)"""
    try:
        files = sorted(
            cfg.paths.screenshots_dir.glob("*.png"),
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
def screen_snapshot():
    """온디맨드 ADB 스크린캡처 (유휴 상태 미러링용)"""
    try:
        device_info = get_device()
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

    stop_event = threading.Event()
    _test_stop_events[session_id] = stop_event

    loop = asyncio.get_event_loop()
    q: asyncio.Queue = asyncio.Queue()
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
                adb_rec = ADBController()
                _active_adb[session_id] = adb_rec
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                adb_rec.start_recording(cfg.paths.recordings_dir, ts)
                asyncio.run_coroutine_threadsafe(q.put({"type": "log", "message": f"🔴 녹화 시작"}), loop)
            except Exception as e:
                asyncio.run_coroutine_threadsafe(q.put({"type": "log", "message": f"⚠️ 녹화 시작 실패: {e}"}), loop)
                adb_rec = None
        try:
            asyncio.run_coroutine_threadsafe(
                q.put({"type": "log", "message": f"━ 파이프라인: {' → '.join(phase_labels) if phase_labels else testcase.title}"}), loop
            )
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
