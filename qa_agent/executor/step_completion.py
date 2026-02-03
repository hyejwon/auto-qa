# qa_agent/executor/step_completion.py
import logging
from typing import Any, Dict
from qa_agent.tools.sanitizer import normalize_tool_output

logger = logging.getLogger(__name__)

def check_step_completion(state: Dict[str, Any], tool_name: str, tool_result: Any) -> bool:
    goal_steps = state.get("goal_steps", [])
    idx = state.get("current_step_idx", 0)
    if not goal_steps or idx >= len(goal_steps):
        logger.debug(f"check_step_completion: no goal_steps or idx out of range (idx={idx}, len={len(goal_steps)})")
        return False

    step_obj = goal_steps[idx]
    tool_result = normalize_tool_output(tool_result)

    if isinstance(step_obj, dict):
        expected_tool = step_obj.get("tool")
        if expected_tool and tool_name != expected_tool:
            logger.debug(f"check_step_completion: tool mismatch (expected={expected_tool}, actual={tool_name})")
            return False

        # tool_result가 dict인 경우
        if isinstance(tool_result, dict):
            # 명시적인 error 필드 체크
            if "error" in tool_result:
                logger.debug(f"check_step_completion: explicit error field found: {tool_result.get('error')}")
                return False
            
            # status 필드가 있는 경우
            status = tool_result.get("status")
            if status is not None:
                if tool_name == "smart_find":
                    result = status in ("present", "success","found", True)
                    logger.debug(f"check_step_completion: smart_find status={status}, result={result}")
                    return result
                else:
                    result = status == "success" or status is True
                    logger.debug(f"check_step_completion: tool={tool_name}, status={status}, result={result}")
                    return result
            
            # status 필드가 없지만 error도 없는 경우 -> 성공으로 간주
            # (일부 tool은 status 없이 결과만 반환할 수 있음)
            logger.debug(f"check_step_completion: no status field, no error field -> success (tool={tool_name})")
            return True

        # tool_result가 dict가 아닌 경우
        result_str = str(tool_result).lower()
        has_error = "error" in result_str
        result = not has_error
        logger.debug(f"check_step_completion: non-dict result, has_error={has_error}, result={result}")
        return result

    logger.debug(f"check_step_completion: step_obj is not dict (type={type(step_obj)})")
    return False
