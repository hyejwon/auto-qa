"""
get_vision_coordinates 함수 테스트
"""
from typing import Tuple 
import pytest
import json
import base64
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from io import BytesIO
from PIL import Image
import subprocess
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
import cv2
import numpy as np

def debug_visualize(main_img_path, template_path, result_coords):
    img = cv2.imread(main_img_path)
    # 중앙에 빨간 점 그리기
    cv2.circle(img, (int(result_coords['x']), int(result_coords['y'])), 10, (0, 0, 255), -1)
    cv2.imshow('Detection Result', img)
    cv2.waitKey(0)
    cv2.destroyAllWindows()

def get_coordinates_by_opencv(main_image_path, template_path, threshold=0.3):
    """
    OpenCV를 사용하여 메인 이미지에서 템플릿 아이콘의 중앙 좌표를 찾습니다.
    """
    # 1. 이미지 로드 (채널 주의: OpenCV는 BGR로 읽음)
    img_rgb = cv2.imread(main_image_path)
    template = cv2.imread(template_path)
    
    # 아이콘의 너비와 높이 저장
    h, w = template.shape[:2]

    # 2. 템플릿 매칭 실행
    # TM_CCOEFF_NORMED: 상관계수를 정규화하여 0~1 사이 값으로 반환 (추천)
    res = cv2.matchTemplate(img_rgb, template, cv2.TM_CCOEFF_NORMED)
    
    # 3. 임계치(Threshold) 이상의 결과 위치 추출
    min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
    
    # max_val: 가장 유사한 지점의 신뢰도 (0.0 ~ 1.0)
    # max_loc: 가장 유사한 지점의 좌측 상단 좌표 (x, y)
    
    if max_val >= threshold:
        top_left = max_loc
        center_x = top_left[0] + w // 2
        center_y = top_left[1] + h // 2
        print(f"아이콘 발견! 신뢰도: {max_val:.4f}") 
        
        return {"x": center_x, "y": center_y, "confidence": max_val}
    else:
        print(f"아이콘을 찾을 수 없습니다. (최고 신뢰도: {max_val:.4f})")
        return {"x": -1, "y": -1, "confidence": max_val}

# 사용 예시
# result = get_coordinates_by_opencv("screenshot.png", "thunder_icon.png")
def _get_image_bytes(file_path:str):
    with open(file_path,'rb') as f:
        img_bytes = f.read
    return img_bytes



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
                    return device_id
    except Exception as e:
        return None

# 프로젝트 루트 경로
PROJECT_ROOT = Path(__file__).parent.parent

# 테스트용 이미지 경로
TEST_IMAGES_DIR = PROJECT_ROOT / "tests" / "fixtures" / "images"
SCREENSHOTS_DEBUG_DIR = PROJECT_ROOT / "screenshots_debug"

current_device = _get_first_connected_device()


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
            # 기본값 반환
            return (720, 1280)
        
        # "Physical size: 720x1280" 형식에서 크기 추출
        output = result.stdout.strip()
        if "x" in output:
            parts = output.split()[-1].split("x")
            if len(parts) == 2:
                return (int(parts[0]), int(parts[1]))
        
        return (720, 1280)  # 기본값
    except Exception as e:
        return (720, 1280)  # 기본값

def get_vision_coordinates(image_data: str, button_name: str = None):
    """
    VLM을 사용하여 스크린샷에서 특정 UI 요소의 좌표를 추출합니다.
    """
 
    
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
            # Base64로 인코딩

    # base64 문자열을 bytes로 디코딩
    image_bytes = base64.b64decode(image_data)
    
    response = client_gemini.models.generate_content(
        model="gemini-2.5-pro", # 또는 gemini-1.5-pro
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
        return {"x": -1, "y": -1, "reason": str(e)}

    image_with_boxes = Image.open(io.BytesIO(image_bytes))
    
    draw = ImageDraw.Draw(image_with_boxes)

    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255), (0, 255, 255)]

    draw.rectangle([abs_x1, abs_y1, abs_x2, abs_y2], outline=colors[0], width=3)
    # Save the image with bounding boxes
    output_path = "/Users/111percent/Documents/auto-qa/screenshots_debug/screenshot_with_boxes.png"
    image_with_boxes.save(output_path)

    center_x = (abs_x1 + abs_x2) / 2
    center_y = (abs_y1 + abs_y2) / 2

    return {"x": center_x, "y": center_y}


def get_vision_coordinates_multimodal(image_data: str,ref_data:str, button_name: str = None):
    """
    VLM을 사용하여 스크린샷에서 특정 UI 요소의 좌표를 추출합니다.
    """
 
    
    width, height = _get_screen_size(current_device)
    prompt = f"""
    첫 번째 이미지에서 {button_name}버튼을 찾아 클릭하려고한다. 버튼 위에 검정색 텍스트로 레이블링이 되어있음. 해당 텍스트를 찾아서 아래 버튼의 좌표를 bbox 로 반환
    [반환 형식]:
    {{
      "bbox": [ymin, xmin, ymax, xmax],
      "reason": "해당 위치에 아이콘이 존재합니다."
    }}
    
    """
            # Base64로 인코딩

    # base64 문자열을 bytes로 디코딩
    # image_bytes = base64.b64decode(image_data)
    response = client_gemini.models.generate_content(
        model="gemini-3-pro-preview", # 또는 gemini-1.5-pro
        contents=[
            Image.open(image_data),
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
        return {"x": -1, "y": -1, "reason": str(e)}

    image_with_boxes = Image.open(image_data)
    
    draw = ImageDraw.Draw(image_with_boxes)

    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255), (0, 255, 255)]

    draw.rectangle([abs_x1, abs_y1, abs_x2, abs_y2], outline=colors[0], width=3)
    # Save the image with bounding boxes
    output_path = "/Users/111percent/Documents/auto-qa/screenshots_debug/screenshot_with_boxes.png"
    image_with_boxes.save(output_path)

    center_x = (abs_x1 + abs_x2) / 2
    center_y = (abs_y1 + abs_y2) / 2

    return {"x": center_x, "y": center_y}





if __name__ == "__main__":
    # with open("/Users/111percent/Documents/auto-qa/screenshots_debug/screenshot_20260205_193127_673.png", "rb") as f:
    #     image_data = base64.b64encode(f.read()).decode()
    # image_data = _get_image_bytes("/Users/111percent/Documents/auto-qa/screenshots_debug/screenshot_20260205_193127_673.png")
    # ref_data = _get_image_bytes("/Users/111percent/Documents/auto-qa/stamina.png")
    image_data = "/Users/111percent/Documents/auto-qa/screenshots_debug/screenshot_20260205_193127_673.png"
    ref_data = "/Users/111percent/Documents/auto-qa/stamina.png"
    
    # get_vision_coordinates(image_data=image_data ,button_name="랜덤매칭")
   
    get_vision_coordinates_multimodal(image_data= image_data, ref_data=ref_data, button_name="스태미너")
    #result_coords = get_coordinates_by_opencv(image_data,ref_data)
    #debug_visualize(main_img_path=image_data, template_path =ref_data, result_coords=result_coords)
    