# qa_agent/tools/schemas.py
from typing import Any, Dict, List

def lc_tools_to_openai_schema(tools: List[Any]) -> List[Dict[str, Any]]:
    schemas = []
    for t in tools:
        params = {"type": "object", "properties": {}, "additionalProperties": True}
        if hasattr(t, "args_schema") and t.args_schema:
            try:
                params = t.args_schema.model_json_schema()
            except Exception:
                pass

        schemas.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": getattr(t, "description", "") or "",
                "parameters": params,
            }
        })
    return schemas
