# qa_agent/executor/recovery.py
from typing import Any, Dict, List

def should_replan(state: Dict[str, Any], recent_results: List[Dict[str, Any]]) -> bool:
    # 1) find_attempts 초과
    if state.get("find_attempts", 0) > state.get("max_find_attempts", 3):
        return True

    # 2) 최근 결과에 error가 연속으로 많음
    errors = [r for r in recent_results if "error" in r]
    if len(recent_results) >= 2 and len(errors) >= 2:
        return True

    # 3) 같은 tool 반복 감지(옵션)
    # repeat_count 같은 걸 state에 유지한다면 여기서 판단 가능

    return False
