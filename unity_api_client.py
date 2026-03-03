import logging
import os
import re
import json
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

import requests
from google import genai
from google.genai import types

from adb_controller import ADBController


logger = logging.getLogger(__name__)

TARGET_HINTS_MAP = {
    "햄버거": ["hamburger", "hamberger", "menu", "top right"],
    "메뉴": ["menu", "hamburger", "hamberger"],
    "설정": ["setting", "popup", "option"],
    "이용약관": ["terms", "terms of use", "service", "policy"],
    "약관": ["terms", "terms of use", "service"],
    "개인정보": ["privacy", "policy"],
    "계정": ["account"],
    "언어": ["language"],
    "랭킹": ["ranking", "rank"],
    "친구": ["friend", "social"],
    "퀘스트": ["quest"],
    "플레이": ["play", "start"],
    "솔로": ["solo"],
    "상점": ["shop", "store"],
    "전투": ["fight", "battle"],
    "유닛": ["unit", "character"],
    "뽑기": ["draw", "gacha", "summon"],
    "진동": ["vibration", "vibrate"],
    "효과음": ["fx", "sound", "effect"],
    "데미지": ["damage", "font"],
    "공지": ["notice"],
    "리롤": ["reroll", "re roll"],
    "합성": ["merge"],
    "on": ["on", "enabled", "true"],
    "off": ["off", "disabled", "false"],
}


@dataclass
class UnityMatch:
    button: Dict[str, Any]
    score: float


class UnityAPIClient:
    """Client for Unity QA helper API."""

    def __init__(
        self,
        adb_controller: Optional[ADBController] = None,
        project: Optional[str] = None,
        location: str = "global",
        model: str = "gemini-3-pro-preview",
        temperature: float = 0.1,
        base_url: Optional[str] = None,
        timeout_sec: float = 5.0,
    ):
        url = (
            base_url
            or os.getenv("UNITY_API_URL")
            or os.getenv("MCP_SERVER_URL")
            or "http://localhost:37772"
        )
        self.adb = adb_controller
        self.base_url = url.rstrip("/")
        self.timeout_sec = timeout_sec
        self.project = project or os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT")
        self.location = location
        self.model = model or os.getenv("UNITY_LLM_MODEL") or "gemini-3-pro-preview"
        self.temperature = temperature
        self.client = None

        if self.project:
            try:
                self.client = genai.Client(
                    vertexai=True,
                    project=self.project,
                    location=self.location,
                )
            except Exception as exc:
                logger.warning("Failed to initialize Gemini client for Unity selection: %s", exc)

    @staticmethod
    def _normalize_text(value: str) -> str:
        value = value.strip().lower()
        value = re.sub(r"[\s\-_]+", " ", value)
        value = re.sub(r"[^\w\s가-힣]", "", value)
        return value

    @staticmethod
    def _button_search_fields(button: Dict[str, Any]) -> List[str]:
        fields = [
            button.get("SpecifiedName"),
            button.get("GameObjectName"),
            button.get("Text"),
            button.get("Name"),
            button.get("ParentMetadata"),
        ]
        return [f for f in fields if isinstance(f, str) and f.strip()]

    @staticmethod
    def _split_identifier_tokens(value: str) -> List[str]:
        if not value:
            return []
        expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
        expanded = expanded.replace("_", " ").replace("-", " ")
        tokens = [token.strip().lower() for token in expanded.split() if token.strip()]
        return tokens

    def _build_target_hints(self, target: str) -> List[str]:
        normalized_target = self._normalize_text(target)
        hints: List[str] = []
        for key, values in TARGET_HINTS_MAP.items():
            if key in normalized_target:
                hints.extend(values)

        if "켜" in target or "활성" in target:
            hints.extend(["on", "enabled", "true"])
        if "꺼" in target or "비활성" in target:
            hints.extend(["off", "disabled", "false"])

        seen = set()
        ordered = []
        for hint in hints:
            if hint not in seen:
                seen.add(hint)
                ordered.append(hint)
        return ordered

    def _filter_buttons(self, buttons: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        filtered: List[Dict[str, Any]] = []
        for button in buttons:
            x = button.get("PositionX")
            y = button.get("PositionY")
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                continue
            if y < 0:
                continue
            if self.adb and not self.unity_to_screen_coords(button, self.adb.width, self.adb.height):
                continue
            filtered.append(button)
        return filtered

    def _score(self, target: str, button: Dict[str, Any]) -> float:
        fields = self._button_search_fields(button)
        if not fields:
            return 0.0

        # Regex target support when caller provides pattern-like target.
        try:
            rx = re.compile(target, re.IGNORECASE)
            if any(rx.search(field) for field in fields):
                return 1.0
        except re.error:
            pass

        normalized_target = self._normalize_text(target)
        if not normalized_target:
            return 0.0

        best = 0.0
        for field in fields:
            normalized_field = self._normalize_text(field)
            if not normalized_field:
                continue
            if normalized_target in normalized_field:
                best = max(best, 0.95)
            best = max(
                best,
                SequenceMatcher(None, normalized_target, normalized_field).ratio(),
            )
        return best

    def _fetch_buttons(self) -> List[Dict[str, Any]]:
        endpoint = f"{self.base_url}/api/findAllButtons"

        if self.adb:
            # Unity API is exposed from the device; keep adb forward in place before querying.
            self.adb.ensure_forward(local_port=37772, remote_port=37772)

        try:
            response = requests.get(endpoint, timeout=self.timeout_sec)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                logger.warning("Unity API returned non-list payload: %s", type(payload))
                return []
            return [item for item in payload if isinstance(item, dict)]
        except requests.RequestException as exc:
            logger.warning("Unity API request failed: %s", exc)
            return []
        except ValueError as exc:
            logger.warning("Unity API JSON parse failed: %s", exc)
            return []

    def _build_llm_candidates(self, buttons: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        candidates = []
        for index, button in enumerate(buttons):
            name_tokens = self._split_identifier_tokens(button.get("GameObjectName") or "")
            specified_tokens = self._split_identifier_tokens(button.get("SpecifiedName") or "")
            path_tokens = self._split_identifier_tokens(button.get("ParentMetadata") or "")
            candidates.append(
                {
                    "index": index,
                    "GameObjectName": button.get("GameObjectName"),
                    "SpecifiedName": button.get("SpecifiedName"),
                    "Text": button.get("Text"),
                    "PositionX": button.get("PositionX"),
                    "PositionY": button.get("PositionY"),
                    "ParentMetadata": button.get("ParentMetadata"),
                    "NameTokens": sorted(set(name_tokens + specified_tokens)),
                    "PathTokens": sorted(set(path_tokens)),
                }
            )
        return candidates

    def _choose_with_llm(
        self,
        target: str,
        buttons: List[Dict[str, Any]],
    ) -> Optional[UnityMatch]:
        if not self.client or not buttons:
            return None

        candidates = self._build_llm_candidates(buttons)
        target_hints = self._build_target_hints(target)
        prompt = f"""
당신은 모바일 QA 자동화용 Unity 버튼 선택기다.
사용자 target: "{target}"
target hints: {json.dumps(target_hints, ensure_ascii=False)}

아래 `findAllButtons` 결과 후보 중에서 target과 가장 잘 맞는 버튼 하나를 고르라.

판단 규칙:
- 한국어 target과 영어 GameObjectName의 의미적 매칭을 고려한다.
- ParentMetadata의 계층 구조를 강하게 활용한다.
- On/Off 같은 일반 이름은 상위 부모 문맥이 target과 맞을 때만 선택한다.
- 설정/약관/계정/랭킹/햄버거 메뉴처럼 의미가 분명한 버튼을 우선 선택한다.
- 철자 변형이나 오타를 허용한다. 예: "Hamberger"는 "Hamburger"와 같은 의미로 본다.
- CamelCase 이름을 단어로 분리해서 해석한다. 예: "Button_TermsOfUse" -> "button", "terms", "of", "use".
- target이 토글 성격이면 On/Off 이름 자체보다 ParentMetadata 안의 기능명(Vibration, FX, DamageFont 등)을 더 중요하게 본다.
- 컨테이너나 장식용 오브젝트가 아니라 실제 클릭 대상 버튼을 선택한다.
- 적절한 후보가 없으면 index를 null로 반환한다.

예시:
- target="햄버거 메뉴" -> "HambergerButton" 같은 이름이 가장 유력하다.
- target="이용약관" -> "Button_TermsOfUse" 같은 이름이 가장 유력하다.
- target="계정 버튼" -> "Button_Account" 같은 이름이 가장 유력하다.
- target="진동 OFF" -> 이름이 단순히 Off 여도 ParentMetadata 에 Vibration 이 있으면 그 후보가 유력하다.

반드시 JSON만 반환:
{{
  "index": 0,
  "confidence": 0.0,
  "reason": "짧은 한국어 설명",
  "matched_field": "GameObjectName | SpecifiedName | Text | ParentMetadata"
}}

후보:
{json.dumps(candidates, ensure_ascii=False)}
""".strip()

        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=self.temperature,
                ),
            )
            payload = json.loads(response.text)
            index = payload.get("index")
            if index is None:
                return None
            if not isinstance(index, int) or index < 0 or index >= len(buttons):
                logger.warning("Gemini returned invalid Unity candidate index: %s", index)
                return None

            confidence = payload.get("confidence", 1.0)
            if not isinstance(confidence, (int, float)):
                confidence = 1.0

            reason = payload.get("reason", "")
            logger.info(
                "Gemini selected Unity button for '%s': index=%s confidence=%.2f reason=%s",
                target,
                index,
                float(confidence),
                reason,
            )
            return UnityMatch(button=buttons[index], score=float(confidence))
        except Exception as exc:
            logger.warning("Gemini Unity button selection failed: %s", exc)
            return None

    def find_best_button(self, target: str, min_score: float = 0.55) -> Optional[UnityMatch]:
        if not target or not target.strip():
            return None

        buttons = self._filter_buttons(self._fetch_buttons())
        llm_match = self._choose_with_llm(target, buttons)
        if llm_match:
            return llm_match

        best_button: Optional[Dict[str, Any]] = None
        best_score = -1.0

        for button in buttons:
            score = self._score(target, button)
            if score > best_score:
                best_button = button
                best_score = score

        if not best_button or best_score < min_score:
            return None
        return UnityMatch(button=best_button, score=best_score)

    def verify_element(self, target: str, min_score: float = 0.55) -> bool:
        return self.find_best_button(target=target, min_score=min_score) is not None

    @staticmethod
    def unity_to_screen_coords(
        button: Dict[str, Any],
        screen_width: int,
        screen_height: int,
    ) -> Optional[Dict[str, int]]:
        x = button.get("PositionX")
        y = button.get("PositionY")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            return None

        # Unity uses bottom-left origin. ADB uses top-left origin.
        screen_x = int(round(float(x)))
        screen_y = int(round(float(screen_height - y)))

        if screen_x < 0 or screen_x > screen_width:
            return None
        if screen_y < 0 or screen_y > screen_height:
            return None

        return {"x": screen_x, "y": screen_y}
