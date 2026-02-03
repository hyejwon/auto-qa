# qa_agent/executor/executor.py
import asyncio
import json
import logging
from typing import Any, Dict
from qa_agent.tools.sanitizer import (
    sanitize_tool_arguments,
    has_valid_coordinates, find_button_by_name
)
from .step_completion import check_step_completion
from .recovery import should_replan

logger = logging.getLogger(__name__)
TRANSITION_TOOLS = {
    "smart_click",
    "unity_click_button",
    "uiauto_click",
    "vision_enhanced_click",
    "unity_scroll",
    "mobile_launch_app",
    "mobile_terminate_app",
    "smart_find"
}
# Tools that need extra time before execution (for screen to fully load)
BUTTON_FINDING_TOOLS = {
    "smart_find",
    "smart_click",
    "unity_click_button",
    "vision_enhanced_click",
    "uiauto_click"
}
# Tools that need extra time after execution (for app to fully launch)
APP_LAUNCH_TOOLS = {
    "mobile_launch_app"
}

async def run_current_step(state: Dict[str, Any]) -> Dict[str, Any]:
    tool_map = state.get("tool_name_map", {}) or {}
    steps = state.get("goal_steps", []) or []
    idx = state.get("current_step_idx", 0)

    if idx >= len(steps):
        return {"status": "done"}

    step_obj = steps[idx]
    name = step_obj["tool"]
    raw_args = step_obj.get("args", {}) or {}

    logger.info(f"\n{'='*60}")
    logger.info(f"🔧 [Step {idx + 1}/{len(steps)}] Tool: {name}")
    logger.info(f"   Goal: {step_obj.get('goal', 'N/A')}")

    tool = tool_map.get(name)
    if tool is None:
        return {"status": "error", "error": f"tool not found: {name}"}

    # sanitize
    args = sanitize_tool_arguments(tool, raw_args)

    # sanitize가 빈 딕셔너리를 반환한 경우, raw_args를 그대로 사용 (필수 인자 보존)
    if not args and raw_args:
        logger.warning(f"⚠️ sanitize_tool_arguments returned empty dict for {name}, using raw_args")
        args = raw_args

    # Show arguments that will be used
    if args:
        logger.info(f"   📋 Arguments: {json.dumps(args, ensure_ascii=False, indent=2)}")
    else:
        logger.info(f"   📋 Arguments: (none)")

    # defaults
    if not args:
        if name == "set_device" and state.get("device_id"):
            args = {"device_id": state["device_id"]}
        if name in ("mobile_launch_app", "mobile_terminate_app") and state.get("package_name"):
            args = {"package_name": state["package_name"]}
    
    # 필수 인자 검증 (smart_click 등)
    if name == "smart_click" and not args.get("target"):
        # raw_args에서 target 찾기
        if raw_args.get("target"):
            args["target"] = raw_args["target"]
        else:
            return {
                "status": "error",
                "error": f"smart_click requires 'target' argument, but got: {raw_args}"
            }

    # unity_click_button coordinate restore (if planner used name-only)
    if name == "unity_click_button":
        if "button" not in args and isinstance(raw_args, dict):
            bn = raw_args.get("button_name")
            if isinstance(bn, str) and bn.strip():
                cached = state.get("last_unity_buttons", [])
                found = find_button_by_name(cached, bn.strip())
                if found:
                    args = {"button": found}

        btn = args.get("button")
        if not (isinstance(btn, dict) and has_valid_coordinates(btn)):
            return {"status":"error", "error":"unity_click_button requires button with PositionX/PositionY (cache miss or invalid)"}

    # Pre-execution delay for button finding tools (wait for screen to fully load)
    if name in BUTTON_FINDING_TOOLS:
        PRE_BUTTON_FIND_DELAY = 5.0  # seconds
        logger.info(f"⏳ Waiting {PRE_BUTTON_FIND_DELAY}s before {name} (for screen to load)...")
        await asyncio.sleep(PRE_BUTTON_FIND_DELAY)

    # execute
    logger.info(f"⚡ Invoking {name}...")
    out = await tool.ainvoke(args) if hasattr(tool, "ainvoke") else tool.invoke(args)

    # Log result summary
    if isinstance(out, dict):
        status = out.get("status", "unknown")
        logger.info(f"✓ Tool {name} completed with status: {status}")
        if status == "error":
            logger.error(f"   ❌ Error: {out.get('error', 'Unknown error')}")
        elif status == "success":
            # Show relevant success details
            success_info = []
            if "message" in out:
                success_info.append(f"message={out['message']}")
            if "found" in out:
                success_info.append(f"found={out['found']}")
            if "element" in out:
                success_info.append(f"element={out['element']}")
            if success_info:
                logger.info(f"   ✓ {', '.join(success_info)}")

        # Full result in debug mode
        logger.debug(f"   📄 Full result: {json.dumps(out, ensure_ascii=False, indent=2)[:1000]}")
    else:
        logger.info(f"✓ Tool {name} completed")
        logger.debug(f"   📄 Result: {str(out)[:500]}")

    # Post-execution delay
    update = {}

    # Basic delay after all tool executions
    TOOL_EXECUTION_DELAY = 2.0  # seconds
    logger.debug(f"⏱️ Waiting {TOOL_EXECUTION_DELAY}s after tool execution...")
    await asyncio.sleep(TOOL_EXECUTION_DELAY)

    # Extra delay after app launch (for app to fully start)
    if name in APP_LAUNCH_TOOLS:
        POST_LAUNCH_DELAY = 8.0  # seconds
        logger.info(f"⏳ Waiting {POST_LAUNCH_DELAY}s after {name} (for app to fully launch)...")
        await asyncio.sleep(POST_LAUNCH_DELAY)

    # record
    result = {"name": name, "result": out}
    update["last_tool_results"] = [result]

    # trace 업데이트 (기존 trace에 추가)
    current_trace = state.get("trace", [])
    current_trace.append({"step": state.get("step", 0), "tool_results": [result]})
    update["trace"] = current_trace

    # step completion
    advanced = check_step_completion(state, name, out)

    if advanced:
        logger.info(f"✅ Step {idx + 1}/{len(steps)} completed successfully")
        logger.info(f"   Goal: {step_obj.get('goal', 'N/A')}")
        if idx >= len(steps) - 1:
            logger.info(f"🎉 All {len(steps)} steps completed!")
            update["status"] = "done"
            update["current_step_idx"] = idx + 1
        else:
            next_step = steps[idx + 1]
            logger.info(f"➡️  Moving to step {idx + 2}/{len(steps)}: {next_step.get('tool', 'unknown')}")
            # 다음 단계로 진행
            update["current_step_idx"] = idx + 1
            update["status"] = "running"
    else:
        # 단계가 완료되지 않음
        # 최대 재시도 횟수 체크
        max_retries = state.get("max_step_retries", 2)
        retry_key = f"step_{idx}_retries"
        retry_count = state.get(retry_key, 0)
        
        if isinstance(out, dict) and out.get("status") == "error":
            # 에러 발생
            if should_replan(state, [result]):
                update["mode"] = "replanning"
                update["status"] = "running"
                logger.warning(f"⚠️ Step {idx + 1} failed, triggering replan")
            elif retry_count >= max_retries:
                # 재시도 횟수 초과 - 다음 단계로 넘어가기
                if idx < len(steps) - 1:
                    update["current_step_idx"] = idx + 1
                    update["status"] = "running"
                    logger.warning(f"⚠️ Step {idx + 1} max retries reached, moving to next step")
                else:
                    update["status"] = "done"
            else:
                # 재시도
                update[retry_key] = retry_count + 1
                update["status"] = "running"
                logger.info(f"🔄 Step {idx + 1} failed, retrying ({retry_count + 1}/{max_retries})")
        else:
            # 에러는 아니지만 완료 조건을 만족하지 않음
            if retry_count >= max_retries:
                # 재시도 횟수 초과 - 다음 단계로 넘어가기
                if idx < len(steps) - 1:
                    update["current_step_idx"] = idx + 1
                    update["status"] = "running"
                    logger.warning(f"⚠️ Step {idx + 1} not completed after {max_retries} retries, moving to next step")
                else:
                    update["status"] = "done"
            else:
                # 재시도
                update[retry_key] = retry_count + 1
                update["status"] = "running"
                logger.info(f"🔄 Step {idx + 1} not completed, retrying ({retry_count + 1}/{max_retries})")

    # recovery trigger (위에서 처리하지 않은 경우만)
    if "mode" not in update and should_replan(state, [result]):
        update["mode"] = "replanning"
        update["status"] = "running"

    # step 카운터 증가
    update["step"] = state.get("step", 0) + 1
    update["status"] = update.get("status", state.get("status", "running"))
    
    return update
