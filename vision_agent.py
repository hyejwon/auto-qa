from google.genai import types
from google import genai
from PIL import Image, ImageDraw
import json
from pathlib import Path
from typing import Dict, Optional, List
from datetime import datetime
import logging
from pydantic import BaseModel
from langsmith import wrappers
from langsmith import traceable

logger = logging.getLogger(__name__)

class BoundingBox(BaseModel):
    """좌표 정보 (화면비 기준 0~1)"""
    x1: float
    y1: float
    x2: float
    y2: float
    
    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2)
    
    def to_pixels(self, width: int, height: int) -> Dict[str, int]:
        """픽셀 좌표로 변환"""
        return {
            "x1": int(width * self.x1),
            "y1": int(height * self.y1),
            "x2": int(width * self.x2),
            "y2": int(height * self.y2),
            "x": int(width * self.center[0]),
            "y": int(height * self.center[1])
        }

class VisionResult(BaseModel):
    """Vision 분석 결과"""
    success: bool
    bbox: Optional[BoundingBox] = None
    description: str = ""
    confidence: float = 0.0
    error: Optional[str] = None

class GeminiVisionAgent:
    """Gemini Vision API 에이전트"""
    
    def __init__(self, project: str, location: str = "global", 
                 model: str = "gemini-2.0-flash-exp"):
        gemini_client= genai.Client(
            vertexai=True,
            project=project,
            location=location
        )
        self.client = wrappers.wrap_gemini(
            gemini_client,
            tracing_extra= {
                "tags": ["gemini","python"],
                "metadata":{
                    "interegration":"google-genai",
                },
            },
        )
        self.model = model
        logger.info(f"Initialized Gemini Vision Agent: {model}")
    @traceable
    def find_element(
        self,
        image_path: Path,
        target_description: str,
        debug_dir: Optional[Path] = None
    ) -> VisionResult:
        """
        화면에서 특정 UI 요소 찾기
        
        Args:
            image_path: 스크린샷 경로
            target_description: 찾을 요소 설명 (예: "스태미너 충전 아이콘")
            debug_dir: 디버그 이미지 저장 경로
        """
        prompt = f"""
        이 게임 화면에서 '{target_description}'을(를) 찾아서 정확한 위치를 알려줘.

        **중요 규칙:**
        1. 좌표는 반드시 0.0~1.0 사이의 소수(float)로 반환. 예: 0.5, 0.23, 0.871
           - 절대 픽셀 좌표(예: 359, 640)나 0~1000 스케일 좌표를 사용하지 마세요.
           - 이미지 왼쪽 상단이 (0.0, 0.0), 오른쪽 하단이 (1.0, 1.0)입니다.
        2. bbox는 해당 요소를 정확히 둘러싸야 함
        3. 요소가 여러 개면 가장 중앙/명확한 것 선택
        4. 찾을 수 없으면 bbox를 null로 반환

        **반환 형식 (JSON만):**
        {{
        "found": true/false,
        "bbox": [x1, y1, x2, y2] or null,
        "description": "찾은 요소 설명",
        "confidence": 0.0~1.0
        }}

        bbox 예시: [0.12, 0.34, 0.56, 0.78] — 모든 값이 0.0~1.0 사이여야 합니다.
        """
        
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=[prompt,Image.open(image_path)],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                )
            )
            
            # 응답 파싱
            data = json.loads(response.text)
            
            if not data.get("found"):
                return VisionResult(
                    success=False,
                    description=data.get("description", "요소를 찾을 수 없음"),
                    confidence=data.get("confidence", 0.0)
                )
            
            bbox_list = data.get("bbox")
            if not bbox_list or len(bbox_list) != 4:
                raise ValueError(f"Invalid bbox format: {bbox_list}")
            
            # 좌표 정규화: x1<x2, y1<y2 보장
            bx1, by1, bx2, by2 = bbox_list
            bbox = BoundingBox(
                x1=min(bx1, bx2),
                y1=min(by1, by2),
                x2=max(bx1, bx2),
                y2=max(by1, by2)
            )
            
            result = VisionResult(
                success=True,
                bbox=bbox,
                description=data.get("description", ""),
                confidence=data.get("confidence", 0.0)
            )
            
            # 디버그 이미지 생성
            if debug_dir and bbox:
                self._draw_bbox(image_path, bbox, debug_dir)
            
            return result
            
        except Exception as e:
            logger.error(f"Vision analysis failed: {e}")
            return VisionResult(
                success=False,
                error=str(e)
            )
    @traceable 
    def analyze_screen_state(self, image_path: Path) -> Dict:
        """
        현재 화면 상태 전반 분석
        """
        prompt = """
        이 게임 화면을 분석해줘.

        screen_type은 반드시 아래 값 중 하나만 사용해:
        - "title"    : 타이틀/스플래시/로그인 화면 (게스트·Google·Apple 로그인 버튼 등)
        - "lobby"    : 메인 로비/홈 화면 (햄버거 메뉴, 전투 시작 등 주요 HUD 포함)
        - "settings" : 설정 팝업 또는 설정 화면 (진동·효과음·이용약관·계정연동 등)
        - "account"  : 계정 연동 팝업 (Google/Apple 연동·로그아웃·계정삭제 버튼 등)
        - "shop"     : 상점/구매 화면
        - "battle"   : 전투/게임플레이 화면
        - "ranking"  : 랭킹 화면
        - "unknown"  : 위 항목에 해당하지 않는 경우

        JSON 형식으로 반환:
        {
        "screen_type": "위 목록 중 하나",
        "ui_elements": ["요소1", "요소2", ...],
        "popups": ["팝업1", ...] or [],
        "suggested_actions": ["액션1", "액션2", ...]
        }
        """
        
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=[Image.open(image_path), prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json"
                )
            )
            return json.loads(response.text)
        except Exception as e:
            logger.error(f"Screen analysis failed: {e}")
            return {}
    
    @traceable
    def read_text(self, image_path: Path, region_description: str) -> Optional[str]:
        """
        화면에서 특정 영역의 텍스트 값을 읽어서 반환

        Args:
            image_path: 스크린샷 경로
            region_description: 읽을 텍스트 영역 설명 (예: "PID 값", "유저 ID 숫자")

        Returns:
            읽은 텍스트 문자열, 찾지 못하면 None
        """
        prompt = f"""
        이 게임 화면에서 '{region_description}'에 해당하는 텍스트 값을 읽어줘.

        **규칙:**
        1. 해당 영역의 텍스트만 정확히 반환한다.
        2. 찾을 수 없으면 value를 null로 반환한다.

        **반환 형식 (JSON만):**
        {{
        "found": true/false,
        "value": "읽은 텍스트" or null
        }}
        """
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=[prompt, Image.open(image_path)],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
            data = json.loads(response.text)
            if not data.get("found"):
                return None
            return data.get("value")
        except Exception as e:
            logger.error(f"read_text failed: {e}")
            return None

    def _draw_bbox(self, image_path: Path, bbox: BoundingBox,
                   debug_dir: Path, width: int = 720, height: int = 1280):
        """BBox 시각화"""
        try:
            img = Image.open(image_path)
            draw = ImageDraw.Draw(img)
            
            pixel_coords = bbox.to_pixels(width, height)
            draw.rectangle(
                [pixel_coords["x1"], pixel_coords["y1"], 
                 pixel_coords["x2"], pixel_coords["y2"]],
                outline=(255, 0, 0),
                width=3
            )
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = debug_dir / f"bbox_{timestamp}.png"
            img.save(output_path)
            logger.info(f"Debug image saved: {output_path}")
        except Exception as e:
            logger.error(f"Draw bbox failed: {e}")
# if __name__ == "__main__":
#     project = "percent-vertex-test"
#     location = "global"
#     gemini_client= genai.Client(
#             vertexai=True,
#             project=project,
#             location=location
#         )
    
#     client = wrappers.wrap_gemini(gemini_client)
#     response = client.models.generate_content(
#             model="gemini-2.5-flash",
#             contents="Why is the sky blue?",
#         )
#     print(response.text) 