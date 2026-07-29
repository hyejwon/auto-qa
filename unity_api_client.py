import logging
import os
import re
import json
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse

import requests
from google import genai
from google.genai import types
from llm_client import build_genai_client

from adb_controller import ADBController
from langfuse_disabled import get_client

langfuse = get_client()

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


# 패키지별 치트 정의.
# - v2 빌드(/api/v2/cheats): {"id": "<cheat id>", "args": {...}}
# - 레거시 빌드(/api/sr/call): {"category": "...", "name": "..."}
GAME_CHEAT_MAP: Dict[str, Dict[str, Dict[str, Any]]] = {
    "com.supermagic.aos.aegisdefense": {
        # 이지스 디펜스는 레거시 /api/sr/call이 404다(2026-07-27 실기기 확인).
        # v2 치트 목록의 "인게임/스테이지 > 인게임 튜토리얼 스킵"을 쓴다.
        #
        # 이지스 치트는 씬 단위로 등록된다. 아웃게임(로비)에서는 ingame.* 치트가
        # 목록에 아예 없어 not_found가 떨어지므로, 이 경우는 스킵할 인게임
        # 튜토리얼이 없는 상태로 보고 성공 처리한다(not_found_ok).
        "skip_tutorial": {
            "id": "ingame.tutorial.skip",
            "args": {},
            "not_found_ok": True,
        },
    },
    "com.percent.aos.cooptd": {
        "skip_tutorial": {
            "category": "Tutorual_[서버]",
            "name": "튜토리얼/ 훈련소 클리어",
        },
    },
    "com.percent.aos.rollinghero": {
        "skip_tutorial": {
            "category": "인게임/0. 빠른 디버깅",
            "name": "현재 튜토 즉시 종료 및 모든 튜토 완료 처리",
        },
    },
    "com.percent.aos.luckydefense": {
        "skip_tutorial": {
            "category": "",
            "name": "",
        },
    },
    "com.percent.aos.arenago2": {
        "skip_tutorial": {
            "category": "튜토리얼",
            "name": "튜토리얼 스킵",
        },
    },
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
        timeout_sec: float = 10.0,
    ):
        explicit_url = base_url or os.getenv("UNITY_API_URL") or os.getenv("MCP_SERVER_URL")
        # 명시적 URL이 없고 adb가 있으면 디바이스별 동적 포워딩 포트를 쓴다.
        self._explicit_base = bool(explicit_url)
        url = explicit_url or "http://127.0.0.1:37772"
        self.adb = adb_controller
        self.base_url = url.rstrip("/")
        self.timeout_sec = timeout_sec
        self.project = project or os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT")
        self.location = location
        self.model = model or os.getenv("UNITY_LLM_MODEL") or "gemini-3-pro-preview"
        self.temperature = temperature
        self.client = None
        self._client_init_attempted = False

        parsed = urlparse(self.base_url)
        self._local_port: int = parsed.port or 37772

    def _ensure_llm_client(self):
        """Initialize the semantic selector only when the legacy fuzzy path needs it."""
        if self.client is not None or self._client_init_attempted:
            return self.client
        self._client_init_attempted = True
        try:
            self.client = build_genai_client()
        except Exception as exc:
            logger.warning(
                "Failed to initialize Gemini client for Unity selection: %s",
                exc,
            )
        return self.client

    def _resolve_base(self) -> str:
        """요청에 사용할 base URL 결정 + adb forward 보장.

        명시적 URL(UNITY_API_URL 등)이 없으면 디바이스별 동적 포트를 할당받아
        같은 호스트에서 여러 디바이스가 병렬로 돌아도 포워딩이 충돌하지 않게 한다.
        """
        if self.adb and not self._explicit_base:
            port = self.adb.forward_port(37772)
            if port:
                return f"http://127.0.0.1:{port}"
            # forward_port 실패 시 여기서 바로 return self.base_url로 빠지면 포워딩이
            # 전혀 없을 수도, 다른 기기가 남긴 37772 stale 포워딩을 그대로 잘못 타서
            # 엉뚱한 기기에 치트가 쏠릴 수도 있다(2026-07-23 확인) — 고정 포트로라도
            # 이 기기 앞으로 포워딩을 다시 강제해준다.
            self.adb.ensure_forward(local_port=37772, remote_port=37772)
            return self.base_url
        if self.adb and self._explicit_base:
            self.adb.ensure_forward(local_port=self._local_port, remote_port=37772)
        return self.base_url

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

    def _expanded_button_search_fields(self, button: Dict[str, Any]) -> List[tuple[str, float, bool]]:
        weighted_fields: List[tuple[str, float, bool]] = []
        field_specs = [
            ("SpecifiedName", 1.0, True),
            ("GameObjectName", 1.0, True),
            ("Text", 0.95, True),
            ("Name", 0.95, True),
            ("ParentMetadata", 0.65, False),
        ]
        for key, weight, is_primary in field_specs:
            value = button.get(key)
            if not isinstance(value, str) or not value.strip():
                continue

            weighted_fields.append((value, weight, is_primary))
            tokens = self._split_identifier_tokens(value)
            tokenized_value = " ".join(tokens)
            if tokenized_value and tokenized_value.lower() != value.strip().lower():
                weighted_fields.append((tokenized_value, weight, is_primary))
        return weighted_fields

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
        fields = self._expanded_button_search_fields(button)
        if not fields:
            return 0.0

        # Regex target support when caller provides pattern-like target.
        try:
            rx = re.compile(target, re.IGNORECASE)
            if any(rx.search(field) for field, _, _ in fields):
                return 1.0
        except re.error:
            pass

        normalized_target = self._normalize_text(target)
        if not normalized_target:
            return 0.0

        target_hints = [
            self._normalize_text(hint)
            for hint in self._build_target_hints(target)
            if isinstance(hint, str) and hint.strip()
        ]
        hint_tokens = {
            token
            for hint in target_hints
            for token in hint.split()
            if token
        }
        toggle_tokens = {"on", "off", "enabled", "disabled", "true", "false", "toggle", "switch", "button"}
        non_toggle_hint_tokens = hint_tokens - {"on", "off", "enabled", "disabled", "true", "false"}

        best_primary = 0.0
        best_context = 0.0
        for field, weight, is_primary in fields:
            normalized_field = self._normalize_text(field)
            if not normalized_field:
                continue
            field_score = 0.0
            if normalized_target in normalized_field:
                field_score = max(field_score, 0.95)
            field_score = max(
                field_score,
                SequenceMatcher(None, normalized_target, normalized_field).ratio(),
            )

            if any(hint == normalized_field for hint in target_hints):
                field_score = max(field_score, 0.92)
            if any(hint in normalized_field for hint in target_hints):
                field_score = max(field_score, 0.80)

            field_tokens = set(normalized_field.split())
            if hint_tokens and field_tokens:
                overlap = len(hint_tokens & field_tokens) / len(hint_tokens)
                if overlap > 0:
                    field_score = max(field_score, 0.55 + (0.35 * overlap))

            if (
                is_primary
                and non_toggle_hint_tokens
                and field_tokens
                and field_tokens <= toggle_tokens
                and field_tokens & {"on", "off", "enabled", "disabled", "true", "false"}
            ):
                field_score = min(field_score, 0.50)

            weighted_score = min(1.0, field_score) * weight
            if is_primary:
                best_primary = max(best_primary, weighted_score)
            else:
                best_context = max(best_context, weighted_score)

        best = max(best_primary, best_context)
        if best_primary > 0 and best_context > 0:
            best = max(best, min(1.0, best_primary + (best_context * 0.25)))
        return best

    def _candidate_bases(self) -> List[str]:
        """치트 요청에 시도할 base URL 목록."""
        resolved_base = self._resolve_base()

        if self.adb and not self._explicit_base and resolved_base != self.base_url:
            # 동적 포워딩 성공 — 고정 37772 후보는 다른 디바이스의 포워딩으로
            # 요청이 새어 나갈 수 있으므로 할당된 포트만 사용한다.
            return [resolved_base]

        candidate_bases: List[str] = []
        for base in (
            resolved_base,
            self.base_url,
            os.getenv("UNITY_API_URL"),
            os.getenv("MCP_SERVER_URL"),
            "http://localhost:37772",
            "http://127.0.0.1:37772",
        ):
            if not isinstance(base, str) or not base.strip():
                continue
            normalized = base.rstrip("/")
            if normalized not in candidate_bases:
                candidate_bases.append(normalized)
        return candidate_bases

    def call_cheat(self, category: str, name: str) -> bool:
        """레거시 SR 치트 API 호출. Unity 서버가 응답 없이 연결을 끊는 경우도 성공으로 처리."""
        # category에 슬래시(/)가 포함될 수 있으므로 safe='/'로 유지
        encoded_query = f"category={quote(category, safe='/')}&name={quote(name, safe='/')}"
        candidate_bases = self._candidate_bases()

        for base in candidate_bases:
            endpoint = f"{base}/api/sr/call?{encoded_query}"
            # curl command in user workflow uses POST first.
            for method in ("POST", "GET"):
                try:
                    if method == "POST":
                        response = requests.post(endpoint, data=b"", timeout=self.timeout_sec)
                    else:
                        response = requests.get(endpoint, timeout=self.timeout_sec)
                    response.raise_for_status()
                    logger.info(
                        "Cheat called (%s): [%s] %s via %s → %s",
                        method,
                        category,
                        name,
                        base,
                        response.text[:200],
                    )
                    return True
                except requests.exceptions.ConnectionError as exc:
                    # Unity 서버가 치트 실행 후 응답 없이 연결을 끊는 경우 → 성공으로 간주
                    cause = str(exc)
                    if "RemoteDisconnected" in cause or "Connection aborted" in cause:
                        logger.info(
                            "Cheat called (%s): [%s] %s via %s — no HTTP response (assumed success)",
                            method,
                            category,
                            name,
                            base,
                        )
                        return True
                    logger.warning("Cheat %s connection error via %s: %s", method, base, exc)
                except requests.exceptions.ReadTimeout:
                    # Unity 서버가 치트 실행 후 응답을 보내지 않아 타임아웃 → 성공으로 간주
                    logger.info(
                        "Cheat called (%s): [%s] %s via %s — read timeout (assumed success)",
                        method,
                        category,
                        name,
                        base,
                    )
                    return True
                except requests.RequestException as exc:
                    logger.warning("Cheat %s failed via %s: %s", method, base, exc)

        logger.error(
            "Cheat call failed [%s] %s after endpoint fallbacks: %s",
            category,
            name,
            candidate_bases,
        )
        return False

    def list_cheats_v2(self) -> List[Dict[str, Any]]:
        """v2 치트 목록 조회. 미지원 빌드면 빈 리스트."""
        for base in self._candidate_bases():
            try:
                response = requests.get(f"{base}/api/v2/cheats", timeout=self.timeout_sec)
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                logger.warning("v2 cheat list failed via %s: %s", base, exc)
                continue

            if not isinstance(payload, dict) or not payload.get("success"):
                continue
            cheats = (payload.get("data") or {}).get("Cheats")
            if isinstance(cheats, list):
                return [item for item in cheats if isinstance(item, dict)]
        return []

    def list_properties_v2(self) -> List[Dict[str, Any]]:
        """v2 프로퍼티 '정의' 목록 조회 (현재값이 아니라 스펙). 미지원 빌드면 빈 리스트."""
        for base in self._candidate_bases():
            try:
                response = requests.get(
                    f"{base}/api/v2/cheats/property", timeout=self.timeout_sec
                )
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                logger.warning("v2 property list failed via %s: %s", base, exc)
                continue

            if not isinstance(payload, dict) or not payload.get("success"):
                continue
            props = (payload.get("data") or {}).get("Properties")
            if isinstance(props, list):
                return [item for item in props if isinstance(item, dict)]
        return []

    def current_scene_prefixes_v2(self) -> List[str]:
        """현재 씬에 등록된 치트·프로퍼티 id의 최상위 접두사 목록.

        치트/프로퍼티는 씬 단위로 등록되므로, 목록에 어떤 접두사가 잡히는지만 봐도
        지금 어느 씬인지 알 수 있다 — 이지스 디펜스 기준 전투 화면이면 `ingame`,
        로비면 `outgame`이 나온다(2026-07-27 실기기 확인).

        화면을 Vision으로 판독하지 않고 v2 정의 목록으로 씬을 판정할 수 있어, "지금
        로비인가 전투인가"를 묻는 verify를 대체한다. 접두사 이름은 게임마다 다를 수
        있으므로 판정 대상 문자열은 호출부(템플릿)가 지정한다.
        """
        prefixes: List[str] = []
        definitions = self.list_cheats_v2() + self.list_properties_v2()
        for item in definitions:
            item_id = str(item.get("Id") or "")
            prefix = item_id.split(".", 1)[0] if "." in item_id else ""
            if prefix and prefix not in prefixes:
                prefixes.append(prefix)
        return prefixes

    def state_context_text(self, max_components: int = 22) -> str:
        """현재 게임 상태를 Vision 프롬프트에 넣을 짧은 텍스트로 직렬화한다.

        Vision은 픽셀만 보기 때문에 "지금 어느 화면인가", "이 화면에 어떤 컴포넌트가
        실제로 있는가"를 모른다. 그래서 아웃게임 네비게이션의 `Btn Aegis`를 인게임의
        "이지스 소환 버튼"으로 오인하는 식의 사고가 난다(2026-07-28 실기기에서 발생).
        게임이 자기 상태를 알려주므로, 그 사실을 프롬프트에 함께 넣어 오판을 줄인다.

        수집 실패는 조용히 빈 문자열을 반환한다 — 이 정보가 없다고 검증이 막히면 안 된다.
        """
        try:
            lines: List[str] = []

            scenes = [p for p in self.current_scene_prefixes_v2() if p not in ("debug",)]
            if scenes:
                lines.append(f"- 현재 씬: {', '.join(scenes)}")

            values = self.get_property_values_v2()
            shown: List[str] = []
            for prop_id, item in values.items():
                if item.get("Error"):
                    continue
                display = item.get("Display")
                if not isinstance(display, str) or not display.strip():
                    display = str(item.get("Value"))
                if len(display) > 40:
                    display = display[:40] + "…"
                shown.append(f"{prop_id}={display}")
            if shown:
                lines.append("- 게임 상태값: " + " | ".join(shown))

            names: List[str] = []
            for button in self._fetch_buttons():
                name = str(button.get("GameObjectName") or "").strip()
                if not name or name in names:
                    continue
                names.append(name)
                if len(names) >= max_components:
                    break
            if names:
                lines.append(
                    "- 이 화면에 실제로 존재하는 UI 컴포넌트: " + ", ".join(names)
                )

            if not lines:
                return ""
            return "\n".join(lines)
        except Exception as exc:  # 상태 수집 실패가 검증을 막지 않도록
            logger.debug("state_context 수집 실패: %s", exc)
            return ""

    def catalog_snapshot_v2(self) -> Dict[str, List[Dict[str, Any]]]:
        """현재 씬에 등록된 치트/프로퍼티 정의 스냅샷.

        ⚠️ 치트·프로퍼티는 씬 단위로 등록된다(인게임 전투 화면에서는 ingame.*,
        로비에서는 outgame.*만 잡힌다 — 2026-07-27 이지스 디펜스 확인). 한 번의 스냅샷은
        '지금 이 씬에 있는 것'만 담기므로, 여러 씬에서 찍어 누적 병합해야 전체 카탈로그가 된다.
        """
        return {
            "cheats": self.list_cheats_v2(),
            "properties": self.list_properties_v2(),
        }

    def get_property_values_v2(
        self, ids: Optional[List[str]] = None
    ) -> Dict[str, Dict[str, Any]]:
        """v2 프로퍼티 현재값 조회 → {id: {"Value", "Display", "Error"}}.

        ids를 주면 해당 항목만, 없으면 현재 씬에 등록된 전체를 가져온다.
        getter가 실패한 항목은 요청 전체가 실패하지 않고 Error에 원인이 담긴다.
        """
        query = ""
        if ids:
            query = "?ids=" + quote(",".join(ids), safe=",")

        for base in self._candidate_bases():
            endpoint = f"{base}/api/v2/cheats/property/values{query}"
            try:
                response = requests.get(endpoint, timeout=self.timeout_sec)
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                logger.warning("v2 property values failed via %s: %s", base, exc)
                continue

            if not isinstance(payload, dict) or not payload.get("success"):
                continue
            props = (payload.get("data") or {}).get("Properties")
            if not isinstance(props, list):
                continue
            return {
                str(item.get("Id")): item
                for item in props
                if isinstance(item, dict) and item.get("Id")
            }
        return {}

    def set_property_v2(self, prop_id: str, value: Any) -> Optional[Dict[str, Any]]:
        """v2 프로퍼티 쓰기. 성공 시 적용된 값({"Value","Display"})을, 실패 시 None을 반환.

        쓰기 불가 항목은 errorCode "read_only", 타입/범위 오류는 "invalid_value"다.
        """
        body = {"Id": prop_id, "Value": value}
        for base in self._candidate_bases():
            endpoint = f"{base}/api/v2/cheats/property/set"
            try:
                response = requests.post(endpoint, json=body, timeout=self.timeout_sec)
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                logger.warning("v2 property set failed via %s: %s", base, exc)
                continue

            if not isinstance(payload, dict) or not payload.get("success"):
                logger.error(
                    "v2 property set rejected: %s = %r errorCode=%s message=%s",
                    prop_id,
                    value,
                    payload.get("errorCode") if isinstance(payload, dict) else None,
                    payload.get("message") if isinstance(payload, dict) else None,
                )
                return None

            data = payload.get("data") or {}
            logger.info(
                "v2 property set: %s = %r → Value=%r Display=%r",
                prop_id,
                value,
                data.get("Value"),
                data.get("Display"),
            )
            return data

        logger.error("v2 property set failed [%s] after endpoint fallbacks", prop_id)
        return None

    def execute_cheat_v2(
        self,
        cheat_id: str,
        args: Optional[Dict[str, Any]] = None,
        not_found_ok: bool = False,
    ) -> bool:
        """v2 치트 실행. 응답 없이 끊기거나 타임아웃되는 경우도 성공으로 처리.

        not_found_ok=True면 현재 씬에 치트가 등록돼 있지 않은 경우(not_found)도
        "적용할 대상이 없음"으로 보고 성공 처리한다.
        """
        body = {"Id": cheat_id, "Args": args or {}}
        candidate_bases = self._candidate_bases()

        for base in candidate_bases:
            endpoint = f"{base}/api/v2/cheats/execute"
            try:
                response = requests.post(endpoint, json=body, timeout=self.timeout_sec)
                if response.status_code == 404:
                    logger.info("v2 cheat API not available via %s", base)
                    continue
                response.raise_for_status()
            except requests.exceptions.ConnectionError as exc:
                # Unity 서버가 치트 실행 후 응답 없이 연결을 끊는 경우 → 성공으로 간주
                cause = str(exc)
                if "RemoteDisconnected" in cause or "Connection aborted" in cause:
                    logger.info(
                        "v2 cheat executed: %s via %s — no HTTP response (assumed success)",
                        cheat_id,
                        base,
                    )
                    return True
                logger.warning("v2 cheat connection error via %s: %s", base, exc)
                continue
            except requests.exceptions.ReadTimeout:
                # 치트가 앱 재시작 등을 유발해 응답이 오지 않는 경우 → 성공으로 간주
                logger.info(
                    "v2 cheat executed: %s via %s — read timeout (assumed success)",
                    cheat_id,
                    base,
                )
                return True
            except requests.RequestException as exc:
                logger.warning("v2 cheat request failed via %s: %s", base, exc)
                continue

            try:
                payload = response.json()
            except ValueError:
                logger.info("v2 cheat executed: %s via %s (non-JSON response)", cheat_id, base)
                return True

            if not isinstance(payload, dict) or not payload.get("success"):
                error_code = payload.get("errorCode") if isinstance(payload, dict) else None
                message = payload.get("message") if isinstance(payload, dict) else None
                if not_found_ok and error_code == "not_found":
                    logger.info(
                        "v2 cheat %s not registered in current scene — nothing to apply (treated as success)",
                        cheat_id,
                    )
                    return True
                logger.error(
                    "v2 cheat rejected: %s errorCode=%s message=%s",
                    cheat_id,
                    error_code,
                    message,
                )
                return False

            result = (payload.get("data") or {}).get("Result") or {}
            success = result.get("Success", True)
            logger.info(
                "v2 cheat executed: %s via %s → success=%s message=%s",
                cheat_id,
                base,
                success,
                result.get("Message"),
            )
            return bool(success)

        logger.error(
            "v2 cheat execute failed [%s] after endpoint fallbacks: %s",
            cheat_id,
            candidate_bases,
        )
        return False

    def find_cheat_id_v2(self, keyword_groups: List[List[str]]) -> Optional[str]:
        """v2 치트 목록에서 keyword_groups를 모두 만족하는 첫 치트 id를 찾는다.

        keyword_groups의 각 그룹은 OR, 그룹 간에는 AND로 본다.
        """
        for cheat in self.list_cheats_v2():
            haystack = " ".join(
                str(cheat.get(key) or "")
                for key in ("Id", "Category", "DisplayName", "Description")
            ).lower()
            if all(any(kw.lower() in haystack for kw in group) for group in keyword_groups):
                cheat_id = cheat.get("Id")
                if isinstance(cheat_id, str) and cheat_id:
                    return cheat_id
        return None

    def skip_tutorial(self, package: str = "") -> bool:
        """패키지명에 맞는 치트로 튜토리얼 스킵"""
        cheat = self._get_cheat(package, "skip_tutorial") or {}

        cheat_id = cheat.get("id")
        if cheat_id:
            return self.execute_cheat_v2(
                str(cheat_id),
                cheat.get("args") or {},
                not_found_ok=bool(cheat.get("not_found_ok")),
            )

        if cheat.get("category") and cheat.get("name"):
            if self.call_cheat(category=str(cheat["category"]), name=str(cheat["name"])):
                return True
            logger.info("Legacy cheat call failed for %s — trying v2 discovery", package)

        # 매핑이 없거나 레거시 호출이 실패한 빌드 → v2 목록에서 튜토리얼 스킵 치트 탐색
        discovered = self.find_cheat_id_v2(
            [["튜토", "tutorial"], ["스킵", "skip", "완료", "종료", "클리어", "clear", "complete"]]
        )
        if discovered:
            logger.info("Discovered v2 skip_tutorial cheat for %s: %s", package, discovered)
            return self.execute_cheat_v2(discovered)

        logger.error("skip_tutorial cheat not configured for package: %s", package)
        return False

    @staticmethod
    def _get_cheat(package: str, cheat_key: str) -> Optional[Dict[str, Any]]:
        """GAME_CHEAT_MAP에서 패키지별 치트 정보 조회 (v2 id 또는 레거시 category/name)"""
        game = GAME_CHEAT_MAP.get(package, {})
        cheat = game.get(cheat_key)
        if not isinstance(cheat, dict):
            return None
        if cheat.get("id"):
            return cheat
        if cheat.get("category") and cheat.get("name"):
            return cheat
        return None


    def _fetch_buttons(self) -> List[Dict[str, Any]]:
        # Unity API is exposed from the device; resolve base + keep adb forward in place.
        endpoint = f"{self._resolve_base()}/api/findAllButtons"

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

    @staticmethod
    def _strip_json_fence(value: str) -> str:
        value = value.strip()
        if value.startswith("```") and value.endswith("```"):
            lines = value.splitlines()
            if len(lines) >= 3:
                return "\n".join(lines[1:-1]).strip()
        return value

    def _parse_llm_selection_payload(self, raw_text: str) -> Optional[Dict[str, Any]]:
        cleaned = self._strip_json_fence(raw_text)
        payload = json.loads(cleaned)

        if isinstance(payload, dict):
            return payload

        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict) and "index" in item:
                    return item
            if len(payload) == 1:
                first = payload[0]
                if isinstance(first, dict):
                    return first
                if isinstance(first, int):
                    return {"index": first}
            logger.warning("Gemini returned list payload without selectable index: %s", payload)
            return None

        logger.warning("Gemini returned unsupported Unity payload type: %s", type(payload).__name__)
        return None

    def _choose_with_llm(
        self,
        target: str,
        buttons: List[Dict[str, Any]],
    ) -> Optional[UnityMatch]:
        if not buttons:
            return None
        client = self._ensure_llm_client()
        if client is None:
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
            with langfuse.start_as_current_observation(
                as_type="span",
                name="choose_unity_button",
                input={"target": target, "candidates_count": len(buttons)},
            ) as span:
                response = client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=self.temperature,
                    ),
                )
                payload = self._parse_llm_selection_payload(response.text)
                span.update(output=payload)

            if not payload:
                return None
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
        if llm_match and llm_match.score >= min_score:
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

    def find_exact_button(self, names: str | List[str]) -> Optional[UnityMatch]:
        """LLM/유사도 없이 Unity 메타데이터가 정확히 하나만 일치할 때 반환한다.

        `unity_name`은 Game Profile이 실기기에서 확인한 안정적인 식별자만 넣는
        결정적 경로다. 후보가 없거나 둘 이상이면 오탭을 피하기 위해 실패시키고,
        호출부가 Vision 폴백 여부를 결정한다.
        """
        raw_names = [names] if isinstance(names, str) else list(names)
        wanted = {
            self._normalize_text(name)
            for name in raw_names
            if isinstance(name, str) and name.strip()
        }
        if not wanted:
            return None

        matches: List[Dict[str, Any]] = []
        for button in self._filter_buttons(self._fetch_buttons()):
            fields = [
                button.get("SpecifiedName"),
                button.get("GameObjectName"),
                button.get("Text"),
                button.get("Name"),
            ]
            normalized_fields = {
                self._normalize_text(value)
                for value in fields
                if isinstance(value, str) and value.strip()
            }
            if wanted & normalized_fields:
                matches.append(button)

        if len(matches) != 1:
            if matches:
                logger.warning(
                    "Unity exact lookup is ambiguous for %s: %d matches",
                    sorted(wanted),
                    len(matches),
                )
            return None
        return UnityMatch(button=matches[0], score=1.0)

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

        if screen_x < 0 or screen_x >= screen_width:
            return None
        if screen_y < 0 or screen_y >= screen_height:
            return None

        return {"x": screen_x, "y": screen_y}
