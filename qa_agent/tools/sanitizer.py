# qa_agent/tools/sanitizer.py
import json
import logging
from typing import Any, Dict, Optional, List

logger = logging.getLogger(__name__)

ARG_ALIASES = {
    "device": "device_id",
    "id": "device_id",
    "udid": "device_id",
    "serial": "device_id",
    "package": "package_name",
    "pkg": "package_name",
    "app": "package_name",
    "bundle_id": "package_name",
    "btn": "button",
    "btn_name": "button",
    "save_path": "debug_dir",  # take_screenshot의 save_path를 debug_dir로 매핑
    "path": "debug_dir",
    "button_name": "button"
}

def normalize_tool_output(out: Any) -> Any:
    if isinstance(out, list) and out and isinstance(out[0], dict) and out[0].get("type") == "text":
        text = out[0].get("text", "")
        if isinstance(text, str) and text:
            try:
                return json.loads(text)
            except Exception:
                return text
        return out

    if isinstance(out, str):
        s = out.strip()
        if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
            try:
                return json.loads(s)
            except Exception:
                return out
        return out

    return out

def _get_allowed_fields(tool) -> set[str]:
    """
    Tool의 실제 인자 스키마에서 허용된 필드를 추출합니다.
    Pydantic 모델, dict, 또는 JSON 스키마를 지원합니다.
    """
    schema = getattr(tool, "args_schema", None)
    if not schema:
        # args_schema가 없으면 tool의 JSON 스키마를 시도
        try:
            if hasattr(tool, "model_json_schema"):
                json_schema = tool.model_json_schema()
                if isinstance(json_schema, dict) and "properties" in json_schema:
                    return set(json_schema["properties"].keys())
        except Exception:
            pass
        return set()

    # dict인 경우 (JSON 스키마)
    if isinstance(schema, dict):
        if "properties" in schema:
            return set(schema["properties"].keys())
        return set(schema.keys())

    # Pydantic 모델인 경우
    try:
        # Pydantic v2: model_json_schema() 사용
        if hasattr(schema, "model_json_schema"):
            json_schema = schema.model_json_schema()
            if isinstance(json_schema, dict) and "properties" in json_schema:
                return set(json_schema["properties"].keys())
        
        # Pydantic v2: model_fields 직접 접근
        if hasattr(schema, "model_fields"):
            return set(schema.model_fields.keys())
        
        # Pydantic v1: __fields__ 접근
        if hasattr(schema, "__fields__"):
            return set(schema.__fields__.keys())
    except Exception as e:
        # 에러 발생 시 로깅 (선택적)
        pass

    # 특수 케이스: unity_click_button
    if getattr(tool, "name", "") == "unity_click_button":
        return {"button"}

    return set()

def sanitize_tool_arguments(tool, raw_args: Dict[str, Any]) -> Dict[str, Any]:
    raw_args = raw_args or {}
    fixed: Dict[str, Any] = {}

    for k, v in raw_args.items():
        if v is None:
            continue
        kk = ARG_ALIASES.get(k, k)
        fixed[kk] = v

    allowed = _get_allowed_fields(tool)
    tool_name = getattr(tool, "name", "")

    if tool_name == "unity_click_button":
        allowed.add("button")
        if "button" in fixed:
            filtered = {"button": fixed["button"]}
            for k, v in fixed.items():
                if k in allowed and k != "button":
                    filtered[k] = v
            return filtered

    # allowed가 비어있으면 모든 fixed 인자를 반환 (필수 인자 보존)
    if len(allowed) == 0:
        return fixed

    return {k: v for k, v in fixed.items() if k in allowed}

def has_valid_coordinates(button: Dict[str, Any]) -> bool:
    if not isinstance(button, dict):
        return False
    x, y = button.get("PositionX"), button.get("PositionY")
    return isinstance(x, (int, float)) and isinstance(y, (int, float))

def find_button_by_name(buttons: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    if not buttons or not name:
        return None
    for b in buttons:
        bn = b.get("SpecifiedName") or b.get("GameObjectName") or ""
        if bn == name:
            return b
    return None
