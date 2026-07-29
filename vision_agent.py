from google.genai import types
from llm_client import build_genai_client
from prompts import (
    FIND_ELEMENT_PROMPT,
    FIND_ELEMENTS_PROMPT,
    ANALYZE_SCREEN_STATE_PROMPT,
    READ_TEXT_PROMPT,
    DETECT_INTERRUPT_PROMPT,
    EXTRACT_ITEMS_PROMPT,
    READ_ITEM_STATES_PROMPT,
    READ_SCREEN_BATCH_PROMPT,
)
from PIL import Image, ImageDraw
import json
import uuid
import time
from pathlib import Path
from typing import Dict, Optional, Sequence
from datetime import datetime
import logging
from pydantic import BaseModel

logger = logging.getLogger(__name__)

def _state_block(state_context: str) -> str:
    """게임이 알려준 현재 상태를 프롬프트에 끼워 넣을 블록으로 감싼다.

    Vision은 스크린샷만 보므로 씬·상태값·실제 존재하는 컴포넌트를 모른다. 이 정보를 함께
    주면 "화면에 없는 것을 비슷하게 생긴 다른 요소로 잘못 잡는" 오판을 줄일 수 있다.
    비어 있으면 프롬프트를 그대로 둔다(수집 실패해도 판정은 계속되어야 한다).
    """
    if not state_context.strip():
        return ""
    # ⚠️ "목록에 없으면 없는 것으로 판정하라"처럼 강한 지시를 넣었더니, 실제로 화면에 있는
    # 요소까지 놓쳤다(2026-07-28 A/B 확인: `Btn Aegis`가 있는데도 found=False).
    # 컴포넌트명은 내부 영문 오브젝트명이라 한국어 target 문구와 표기가 달라 그렇다.
    # 그래서 상태는 참고 정보로만 주고, 판단 주체는 이미지로 유지한다.
    return (
        "\n[참고 — 게임이 보고한 현재 상태. 화면 해석을 돕는 보조 정보이며 판단은 이미지가 우선한다]\n"
        f"{state_context.strip()}\n"
        "컴포넌트 이름은 내부 오브젝트명이라 target 문구와 표기가 다를 수 있다. 목록에 없다는\n"
        "이유만으로 화면에 분명히 보이는 요소를 놓치지 마라. 반대로 현재 씬과 무관한 요소를\n"
        "요구받았다면 비슷하게 생긴 다른 요소를 억지로 고르지 마라.\n\n"
    )



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
                 model: str = "gemini-3.1-flash-lite"):
        self.client = build_genai_client()
        self.model = model
        logger.info(f"Initialized Gemini Vision Agent: {model}")

    @staticmethod
    def _image_part(image_path: Path) -> types.Part:
        """PIL 대신 bytes로 변환하여 OpenInference 호환성 확보"""
        data = Path(image_path).read_bytes()
        return types.Part.from_bytes(data=data, mime_type="image/png")

    def find_element(
        self,
        image_path: Path,
        target_description: str,
        debug_dir: Optional[Path] = None,
        state_context: str = "",
    ) -> VisionResult:
        """
        화면에서 특정 UI 요소 찾기

        Args:
            image_path: 스크린샷 경로
            target_description: 찾을 요소 설명 (예: "스태미너 충전 아이콘")
            debug_dir: 디버그 이미지 저장 경로
        """
        prompt = FIND_ELEMENT_PROMPT.format(
            target_description=target_description,
            state_context=_state_block(state_context),
        )
        started = time.monotonic()

        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=[prompt, self._image_part(image_path)],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                )
            )

            data = json.loads(response.text)

            if not data.get("found"):
                logger.info(
                    "Vision element completed: target='%s' found=false duration=%.2fs model=%s",
                    target_description, time.monotonic() - started, self.model,
                )
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

            if debug_dir and bbox:
                self._draw_bbox(image_path, bbox, debug_dir)

            logger.info(
                "Vision element completed: target='%s' duration=%.2fs model=%s",
                target_description, time.monotonic() - started, self.model,
            )

            return result

        except Exception as e:
            logger.error(
                "Vision analysis failed: target='%s' duration=%.2fs error=%s",
                target_description, time.monotonic() - started, e,
            )
            return VisionResult(
                success=False,
                error=str(e)
            )

    def find_elements(
        self,
        image_path: Path,
        target_descriptions: Sequence[str],
        debug_dir: Optional[Path] = None,
    ) -> list[VisionResult]:
        """동일 스크린샷에서 여러 UI 요소를 VLM 1회 호출로 탐색한다."""
        targets = [str(target).strip() for target in target_descriptions]
        if not targets:
            return []
        if len(targets) == 1:
            return [self.find_element(image_path, targets[0], debug_dir)]

        targets_json = json.dumps(
            [{"index": index, "target": target} for index, target in enumerate(targets)],
            ensure_ascii=False,
            indent=2,
        )
        prompt = FIND_ELEMENTS_PROMPT.format(targets_json=targets_json)
        started = time.monotonic()

        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=[prompt, self._image_part(image_path)],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
            data = json.loads(response.text)
            raw_elements = data.get("elements", [])
            indexed = {}
            for item in raw_elements:
                if not isinstance(item, dict):
                    continue
                try:
                    index = int(item.get("index"))
                except (TypeError, ValueError):
                    continue
                indexed[index] = item
            results: list[VisionResult] = []
            missing_indexes: list[int] = []
            for index, target in enumerate(targets):
                item = indexed.get(index)
                if item is None:
                    results.append(VisionResult(
                        success=False,
                        error=f"배치 응답에 index {index} 누락",
                    ))
                    missing_indexes.append(index)
                    continue
                if not item.get("found"):
                    results.append(VisionResult(
                        success=False,
                        description=item.get("description", "요소를 찾을 수 없음"),
                        confidence=item.get("confidence", 0.0),
                    ))
                    continue
                box_2d = item.get("box_2d")
                if not isinstance(box_2d, list) or len(box_2d) != 4:
                    results.append(VisionResult(
                        success=False,
                        error=f"Invalid box_2d format for index {index}: {box_2d}",
                    ))
                    missing_indexes.append(index)
                    continue
                ymin, xmin, ymax, xmax = map(float, box_2d)
                results.append(VisionResult(
                    success=True,
                    bbox=BoundingBox(
                        x1=min(xmin, xmax) / 1000,
                        y1=min(ymin, ymax) / 1000,
                        x2=max(xmin, xmax) / 1000,
                        y2=max(ymin, ymax) / 1000,
                    ),
                    description=item.get("description", ""),
                    confidence=item.get("confidence", 0.0),
                ))

            # 불완전한 배치 응답만 개별 호출로 보완해 정확도를 유지한다.
            for index in missing_indexes:
                results[index] = self.find_element(image_path, targets[index], debug_dir)
            logger.info(
                "Batch vision completed: targets=%d fallback=%d duration=%.2fs model=%s",
                len(targets), len(missing_indexes), time.monotonic() - started, self.model,
            )
            return results
        except Exception as e:
            logger.error(
                "Batch vision analysis failed after %.2fs, falling back to individual calls: %s",
                time.monotonic() - started, e,
            )
            return [self.find_element(image_path, target, debug_dir) for target in targets]
    def analyze_screen_state(self, image_path: Path) -> Dict:
        """
        현재 화면 상태 전반 분석
        """
        prompt = ANALYZE_SCREEN_STATE_PROMPT

        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=[self._image_part(image_path), prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json"
                )
            )
            data = json.loads(response.text)
            return data
        except Exception as e:
            logger.error(f"Screen analysis failed: {e}")
            return {}
    
    def detect_interrupt(self, image_path: Path) -> Dict:
        """예상 밖 인터럽트 팝업(이벤트/공지/오류 등) 감지 및 닫기 방법 판단.

        반환: {"is_interrupt": bool, "kind": str,
               "close_method": "tap"|"tap_center"|"back"|None,
               "close_box_2d": [ymin,xmin,ymax,xmax]|None, "description": str}
        실패 시 빈 dict — 호출부는 인터럽트 아님으로 처리한다.
        """
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=[DETECT_INTERRUPT_PROMPT, self._image_part(image_path)],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
            return json.loads(response.text)
        except Exception as e:
            logger.error(f"Interrupt detection failed: {e}")
            return {}

    def extract_items(self, image_path: Path, items_description: str) -> list:
        """화면에 보이는 목록 항목들 추출 (예: 빌드 버전 목록).

        반환: [{"text": str, "info": str}, ...] — 실패 시 빈 리스트
        """
        prompt = EXTRACT_ITEMS_PROMPT.format(items_description=items_description)
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=[prompt, self._image_part(image_path)],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
            data = json.loads(response.text)
            items = data.get("items", [])
            return [i for i in items if isinstance(i, dict) and i.get("text")]
        except Exception as e:
            logger.error(f"extract_items failed: {e}")
            return []

    def read_item_states(self, image_path: Path, items_description: str) -> list:
        """화면의 아이템 목록 + 보유 여부를 함께 추출 (인벤토리/도감 탭 스냅샷용).

        반환: [{"name": str, "owned": bool, "info": str}, ...] — 실패 시 빈 리스트
        """
        prompt = READ_ITEM_STATES_PROMPT.format(items_description=items_description)
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=[prompt, self._image_part(image_path)],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
            data = json.loads(response.text)
            items = data.get("items", [])
            return [i for i in items if isinstance(i, dict) and i.get("name")]
        except Exception as e:
            logger.error(f"read_item_states failed: {e}")
            return []

    def read_screen_batch(self, image_path: Path, items: list,
                          state_context: str = "") -> list:
        """한 화면에 같이 보이는 여러 항목(재화 값 여러 개 + 조건부 존재 확인 등)을
        vision 호출 1번으로 모아서 확인 — 매번 따로 부르지 않고 라운드트립을 줄인다.

        items: [{"name": str, "description": str}, ...] — description은 값을 읽을
        영역이거나("다이아 수량") 존재 조건("검귀 카드 — 다이아/자물쇠 아이콘 없음")이다.
        반환: [{"name": str, "found": bool, "value": str|None}, ...] — 실패 시 빈 리스트
        """
        items_block = "\n".join(
            f"{i + 1}. name=\"{it['name']}\" — {it['description']}"
            for i, it in enumerate(items)
        )
        prompt = READ_SCREEN_BATCH_PROMPT.format(
            items_block=items_block,
            state_context=_state_block(state_context),
        )
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=[prompt, self._image_part(image_path)],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
            data = json.loads(response.text)
            results = data.get("items", [])
            return [r for r in results if isinstance(r, dict) and r.get("name")]
        except Exception as e:
            logger.error(f"read_screen_batch failed: {e}")
            return []

    def read_text(self, image_path: Path, region_description: str,
                  state_context: str = "") -> Optional[str]:
        """
        화면에서 특정 영역의 텍스트 값을 읽어서 반환

        Args:
            image_path: 스크린샷 경로
            region_description: 읽을 텍스트 영역 설명 (예: "PID 값", "유저 ID 숫자")

        Returns:
            읽은 텍스트 문자열, 찾지 못하면 None
        """
        prompt = READ_TEXT_PROMPT.format(
            region_description=region_description,
            state_context=_state_block(state_context),
        )

        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=[prompt, self._image_part(image_path)],
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

            # 병렬 세션·같은 초 내 중복 호출에도 파일명이 겹치지 않도록 ms + 랜덤 suffix
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            output_path = debug_dir / f"bbox_{timestamp}_{uuid.uuid4().hex[:6]}.png"
            img.save(output_path)
            logger.info(f"Debug image saved: {output_path}")
            return output_path
        except Exception as e:
            logger.error(f"Draw bbox failed: {e}")
            return None
        
# if __name__ == "__main__":
    # project = "percent-vertex-test"
    # location = "global"
    # gemini_client= genai.Client(
    #         vertexai=True,
    #         project=project,
    #         location=location
    #     )
    
    # client = wrappers.wrap_gemini(gemini_client)
    # response = client.models.generate_content(
    #         model="gemini-2.5-flash",
    #         contents="Why is the sky blue?",
    #     )
    # print(response.text) 
