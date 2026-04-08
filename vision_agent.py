from google.genai import types
from google import genai
from llm_client import build_genai_client
from PIL import Image, ImageDraw
import base64
import json
from pathlib import Path
from typing import Dict, Optional, List
from datetime import datetime
import logging
from pydantic import BaseModel
from langfuse import get_client
from langfuse.media import LangfuseMedia

langfuse = get_client()

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
    
    def __init__(self, project: str = "", location: str = "global",
                 model: str = "gemini-2.0-flash-exp"):
        self.client = build_genai_client()
        self.model = model
        logger.info(f"Initialized Gemini Vision Agent: {model}")

    @staticmethod
    def _image_part(image_path: Path) -> types.Part:
        """PIL 대신 bytes로 변환하여 OpenInference 호환성 확보"""
        data = Path(image_path).read_bytes()
        return types.Part.from_bytes(data=data, mime_type="image/png")

    @staticmethod
    def _image_b64_url(image_path: Path) -> str:
        """Langfuse에서 이미지 렌더링용 base64 data URL 생성"""
        data = Path(image_path).read_bytes()
        b64 = base64.b64encode(data).decode()
        return f"data:image/png;base64,{b64}"

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
        prompt_client = langfuse.get_prompt("find_element", label="production")
        prompt = prompt_client.compile(target_description=target_description)

        try:
            with langfuse.start_as_current_observation(
                as_type="span",
                name="find_element",
                input={"target": target_description, "prompt": prompt},
                prompt=prompt_client,
            ) as span:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=[prompt, self._image_part(image_path)],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                    )
                )

                # 응답 파싱
                data = json.loads(response.text)

                if not data.get("found"):
                    span.update(output=data)
                    return VisionResult(
                        success=False,
                        description=data.get("description", "요소를 찾을 수 없음"),
                        confidence=data.get("confidence", 0.0)
                    )

                box_2d = data.get("box_2d")
                if not box_2d or len(box_2d) != 4:
                    raise ValueError(f"Invalid box_2d format: {box_2d}")

                # Gemini 공식 형식: [ymin, xmin, ymax, xmax] 0-1000 → 0-1 변환
                ymin, xmin, ymax, xmax = box_2d
                bbox = BoundingBox(
                    x1=min(xmin, xmax) / 1000,
                    y1=min(ymin, ymax) / 1000,
                    x2=max(xmin, xmax) / 1000,
                    y2=max(ymin, ymax) / 1000,
                )

                result = VisionResult(
                    success=True,
                    bbox=bbox,
                    description=data.get("description", ""),
                    confidence=data.get("confidence", 0.0)
                )

                # 디버그 이미지 생성 & bbox 이미지를 output에 포함
                bbox_debug_path = None
                if debug_dir and bbox:
                    bbox_debug_path = self._draw_bbox(image_path, bbox, debug_dir)

                if bbox_debug_path:
                    bbox_media = LangfuseMedia(
                        content_type="image/png",
                        content_bytes=Path(bbox_debug_path).read_bytes(),
                    )
                    span.update(output={
                        "result": data,
                        "bbox_image": bbox_media,
                    })
                else:
                    span.update(output=data)

                return result

        except Exception as e:
            logger.error(f"Vision analysis failed: {e}")
            return VisionResult(
                success=False,
                error=str(e)
            )
    def analyze_screen_state(self, image_path: Path) -> Dict:
        """
        현재 화면 상태 전반 분석
        """
        prompt_client = langfuse.get_prompt("analyze_screen_state", label="production")
        prompt = prompt_client.compile()

        try:
            with langfuse.start_as_current_observation(
                as_type="span",
                name="analyze_screen_state",
                input={"prompt": prompt},
                prompt=prompt_client,
            ) as span:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=[self._image_part(image_path), prompt],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json"
                    )
                )
                data = json.loads(response.text)
                span.update(output=data)
            return data
        except Exception as e:
            logger.error(f"Screen analysis failed: {e}")
            return {}
    
    def read_text(self, image_path: Path, region_description: str) -> Optional[str]:
        """
        화면에서 특정 영역의 텍스트 값을 읽어서 반환

        Args:
            image_path: 스크린샷 경로
            region_description: 읽을 텍스트 영역 설명 (예: "PID 값", "유저 ID 숫자")

        Returns:
            읽은 텍스트 문자열, 찾지 못하면 None
        """
        prompt_client = langfuse.get_prompt("read_text", label="production")
        prompt = prompt_client.compile(region_description=region_description)

        try:
            with langfuse.start_as_current_observation(
                as_type="span",
                name="read_text",
                input={"region": region_description, "prompt": prompt},
                prompt=prompt_client,
            ) as span:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=[prompt, self._image_part(image_path)],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                    ),
                )
                data = json.loads(response.text)
                span.update(output=data)
            if not data.get("found"):
                return None
            return data.get("value")
        except Exception as e:
            logger.error(f"read_text failed: {e}")
            return None

    def _draw_bbox(self, image_path: Path, bbox: BoundingBox,
                   debug_dir: Path) -> Optional[Path]:
        """BBox 시각화. 성공 시 저장 경로 반환."""
        try:
            img = Image.open(image_path)
            width, height = img.size
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
            return output_path
        except Exception as e:
            logger.error(f"Draw bbox failed: {e}")
            return None
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