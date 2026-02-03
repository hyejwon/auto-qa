from fastmcp import FastMCP
import subprocess
import json
import base64
from typing import Any, Dict, List, Optional
import xml.etree.ElementTree as ET
import re
from datetime import datetime
from pathlib import Path
import logging
import base64
from google.genai import types
from google import genai
from pydantic import BaseModel, Field
import asyncio
import io
from PIL import Image, ImageDraw
import os
from typing import Any
import httpx

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:37772")

client_gemini = genai.Client(vertexai=True, project="percent-vertex-test", location="global")

# 로깅 설정 (버퍼링 최소화로 중단 시에도 로그 보존)
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

# 파일 핸들러 (버퍼링 없이 즉시 쓰기)
file_handler = logging.FileHandler('mcp_server.log', encoding='utf-8', mode='a')
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.INFO)

# 콘솔 핸들러 (stderr로 출력하여 stdout과 분리)
import sys
console_handler = logging.StreamHandler(sys.stderr)  # stderr로 출력
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.INFO)

# 로거 설정
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# 루트 로거로 전파하지 않도록 설정 (중복 출력 방지)
logger.propagate = False

# FastMCP 서버 생성
mcp = FastMCP("mobile-mcp-server")

# 현재 연결된 디바이스 (전역 변수)
def _get_first_connected_device() -> str | None:
    """ADB로 연결된 첫 번째 디바이스 ID 반환"""
    try:
        result = subprocess.run(
            ["adb", "devices"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.split("\n")[1:]:
            if "\t" in line:
                device_id = line.split("\t")[0]
                if device_id:
                    logger.info(f"Auto-detected device: {device_id}")
                    return device_id
    except Exception as e:
        logger.warning(f"Failed to auto-detect device: {e}")
    return None

current_device = _get_first_connected_device()

button_map = {
    "google": "KEYCODE_DPAD_CENTER",
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "menu": "KEYCODE_MENU",
    "power": "KEYCODE_POWER",
    "volume_up": "KEYCODE_VOLUME_UP",
    "volume_down": "KEYCODE_VOLUME_DOWN",
    "volume_mute": "KEYCODE_VOLUME_MUTE",
}


# ========= FastMCP 툴 정의 (Python 버전) =========
class VisionClickArgs(BaseModel):
    button_description: str = Field(
        ..., 
        description="이미지에서 클릭할 버튼에 대한 구체적인 설명 (예: '그린스톤 구매 버튼', '2700 실버라고 적힌 녹색 버튼')"
    )

# ========= 내부 구현 함수들 (직접 호출용) =========
import re
from difflib import SequenceMatcher
from typing import Optional, Tuple, List, Dict, Any

async def validate_action(action: str) -> bool:
    """
    주어진 action이 유효한지 llm 혹은 버튼 조회 으로 화면을 검증해서 검증 결과를 반환
    """
    
    smart_find_impl(action)
    
    
    return {"status": "success", "reason": "action is valid"}
    
async def llm_choose_unity_candidate(target: str, buttons: List[Dict[str, Any]]) -> Dict[str, Any] | None:
    """
    alias 하드코딩 없이 LLM이 후보 중 best 1개를 고르게 함.
    반환: {"index": int, "reason": str} or None
    """
    result = take_screenshot_impl()
    screenshot_image = result.get("image", "")
    # LLM 입력 크기 제한을 위해 상위 N개만 (너무 많으면 토큰 폭발)
    N = 60
    cand = []
    for i, b in enumerate(buttons[:N]):
        cand.append({
            "i": i,
            "name": (b.get("GameObjectName") or ""),
            "position": (b.get("PositionX"), b.get("PositionY")),
            "parent": (b.get("ParentMetadata") or "")[:220],  # parent 너무 길면 자르기
        })

    prompt = f"""
너는 모바일 QA 자동화에서 Unity UI 버튼을 고르는 랭커다.
사용자 target(사람 언어): "{target}"

아래 후보들 중 target과 의미적으로 가장 일치하는 버튼 1개를 고르고, 그 후보의 i를 반환해라.
- target은 한국어일 수 있고 후보 name은 영어 식별자일 수 있다. 예: "햄버거" == "HambergerButton"
- 이미지의 위치를 보고 주어진 target을 찾아라.
- ParentMetadata에 Top/Right/Menu/Setting 같은 컨텍스트가 있으면 그걸 근거로 삼아라.
- Frame 같은 일반 이름은 특별한 근거가 없으면 피하라.

반환은 반드시 JSON만:
{{"index": <int>, "reason": "<짧게>"}}

후보 목록:
{json.dumps(cand, ensure_ascii=False)}
""".strip()

    try:
        # 너 코드에 이미 client_gemini 있으니 그대로 사용
        resp = client_gemini.models.generate_content(
            model="gemini-2.5-flash",
            contents=[prompt,types.Part.from_bytes(data=screenshot_image, mime_type="image/jpeg")],
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        obj = json.loads(resp.text)
        idx = obj.get("index")
        if not isinstance(idx, int):
            return None
        if idx < 0 or idx >= min(len(buttons), N):
            return None
        return {"index": idx, "reason": obj.get("reason", "")}
    except Exception as e:
        logger.error(f"[llm_choose_unity_candidate] error: {e}")
        return None


def mobile_swipe_on_screen_impl(
    direction: str,
    x: Optional[int] = None,
    y: Optional[int] = None,
    distance: Optional[int] = None,
) -> dict:
    """
    화면에서 스와이프 수행 (dict 반환)
    
    Args:
        direction: 스와이프 방향 ("up", "down", "left", "right")
        x: 스와이프 시작 x 좌표 (픽셀). 제공되지 않으면 화면 중앙 사용
        y: 스와이프 시작 y 좌표 (픽셀). 제공되지 않으면 화면 중앙 사용
        distance: 스와이프 거리 (픽셀). 제공되지 않으면 화면 크기의 30% 사용
    """
    try:
        device_id = current_device or "emulator-5554"
        screen_width, screen_height = _get_screen_size(device_id)
        
        # 기본 거리 설정 (화면 크기의 30%)
        if distance is None:
            if direction in ("up", "down"):
                distance = int(screen_height * 0.3)
            else:  # left, right
                distance = int(screen_width * 0.3)
        
        # 좌표가 제공된 경우
        if x is not None and y is not None:
            start_x = x
            start_y = y
            
            # 방향에 따라 종료 좌표 계산
            if direction == "up":
                end_x, end_y = start_x, start_y - distance
            elif direction == "down":
                end_x, end_y = start_x, start_y + distance
            elif direction == "left":
                end_x, end_y = start_x - distance, start_y
            elif direction == "right":
                end_x, end_y = start_x + distance, start_y
            else:
                return {"error": f"Invalid direction: {direction}"}
            
            # 화면 범위 내로 제한
            end_x = max(0, min(screen_width - 1, end_x))
            end_y = max(0, min(screen_height - 1, end_y))
        else:
            # 좌표가 제공되지 않은 경우 화면 중앙에서 스와이프
            start_x = screen_width // 2
            start_y = screen_height // 2
            
            if direction == "up":
                end_x, end_y = start_x, start_y - distance
            elif direction == "down":
                end_x, end_y = start_x, start_y + distance
            elif direction == "left":
                end_x, end_y = start_x - distance, start_y
            elif direction == "right":
                end_x, end_y = start_x + distance, start_y
            else:
                return {"error": f"Invalid direction: {direction}"}
            
            # 화면 범위 내로 제한
            end_x = max(0, min(screen_width - 1, end_x))
            end_y = max(0, min(screen_height - 1, end_y))
        
        # ADB swipe 명령 실행
        cmd = [
            "adb",
            "-s",
            device_id,
            "shell",
            "input",
            "swipe",
            str(start_x),
            str(start_y),
            str(end_x),
            str(end_y),
            "300",  # duration in milliseconds
        ]
        
        subprocess.run(cmd, capture_output=True)
        
        distance_text = f" {distance} pixels" if distance else ""
        coord_text = f" from coordinates: {start_x}, {start_y}" if (x is not None and y is not None) else ""
        
        return {
            "status": "success",
            "message": f"Swiped {direction}{distance_text}{coord_text}",
            "direction": direction,
            "start": (start_x, start_y),
            "end": (end_x, end_y),
            "distance": distance,
        }
    except Exception as e:
        return {"error": str(e)}


def _get_screen_size(device_id: str) -> Tuple[int, int]:
    """
    디바이스의 화면 크기를 반환합니다.
    Returns:
        (width, height) tuple
    """
    try:
        cmd = ["adb", "-s", device_id, "shell", "wm", "size"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            logger.error(f"Failed to get screen size: {result.stderr.strip()}")
            # 기본값 반환
            return (720, 1280)
        
        # "Physical size: 720x1280" 형식에서 크기 추출
        output = result.stdout.strip()
        if "x" in output:
            parts = output.split()[-1].split("x")
            if len(parts) == 2:
                return (int(parts[0]), int(parts[1]))
        
        logger.error(f"Unexpected screen size output: {output}")
        return (720, 1280)  # 기본값
    except Exception as e:
        logger.error(f"Error getting screen size: {e}")
        return (720, 1280)  # 기본값

def _get_link_text(item: Dict[str, Any]) -> str:
    # 실제 스키마에 맞게 조정
    return (
        item.get("text")
        or item.get("Text")
        or item.get("Label")
        or item.get("SpecifiedName")
        or item.get("GameObjectName")
        or ""
    )

def _get_button_text(item: Dict[str, Any]) -> str:
    return (
        item.get("SpecifiedName")
        or item.get("GameObjectName")
        or item.get("text")
        or item.get("Text")
        or ""
    )
    
def _norm_text(s: str) -> str:
    if not s:
        return ""
    s = s.strip().lower()
    # 공백/구두점 단순화 (필요시 조절)
    s = re.sub(r"[\s\-_]+", " ", s)
    s = re.sub(r"[^\w\s가-힣]", "", s)
    return s

def _sim(a: str, b: str) -> float:
    a, b = _norm_text(a), _norm_text(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()

def _pick_best_fuzzy(
    target: str,
    candidates: List[Dict[str, Any]],
    text_getter,
    min_score: float = 0.72,
) -> Optional[Tuple[Dict[str, Any], float]]:
    """
    candidates 중 target과 가장 유사한 1개 선택.
    text_getter: candidate -> 비교할 텍스트 반환 함수
    """
    # 1) 정규식이면 우선 정규식 매칭 먼저 (기존 동작 유지)
    try:
        rx = re.compile(target, re.IGNORECASE)
        regex_hits = [c for c in candidates if rx.search(_norm_text(text_getter(c)))]
        if regex_hits:
            # regex hit이 여러 개면 그 중에서도 유사도로 1개 고르면 안정적
            best = None
            best_s = -1.0
            for c in regex_hits:
                s = _sim(target, text_getter(c))
                if s > best_s:
                    best, best_s = c, s
            return (best, best_s)
    except re.error:
        # 정규식이 아니라면 그냥 fuzzy로
        pass

    # 2) fuzzy
    best = None
    best_s = -1.0
    for c in candidates:
        s = _sim(target, text_getter(c))
        if s > best_s:
            best, best_s = c, s

    if best is None or best_s < min_score:
        return None
    return (best, best_s)

def mobile_launch_app_impl(package_name: str) -> dict:
    """
    모바일 앱 실행
    """
    try:
        device_id = current_device or "emulator-5554"
        cmd = [
            "adb",
            "-s",
            device_id,
            "shell",
            "monkey",
            "-p",
            package_name,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        ]
        logger.debug(f"Running command: {cmd}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return {
                "status": "error",
                "package": package_name,
                "stderr": result.stderr.strip(),
                "stdout": result.stdout.strip(),
            }
        return {
            "status": "success",
            "package": package_name,
            "stdout": result.stdout.strip(),
        }
    except Exception as e:
        return {"error": str(e)}


def mobile_list_apps_impl() -> dict:
    """현재 디바이스에 설치된 앱 목록 조회 (dict 반환)"""
    try:
        device_id = current_device or "emulator-5554"
        cmd = ["adb", "-s", device_id, "shell", "pm", "list", "packages"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return {
                "status": "error",
                "device": device_id,
                "stderr": result.stderr.strip(),
            }

        packages: List[str] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("package:"):
                packages.append(line.split("package:", 1)[1])

        return {"status": "success", "device": device_id, "packages": packages}
    except Exception as e:
        return {"error": str(e)}


def mobile_terminate_app_impl(package_name: str) -> dict:
    """앱 강제 종료 (dict 반환)"""
    try:
        device_id = current_device or "emulator-5554"
        cmd = ["adb", "-s", device_id, "shell", "am", "force-stop", package_name]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return {
                "status": "error",
                "device": device_id,
                "package": package_name,
                "stderr": result.stderr.strip(),
                "stdout": result.stdout.strip(),
            }
        return {
            "status": "success",
            "device": device_id,
            "package": package_name,
        }
    except Exception as e:
        return {"error": str(e)}


def mobile_install_app_impl(path: str) -> dict:
    """APK/앱 파일 설치 (dict 반환)"""
    try:
        device_id = current_device or "emulator-5554"
        cmd = ["adb", "-s", device_id, "install", "-r", path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            return {
                "status": "error",
                "device": device_id,
                "path": path,
                "stderr": result.stderr.strip(),
                "stdout": result.stdout.strip(),
            }
        return {
            "status": "success",
            "device": device_id,
            "path": path,
            "stdout": result.stdout.strip(),
        }
    except Exception as e:
        return {"error": str(e)}


def mobile_uninstall_app_impl(bundle_id: str) -> dict:
    """앱 제거 (dict 반환)"""
    try:
        device_id = current_device or "emulator-5554"
        cmd = ["adb", "-s", device_id, "uninstall", bundle_id]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return {
                "status": "error",
                "device": device_id,
                "bundle_id": bundle_id,
                "stderr": result.stderr.strip(),
                "stdout": result.stdout.strip(),
            }
        return {
            "status": "success",
            "device": device_id,
            "bundle_id": bundle_id,
            "stdout": result.stdout.strip(),
        }
    except Exception as e:
        return {"error": str(e)}


def mobile_get_screen_size_impl() -> dict:
    """현재 디바이스의 화면 해상도 조회 (dict 반환)"""
    try:
        device_id = current_device or "emulator-5554"
        width, height = _get_screen_size(device_id)
        return {
            "status": "success",
            "device": device_id,
            "width": width,
            "height": height,
        }
    except Exception as e:
        return {"error": str(e)}

def list_devices_impl() -> dict:
    """사용 가능한 모바일 디바이스 목록 조회 (dict 반환)"""
    try:
        # Android 디바이스 목록
        result = subprocess.run(
            ["adb", "devices"],
            capture_output=True,
            text=True,
        )
        devices = []
        for line in result.stdout.split("\n")[1:]:
            if "\t" in line:
                device_id = line.split("\t")[0]
                devices.append(device_id)

        return {"devices": devices}
    except Exception as e:
        return {"error": str(e)}

def set_device_impl(device_id: str) -> dict:
    """사용할 디바이스 설정 (dict 반환)"""
    global current_device
    current_device = device_id
    return {"status": "success", "device": device_id}

def unity_find_buttons_impl() -> List[Dict[str, Any]] | Dict[str, Any]:
    """
    Unity 버튼 목록 조회 (TypeScript mobile_unity_find_buttons 로직 반영)

    - Unity API 에서 버튼 배열을 가져온 뒤
      * PositionY 가 음수인 버튼 제외
      * ScrollView(ParentScrollViewUid) 가 있는 경우, 해당 ScrollView bounds 안에 있는 버튼만 포함
    - 필터링된 버튼의 원본 JSON dict 리스트를 반환
    """
    try:
        import requests

        try:
            response = requests.get(f"{MCP_SERVER_URL}/api/findAllButtons", timeout=5)
            response.raise_for_status()
        except requests.exceptions.ConnectionError:
            return {"error": f"Unity API connection failed: {MCP_SERVER_URL}/api/findAllButtons - Is the Unity API server running?"}
        except requests.exceptions.Timeout:
            return {"error": f"Unity API request timeout: {MCP_SERVER_URL}/api/findAllButtons"}
        except requests.exceptions.HTTPError as e:
            return {"error": f"Unity API HTTP error: {e.response.status_code} - {e.response.text[:200]}"}
        except requests.exceptions.RequestException as e:
            return {"error": f"Unity API request failed: {str(e)}"}
        device_id = current_device or "emulator-5554"
        screen_width, screen_height = _get_screen_size(device_id)    
        try:
            data = response.json()
        except ValueError as e:
            return {"error": f"Unity API returned invalid JSON: {str(e)} - Response: {response.text[:200]}"}

        if not isinstance(data, list):
            return {"error": f"Unity API returned invalid data format: {type(data)} - Expected list, got {type(data)}"}


        buttons = []
        for button in data:
            pos_x = button.get("PositionX")
            pos_y = button.get("PositionY")
            if pos_x is None or pos_y is None:
                continue
            if pos_x < 0 or pos_x > screen_width or pos_y < 0 or pos_y > screen_height:
                continue
            buttons.append(button)
            
        #buttons: List[Dict[str, Any]] = data

        if not buttons:
            # Unity API에서 버튼을 찾지 못함
            return []
        return buttons

    except Exception as e:

        return {"error": str(e)}

async def get_vision_coordinates(image_data: str, button_name: str = None):
    """
    VLM을 사용하여 스크린샷에서 특정 UI 요소의 좌표를 추출합니다.
    """
    # image_path = "screenshot.png"

    # with open(image_path, "rb") as f:
    #     image_bytes = f.read()

    # 프롬프트 구성: 픽셀 좌표를 직접 추출하도록 유도


    width, height = _get_screen_size(current_device)
    prompt = f"""
    당신은 UI 좌표 추출 전문가입니다. 
    제공된 이미지에서 오직 '{button_name}'라는 텍스트나 아이콘을 포함한 버튼 하나를 찾아서 bounding box를 추출해줘.
    제공된 이미지 사이즈는 {width}x{height} 입니다.

    [반환 형식]:
    절대 아래 JSON 형식 이외의 텍스트를 출력하지 마.
    {{
      "bbox": [ymin, xmin, ymax, xmax],
      "reason": "<왜 이 영역이 '{button_name}' 버튼이라고 판단했는지에 대한 간단한 설명>"
    }}

    - bbox 는 픽셀 단위 좌표여야 한다.
    - bbox 배열에는 정확히 네 개의 값만 포함된다.
    - reason 은 한국어 한두 문장으로만 작성한다.
    """
    # base64 문자열을 bytes로 디코딩
    image_bytes = base64.b64decode(image_data)
    
    response = client_gemini.models.generate_content(
        model="gemini-3-pro-preview", # 또는 gemini-1.5-pro
        contents=[
            types.Part.from_bytes(data=image_data, mime_type="image/jpeg"),
            prompt
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
        )
    )
    
    try:
        resp_obj = json.loads(response.text)
        bbox = resp_obj.get("bbox") or []
        reason = resp_obj.get("reason", "")

        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError(f"Invalid bbox format: {bbox}")

        abs_y1 = bbox[0]
        abs_x1 = bbox[1]
        abs_y2 = bbox[2]
        abs_x2 = bbox[3]
    except Exception as e:
        logger.error(f"[get_vision_coordinates] parse error: {e}, raw: {response.text}")
        return {"x": -1, "y": -1, "reason": str(e)}
    # abs_y1 = int(bounding_box["box_2d"][0]/1000 * height)
    # abs_x1 = int(bounding_box["box_2d"][1]/1000 * width)
    # abs_y2 = int(bounding_box["box_2d"][2]/1000 * height)
    # abs_x2 = int(bounding_box["box_2d"][3]/1000 * width)
   
    # base64 문자열을 PIL Image로 변환 (위에서 이미 디코딩한 image_bytes 재사용)
    image_with_boxes = Image.open(io.BytesIO(image_bytes))
    
    draw = ImageDraw.Draw(image_with_boxes)

    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255), (0, 255, 255)]

    # for i, bbox in enumerate([bounding_box]):
    #     x1, y1, x2, y2 = bbox
    #     # Draw rectangle with different colors for each box
    #     color = colors[i % len(colors)]
    draw.rectangle([abs_x1, abs_y1, abs_x2, abs_y2], outline=colors[0], width=3)
        
        # # Optionally add label if available
        # if i < len(bounding_boxes) and isinstance(bounding_boxes[i], dict) and "label" in bounding_boxes[i]:
        #     label = bounding_boxes[i]["label"]
        #     draw.text((x1, y1 - 15), label, fill=color)

    # Save the image with bounding boxes
    output_path = "/Users/111percent/Documents/auto-qa/screenshots_debug/screenshot_with_boxes.png"
    image_with_boxes.save(output_path)
 
    center_x = (abs_x1 + abs_x2) / 2
    center_y = (abs_y1 + abs_y2) / 2

    return {"x": center_x, "y": center_y}

    
async def adb_press_button_impl(button: str) -> dict:
    # 표준화
    if button.lower() not in button_map:
        return {"error": f"Button {button} is not supported"}
    adb_cmd = ["adb", "shell", "input", "keyevent", button_map[button.lower()]]
    subprocess.run(adb_cmd, capture_output=True)
    return {"status": "success", "button": button}

async def click_button_with_coordinates_impl(x: int, y: int) -> dict:
    """
    LLM으로 버튼 클릭
    """
    # bbox center 값 추출
    cmd = [
        "adb",
        "-s",
        current_device,
        "shell",
        "input",
        "tap",
        str(x),
        str(y),
        ]
    subprocess.run(cmd, capture_output=True)
    return {"status": "success"}

def unity_click_button_impl(button: Dict[str, Any]) -> dict:
    """
    Unity 버튼 하나를 직접 클릭
    
    Args:
        button: Unity API에서 받은 단일 버튼 dict
            예) {
                "GameObjectName": "GoogleButton",
                "PositionX": 360.0,
                "PositionY": 445.33,
                "ComponentType": "Button"
            }
    """

    try:
        pos_x = button.get("PositionX")
        pos_y = button.get("PositionY")
        name = button.get("GameObjectName") or button.get("SpecifiedName") or "unknown"

        # 좌표 유효성 검사
        if not isinstance(pos_x, (int, float)) or not isinstance(pos_y, (int, float)):
            return {
                "error": f'Button "{name}" has invalid coordinates (PositionX={pos_x}, PositionY={pos_y}).'
            }

        if pos_y < 0:
            return {
                "error": f'Button "{name}" is outside tappable area (negative PositionY={pos_y}).'
            }

        # 화면 크기 가져오기 (Unity Y -> 화면 Y 변환용)
        device_id = current_device or "emulator-5554"
        screen_width, screen_height = _get_screen_size(device_id)

        # Unity: Y 위로 +, origin bottom-left
        # Screen(ADB): Y 아래로 +, origin top-left
        screen_x = float(pos_x)
        screen_y = float(screen_height - pos_y)

        # 화면 범위 체크
        if not (0 <= screen_x <= screen_width and 0 <= screen_y <= screen_height):
            return {
                "error": (
                    f'Button "{name}" converted position is out of screen bounds. '
                    f"screen=({screen_x}, {screen_y}), size=({screen_width}, {screen_height})."
                )
            }

        # ADB로 클릭
        tap_cmd = [
            "adb",
            "-s",
            device_id,
            "shell",
            "input",
            "tap",
            str(int(screen_x)),
            str(int(screen_y)),
        ]
        subprocess.run(tap_cmd, capture_output=True)

        return {
            "status": "success",
            "button": name,
            "unity_position": {"x": float(pos_x), "y": float(pos_y)},
            "screen_position": {"x": screen_x, "y": screen_y},
        }
    except Exception as e:
        return {"error": str(e)}


def take_screenshot_impl(save_debug: bool = True, debug_dir: str = "./screenshots_debug") -> dict:
    """
    화면 스크린샷 찍기 (dict 반환, base64 이미지 포함)
    
    Args:
        save_debug: 디버깅용 스크린샷을 로컬에 저장할지 여부
        debug_dir: 디버깅 스크린샷 저장 디렉토리
    """
    try:
        # 디버깅 디렉토리 생성
        if save_debug:
            debug_path = Path(debug_dir)
            debug_path.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]  # 밀리초까지
            debug_filename = f"screenshot_{timestamp}.png"
            debug_filepath = debug_path / debug_filename
        
        # 스크린샷 저장
        if current_device:
            subprocess.run(
                [
                    "adb",
                    "-s",
                    current_device,
                    "shell",
                    "screencap",
                    "-p",
                    "/sdcard/screenshot.png",
                ],
                capture_output=True
            )
            # 디버깅용 파일로 저장
            if save_debug:
                subprocess.run(
                    [
                        "adb",
                        "-s",
                        current_device,
                        "pull",
                        "/sdcard/screenshot.png",
                        str(debug_filepath),
                    ],
                    capture_output=True  # stdout/stderr 캡처하여 출력 방지
                )
            
            # 기존 호환성을 위해 screenshot.png도 저장
            subprocess.run(
                [
                    "adb",
                    "-s",
                    current_device,
                    "pull",
                    "/sdcard/screenshot.png",
                    "screenshot.png",
                ],
                capture_output=True
            )
        else:
            subprocess.run(
                ["adb", "shell", "screencap", "-p", "/sdcard/screenshot.png"],
                capture_output=True
            )
            # 디버깅용 파일로 저장
            if save_debug:
                subprocess.run(
                    [
                        "adb",
                        "pull",
                        "/sdcard/screenshot.png",
                        str(debug_filepath),
                    ],
                    capture_output=True  # stdout/stderr 캡처하여 출력 방지
                )
            
            # 기존 호환성을 위해 screenshot.png도 저장
            subprocess.run(
                ["adb", "pull", "/sdcard/screenshot.png", "screenshot.png"],
                capture_output=True
            )

        # Base64로 인코딩
        with open("screenshot.png", "rb") as f:
            image_data = base64.b64encode(f.read()).decode()

        result = {"image": image_data}
        if save_debug:
            result["debug_path"] = str(debug_filepath)
        
        return result
    except Exception as e:
        return {"error": str(e)}


def unity_scroll_impl(direction: str = "down") -> dict:
    """
    Unity ScrollView 스크롤 (dict 반환)

    - findAllButtons 응답 구조(ScreenPosition 등)에 의존하지 않고
      ADB 화면 크기 기반으로 단순 스와이프 수행
    """
    try:
        device_id = current_device or "emulator-5554"
        screen_width, screen_height = _get_screen_size(device_id)

        start_x = screen_width // 2
        y_start = int(screen_height * 0.7)
        y_end = int(screen_height * 0.3)

        if direction == "down":
            # 화면을 아래로 스크롤: 손가락은 위로 이동 (y_start -> y_end)
            swipe_start_y, swipe_end_y = y_start, y_end
        elif direction == "up":
            # 화면을 위로 스크롤: 손가락은 아래로 이동 (y_end -> y_start)
            swipe_start_y, swipe_end_y = y_end, y_start
        else:
            return {"error": "Invalid direction"}

        cmd = [
            "adb",
            "-s",
            device_id,
            "shell",
            "input",
            "swipe",
            str(start_x),
            str(swipe_start_y),
            str(start_x),
            str(swipe_end_y),
            "800",
        ]

        subprocess.run(cmd, capture_output=True)

        return {
            "status": "success",
            "direction": direction,
            "start": (start_x, swipe_start_y),
            "end": (start_x, swipe_end_y),
        }
    except Exception as e:
        return {"error": str(e)}


def get_foreground_app_impl() -> Dict[str, Any]:
    """
    현재 포그라운드 앱 패키지 이름 조회.

    여러 방법을 시도:
    1) dumpsys activity activities | grep mResumedActivity (가장 정확)
    2) dumpsys window windows 에서 isOnScreen=true, isVisible=true 인 Window 찾기
    3) 문자열 포함 체크 (Google Play 전용)
    """
    try:
        device_id = current_device or "emulator-5554"

        # 방법 1: dumpsys activity activities (가장 정확)
        cmd1 = ["adb", "-s", device_id, "shell", "dumpsys", "activity", "activities"]
        result1 = subprocess.run(cmd1, capture_output=True, text=True, timeout=3)
        out1 = result1.stdout

        # mResumedActivity 패턴 찾기
        m1 = re.search(r"mResumedActivity.*?([a-zA-Z0-9._]+)/(?:[a-zA-Z0-9._$]+)", out1)
        if m1:
            package = m1.group(1)
            return {"package": package, "method": "dumpsys_activity"}

        # 방법 2: dumpsys window windows 에서 isOnScreen=true, isVisible=true 인 Window 찾기
        cmd2 = ["adb", "-s", device_id, "shell", "dumpsys", "window", "windows"]
        result2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=3)
        out2 = result2.stdout

        # Window{... u0 com.android.vending/...} 형태에서 패키지 추출
        # isOnScreen=true, isVisible=true 인 Window 우선
        windows = list(re.finditer(
            r"Window\{[^}]+\}\s+u0\s+([a-zA-Z0-9._]+)/(?:[a-zA-Z0-9._$]+)", out2
        ))
        for win_match in windows:
            # 해당 Window 블록에서 isOnScreen=true, isVisible=true 확인
            win_start = win_match.start()
            # 다음 Window 또는 파일 끝까지
            next_win = out2.find("Window{", win_match.end())
            win_block = out2[win_start : (next_win if next_win > 0 else len(out2))]

            if "isOnScreen=true" in win_block and "isVisible=true" in win_block:
                package = win_match.group(1)
                return {"package": package, "method": "dumpsys_window_visible"}

        # 방법 3: 문자열 포함 체크 (Google Play 전용, fallback)
        if "com.android.vending" in out2:
            return {"package": "com.android.vending", "method": "string_match"}

        return {"package": "", "method": "none", "raw": out2[:500]}
    except Exception as e:
        return {"error": str(e)}


def dump_ui_automator_impl() -> Dict[str, Any]:
    """
    UIAutomator XML 덤프를 떠서 간단한 요소 리스트로 변환.

    반환 형식:
    {
      "elements": [
        {
          "text": str,
          "content_desc": str,
          "resource_id": str,
          "class_name": str,
          "clickable": bool,
          "bounds": "[x1,y1][x2,y2]",
          "center": {"x": int, "y": int},
        },
        ...
      ]
    }
    """
    import os

    try:
        device_id = current_device or "emulator-5554"

        # 1) uiautomator dump 생성
        result1 = subprocess.run(
            ["adb", "-s", device_id, "shell", "uiautomator", "dump", "/sdcard/window_dump.xml"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result1.returncode != 0:
            return {
                "error": f"uiautomator dump failed: {result1.stderr}",
                "elements": [],
            }

        # 2) 파일 가져오기
        result2 = subprocess.run(
            ["adb", "-s", device_id, "pull", "/sdcard/window_dump.xml", "window_dump.xml"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result2.returncode != 0:
            return {
                "error": f"adb pull failed: {result2.stderr}",
                "elements": [],
            }

        # 3) 파일 존재 확인
        if not os.path.exists("window_dump.xml"):
            return {
                "error": "window_dump.xml file not found after pull",
                "elements": [],
            }

        file_size = os.path.getsize("window_dump.xml")

        if file_size == 0:
            return {
                "error": "window_dump.xml is empty",
                "elements": [],
            }

        # 4) XML 파싱
        tree = ET.parse("window_dump.xml")
        root = tree.getroot()

        elements: List[Dict[str, Any]] = []

        def parse_bounds(b: str) -> Dict[str, int]:
            # 예: "[0,1728][1080,2034]"
            m = re.match(r"\[(\d+),(\d+)]\[(\d+),(\d+)]", b or "")
            if not m:
                return {"x1": 0, "y1": 0, "x2": 0, "y2": 0}
            x1, y1, x2, y2 = map(int, m.groups())
            return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}

        for node in root.iter("node"):
            text = node.get("text", "") or ""
            content_desc = node.get("content-desc", "") or ""
            resource_id = node.get("resource-id", "") or ""
            class_name = node.get("class", "") or ""
            clickable = node.get("clickable", "false") == "true"
            bounds_str = node.get("bounds", "") or ""
            bounds = parse_bounds(bounds_str)
            cx = (bounds["x1"] + bounds["x2"]) // 2
            cy = (bounds["y1"] + bounds["y2"]) // 2

            elements.append(
                {
                    "text": text,
                    "content_desc": content_desc,
                    "resource_id": resource_id,
                    "class_name": class_name,
                    "clickable": clickable,
                    "bounds": bounds_str,
                    "center": {"x": cx, "y": cy},
                }
            )

        return {"elements": elements}
    except ET.ParseError as e:
        return {
            "error": f"XML parse error: {str(e)}",
            "elements": [],
        }
    except FileNotFoundError as e:
        return {
            "error": f"File not found: {str(e)}",
            "elements": [],
        }
    except Exception as e:
        return {
            "error": f"Unexpected error: {str(e)}",
            "elements": [],
        }

def _match_text(target: str, elem: Dict[str, Any]) -> bool:
    # target: "동의|허용|Allow" 같은 regex 가능
    pat = re.compile(target, re.IGNORECASE)
    for k in ("text", "content_desc", "resource_id", "class_name"):
        v = (elem.get(k) or "")
        if v and pat.search(v):
            return True
    return False


async def click_uiauto_impl(target: str) -> Dict[str, Any]:
    """UIAutomator 기반으로 요소를 찾아 클릭"""
    find_result = await find_uiauto_impl(target)
    
    if find_result.get("status") != "found":
        return find_result
    
    chosen = find_result.get("element")
    cx, cy = chosen["center"]["x"], chosen["center"]["y"]
    await click_button_with_coordinates_impl(x=cx, y=cy)

    return {"status": "success", "method": "uiautomator", "clicked": chosen}

def _match_unity(target: str, btn: Dict[str, Any]) -> bool:
    pat = re.compile(target, re.IGNORECASE)
    for k in ("GameObjectName", "SpecifiedName", "Text", "Name"):
        v = btn.get(k)
        if isinstance(v, str) and pat.search(v):
            return True
    return False


# ========= FastMCP 툴 래퍼 (기존 MCP 프로토콜용) =========

async def find_uiauto_impl(target: str) -> Dict[str, Any]:
    """UIAutomator 기반으로 요소 찾기만 수행 (클릭하지 않음)"""
    dump = dump_ui_automator_impl()
    elements = dump.get("elements", [])
    if not elements:
        return {"status": "not_found", "reason": "no uiautomator elements"}

    # clickable 우선 + 텍스트 매칭
    candidates = [e for e in elements if e.get("clickable") and _match_text(target, e)]
    if not candidates:
        # clickable 아니어도 매칭되는게 있으면 일단 클릭 후보로
        candidates = [e for e in elements if _match_text(target, e)]

    if not candidates:
        return {"status": "not_found", "reason": f"no match for target={target}"}

    # 가장 큰 버튼/중앙에 가까운 요소 등 고도화 가능. 일단 첫 번째.
    chosen = candidates[0]
    return {"status": "found", "method": "uiautomator", "element": chosen}

async def find_vision_impl(button_name: str) -> Dict[str, Any]:
    """Vision 기반으로 버튼 찾기만 수행 (클릭하지 않음)"""
    if not button_name or len(button_name.strip()) == 0:
        return {"status": "error", "reason": "button_name is empty"}

    screenshot_result = take_screenshot_impl()
    image_data = screenshot_result["image"]

    vision_data = await get_vision_coordinates(image_data, button_name)

    x, y = vision_data["x"], vision_data["y"]

    # 못 찾으면 -1 리턴하도록 prompt에 써놨으니 여기서 막아야 함
    if x < 0 or y < 0:
        return {"status": "not_found", "reason": f"'{button_name}' not found on screen"}

    return {
        "status": "found",
        "method": "vision",
        "coordinates": {"x": int(x), "y": int(y)},
        "button_name": button_name
    }
def _get_unity_search_text(btn: Dict[str, Any]) -> str:
    parts = []
    for k in ("SpecifiedName", "GameObjectName", "Text", "Name"):
        v = btn.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    meta = btn.get("ParentMetadata")
    if isinstance(meta, str) and meta.strip():
        parts.append(meta.strip())
    return " | ".join(parts)

async def smart_find_impl(
    target: str,
    strategy: str = "auto",
) -> Dict[str, Any]:
    """
    target(정규식 가능)으로 버튼을 찾기만 수행 (클릭하지 않음).
    우선순위: Unity Hyperlink Text -> Unity Button -> UIAutomator -> Vision
    
    Returns:
        {
            "status": "found" | "not_found",
            "method": "unity_hyperlink_text" | "unity_button" | "uiautomator" | "vision",
            "element": {...},  # 찾은 요소 정보
            "score": float,  # 유사도 점수 (fuzzy 매칭인 경우)
            ...
        }
    """
    if not target or not target.strip():
        return {"status": "error", "reason": "empty target"}
    
    # 1) Unity Hyperlink Text
    if strategy in ("auto", "unity"):
        links = unity_hyperlink_text_impl()
        picked = await llm_choose_unity_candidate(target, links)
        if picked:
            link = links[picked["index"]]
            return {
                "status": "found",
                "method": "unity_hyperlink_text",
                "element": link,
                "score": 1.0,
                "reason": picked.get("reason", "")
            }
        # return {"status": "not_found", "reason": "no link found"}
        # if isinstance(links, list) and links:
        #     picked = _pick_best_fuzzy(
        #         target,
        #         links,
        #         text_getter=_get_link_text,
        #         min_score=0.2,  # 링크 텍스트는 짧으니 약간 낮게
        #     )
        #     if picked:
        #         link, score = picked
        #         return {
        #             "status": "found",
        #             "method": "unity_hyperlink_text",
        #             "element": link,
        #             "score": score
        #         }

    # 2) Unity Button
    if strategy in ("auto", "unity"):
        unity_buttons = unity_find_buttons_impl()
        if isinstance(unity_buttons, list) and unity_buttons:
            # 1) 먼저 규칙 기반(빠르고 공짜)으로 대충 컷다운
            #    - Frame/음수Y/화면 밖 제거는 이미 unity_find_buttons_impl에서 하고 있음
            #    - 그래도 너무 많으면 text가 있는 애들 우선
            pre = unity_buttons
            with_text = [b for b in pre if (b.get("SpecifiedName") or b.get("Text"))]
            if with_text:
                pre = with_text

            # 2) LLM에게 최종 1개 선택 맡김 (alias 하드코딩 X)
            picked = await llm_choose_unity_candidate(target, pre)

            if picked:
                btn = pre[picked["index"]]
                return {
                    "status": "found",
                    "method": "unity_button",
                    "element": btn,
                    "score": 1.0,
                    "reason": picked.get("reason", "")
                }



    # # 2) Unity Button
    # if strategy in ("auto", "unity"):
    #     unity_buttons = unity_find_buttons_impl()
                
    #     if isinstance(unity_buttons, list) and unity_buttons:
    #         picked = _pick_best_fuzzy(
    #             target,
    #             unity_buttons,
    #             text_getter=_get_button_text,
    #             min_score=0.5,
    #         )
    #         if picked:
    #             btn, score = picked
    #             return {
    #                 "status": "found",
    #                 "method": "unity_button",
    #                 "element": btn,
    #                 "score": score
    #             }

    # 3) UIAutomator
    if strategy in ("auto", "uiauto"):
        out = await find_uiauto_impl(target)
        if out.get("status") == "found":
            return out

    # 4) Vision
    if strategy in ("auto", "vision"):
        out = await find_vision_impl(target)
        if out.get("status") == "found":
            return out

    return {"status": "not_found", "reason": "no strategy matched"}

@mcp.tool()
async def smart_find(
    target: str,
    strategy: str = "auto",
) -> str:
    """
    target(정규식 가능)으로 버튼을 찾기만 수행 (클릭하지 않음).
    우선순위: Unity Hyperlink Text -> Unity Button -> UIAutomator -> Vision
    """
    result = await smart_find_impl(target=target, strategy=strategy)
    return json.dumps(result, ensure_ascii=False)

@mcp.tool()
async def smart_click(
    target: str,
    strategy: str = "auto",
    max_scroll: int = 2,
) -> str:
    """
    target(정규식 가능)으로 버튼을 찾아 클릭.
    우선순위: Unity Hyperlink Text -> Unity Button -> UIAutomator -> Vision
    실패 시 스크롤 후 재시도.
    """
    if not target or not target.strip():
        return json.dumps({"status": "error", "reason": "empty target"}, ensure_ascii=False)
    
    async def _try_once() -> Dict[str, Any]:
        # smart_find_impl로 버튼 찾기
        find_result = await smart_find_impl(target=target, strategy=strategy)
        
        if find_result.get("status") != "found":
            return find_result
        
        # 찾은 버튼을 클릭
        method = find_result.get("method")
        element = find_result.get("element")
        
        if method == "unity_hyperlink_text" or method == "unity_button":
            # Unity 버튼 클릭
            out = unity_click_button_impl(element)
            return {
                "status": "success",
                "method": method,
                "score": find_result.get("score"),
                "detail": out
            }
        elif method == "uiautomator":
            # UIAutomator 요소 클릭
            chosen = element
            cx, cy = chosen["center"]["x"], chosen["center"]["y"]
            await click_button_with_coordinates_impl(x=cx, y=cy)
            return {
                "status": "success",
                "method": "uiautomator",
                "detail": {"clicked": chosen}
            }
        elif method == "vision":
            # Vision 좌표 클릭
            coords = find_result.get("coordinates", {})
            x, y = coords["x"], coords["y"]
            await click_button_with_coordinates_impl(x=x, y=y)
            return {
                "status": "success",
                "method": "vision",
                "detail": f"clicked '{find_result.get('button_name')}' at ({x}, {y})"
            }
        
        return {"status": "error", "reason": f"unknown method: {method}"}

    # 스크롤 포함 retry
    for i in range(max_scroll + 1):
        res = await _try_once()
        if res.get("status") == "success":
            return json.dumps(res, ensure_ascii=False)

        if i < max_scroll:
            unity_scroll_impl("down")
            await asyncio.sleep(0.5)

    return json.dumps({"status": "not_found", "target": target}, ensure_ascii=False)

@mcp.tool()
def list_devices() -> str:
    """사용 가능한 모바일 디바이스 목록 조회 (JSON 문자열 반환)"""
    return json.dumps(list_devices_impl())


@mcp.tool()
def set_device(device_id: str) -> str:
    """사용할 디바이스 설정 (JSON 문자열 반환)"""
    return json.dumps(set_device_impl(device_id))

@mcp.tool()
def unity_find_buttons() -> str:
    """Unity 게임에서 버튼 찾기 (JSON 문자열 반환)"""
    result = unity_find_buttons_impl()
    return json.dumps(result)


def unity_click_button(
    button: Dict[str, Any]
) -> str:
    """Unity 버튼 클릭 (JSON 문자열 반환)
    
    Args:
        button: unity_find_buttons 결과에서 받은 버튼 객체 전체 (Dict)
            필수 필드: PositionX, PositionY (좌표)
            예: {"GameObjectName": "GoggleButton", "PositionX": 360.0, "PositionY": 445.33, "ComponentType": "Button"}
            주의: button_name (문자열)이 아니라 button 객체 전체를 전달해야 함
    """
    return json.dumps(unity_click_button_impl(button=button))


@mcp.tool()
def take_screenshot(save_debug: bool = True, debug_dir: str = "./screenshots_debug") -> str:
    """화면 스크린샷 찍기 (JSON 문자열 반환)"""
    return json.dumps(take_screenshot_impl(save_debug=save_debug, debug_dir=debug_dir))


@mcp.tool()
def unity_scroll(direction: str = "down") -> str:
    """Unity ScrollView 스크롤 (JSON 문자열 반환)"""
    return json.dumps(unity_scroll_impl(direction=direction))


@mcp.tool()
def get_foreground_app() -> str:
    """현재 포그라운드 앱 패키지 이름 조회 (JSON 문자열 반환)"""
    return json.dumps(get_foreground_app_impl())



@mcp.tool()
def mobile_list_apps() -> str:
    """현재 선택된 디바이스에 설치된 앱 목록 조회 (JSON 문자열 반환)"""
    return json.dumps(mobile_list_apps_impl())


@mcp.tool()
def mobile_launch_app(package_name: str) -> str:
    """모바일 앱 실행 (JSON 문자열 반환)"""
    return json.dumps(mobile_launch_app_impl(package_name))


@mcp.tool()
def mobile_terminate_app(package_name: str) -> str:
    """모바일 앱 강제 종료 (JSON 문자열 반환)"""
    return json.dumps(mobile_terminate_app_impl(package_name))


@mcp.tool()
def mobile_install_app(path: str) -> str:
    """APK/앱 파일 설치 (JSON 문자열 반환)"""
    return json.dumps(mobile_install_app_impl(path))


@mcp.tool()
def mobile_uninstall_app(bundle_id: str) -> str:
    """앱 제거 (JSON 문자열 반환)"""
    return json.dumps(mobile_uninstall_app_impl(bundle_id))
@mcp.tool()
def dump_ui_automator() -> str:
    """UIAutomator XML 덤프 후 요소 리스트 반환 (JSON 문자열)"""
    return json.dumps(dump_ui_automator_impl())

@mcp.tool()
def mobile_get_screen_size() -> str:
    """현재 디바이스의 화면 해상도 조회 (JSON 문자열 반환)"""
    return json.dumps(mobile_get_screen_size_impl())

@mcp.tool()
async def click_button_with_coordinates(x: int, y: int) -> str:
    """LLM으로 버튼 클릭 (JSON 문자열 반환)"""
    result = await click_button_with_coordinates_impl(x=x, y=y)
    return json.dumps(result)

async def vision_enhanced_click_impl(button_name: str) -> str:
    """Vision 기반 버튼 클릭 구현 함수"""
    find_result = await find_vision_impl(button_name)
    
    if find_result.get("status") != "found":
        if find_result.get("status") == "error":
            return f"ERROR: {find_result.get('reason', 'unknown error')}"
        return f"NOT_FOUND: {find_result.get('reason', 'button not found')}"
    
    coords = find_result.get("coordinates", {})
    x, y = coords["x"], coords["y"]
    await click_button_with_coordinates_impl(x=x, y=y)
    return f"OK: clicked '{button_name}' at ({x}, {y})"

async def vision_enhanced_click(button_name: str) -> str:
    """Vision 기반 버튼 클릭 (JSON 문자열 반환)"""
    return await vision_enhanced_click_impl(button_name)

@mcp.tool()
async def uiauto_click(target: str) -> str:
    """UIAutomator 기반으로 target(정규식 가능)에 매칭되는 요소를 클릭"""
    result = await click_uiauto_impl(target)
    return json.dumps(result, ensure_ascii=False)

def unity_hyperlink_text() -> str:
    """
    Unity 텍스트 목록 조회 (HyperLinkPositions -> clickable positions로 변환)
    """
    return json.dumps(unity_hyperlink_text_impl())


def unity_hyperlink_text_impl() -> List[Dict[str, Any]]:
    """
    Unity 텍스트 목록 조회 (HyperLinkPositions -> clickable positions로 변환)
    """
    try:
        import requests
        response = requests.get(f"{MCP_SERVER_URL}/api/findHyperTextPositions", timeout=5)
        response.raise_for_status()
        data = response.json()

        buttons = []
        screen_width, screen_height = _get_screen_size(current_device)

        for text_item in data:
            hyperlink_positions = text_item.get("HyperLinkPositions", []) or []
            if hyperlink_positions:
                for pos in hyperlink_positions:
                    pos_x = pos.get("PositionX")
                    pos_y = pos.get("PositionY")
                    if pos_x is None or pos_y is None:
                        continue
                    if pos_x < 0 or pos_x > screen_width or pos_y < 0 or pos_y > screen_height:
                        continue

                    buttons.append({
                        "GameObjectName": text_item.get("GameObjectName"),
                        "PositionX": pos_x,
                        "PositionY": pos_y,
                        "Text": text_item.get("Text", "") or "",
                        # 디버그/식별용
                        "Type": "HyperLinkPosition",
                    })
            else:
                # 하위 호환: 최상위 PositionX/Y가 있는 형태
                pos_x = text_item.get("PositionX")
                pos_y = text_item.get("PositionY")
                if pos_x is not None and pos_y is not None:
                    if 0 <= pos_x <= screen_width and 0 <= pos_y <= screen_height:
                        text_item2 = dict(text_item)
                        text_item2["Type"] = "HyperText"
                        buttons.append(text_item2)

        return buttons

    except requests.exceptions.ConnectionError:
        return {"error": f"Unity API connection failed: {MCP_SERVER_URL}/api/findHyperTextPositions"}
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
async def adb_press_button(button: str) -> str:
    """ADB 버튼 클릭 (JSON 문자열 반환)"""
    result = await adb_press_button_impl(button=button)
    return json.dumps(result)

# @mcp.tool()
# async def handle_system_dialogs() -> str:
#     """
#     흔한 시스템 팝업 자동 처리:
#     Allow/허용/OK/확인/동의/Agree/Close/닫기 등
#     """
#     patterns = [
#         r"허용|Allow",
#         r"확인|OK",
#         r"동의|Agree",
#         r"닫기|Close|Cancel|취소",
#         r"다음|Next",
#     ]

#     for p in patterns:
#         out = await click_uiauto_impl(p)
#         if out.get("status") == "success":
#             return json.dumps({"status": "handled", "pattern": p, "detail": out}, ensure_ascii=False)

#     return json.dumps({"status": "none"}, ensure_ascii=False)

@mcp.tool()
def mobile_swipe_on_screen(
    direction: str,
    x: Optional[int] = None,
    y: Optional[int] = None,
    distance: Optional[int] = None,
) -> str:
    """
    화면에서 스와이프 수행 (JSON 문자열 반환)
    
    Args:
        direction: 스와이프 방향 ("up", "down", "left", "right")
        x: 스와이프 시작 x 좌표 (픽셀). 제공되지 않으면 화면 중앙 사용
        y: 스와이프 시작 y 좌표 (픽셀). 제공되지 않으면 화면 중앙 사용
        distance: 스와이프 거리 (픽셀). 제공되지 않으면 화면 크기의 30% 사용
    """
    return json.dumps(mobile_swipe_on_screen_impl(direction=direction, x=x, y=y, distance=distance), ensure_ascii=False)

# @mcp.tool()
# async def vision_assert_text(expected: str, image_path: Optional[str] = None) -> Dict[str, Any]:
#     """
#     Args:
#         expected: 화면에서 존재해야 하는 텍스트
#         image_path: 검사할 이미지 경로 (없으면 마지막 스크린샷 사용)
#     Returns:
#         {
#           "status": "present" | "absent",
#           "expected": "...",
#           "found_text": "...",
#         }
#     """
#     try:
#         img = Image.open(image_path)
#         text = pytesseract.image_to_string(img, lang="kor+eng")

#         found = expected in text
#         return {
#             "status": "present" if found else "absent",
#             "expected": expected,
#             "found_text": text[:2000],  # 너무 길면 잘라서 반환
#         }
#     except Exception as e:
#         return {
#             "status": "absent",
#             "expected": expected,
#             "error": str(e),
#         }


# @mcp.tool()
# def smart_assert(target: str) -> str:
#     """
#     target(정규식 가능)이 화면에 존재하는지 체크 (uiauto 기반)
#     """
#     dump = dump_ui_automator_impl()
#     elements = dump.get("elements", [])
#     pat = re.compile(target, re.IGNORECASE)

#     for e in elements:
#         for k in ("text", "content_desc", "resource_id"):
#             v = (e.get(k) or "")
#             if v and pat.search(v):
#                 return json.dumps({"status": "present", "matched": v, "element": e}, ensure_ascii=False)

#     return json.dumps({"status": "absent", "target": target}, ensure_ascii=False)

# 서버 실행 (개발 모드)
if __name__ == "__main__":
    try:
        logger.info("🚀 Starting MCP server...")
        mcp.run()
    except KeyboardInterrupt:
        logger.warning("⚠️ MCP server interrupted by user")
        # 로그 강제 flush
        for handler in logger.handlers:
            handler.flush()
    except Exception as e:
        logger.error(f"❌ MCP server error: {e}")
        # 로그 강제 flush
        for handler in logger.handlers:
            handler.flush()
        raise
    finally:
        logger.info("🔌 MCP server shutting down...")
        # 최종 로그 flush
        for handler in logger.handlers:
            handler.flush()