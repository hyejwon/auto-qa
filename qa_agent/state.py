# qa_agent/state.py
from typing import Any, Dict, List, Optional, TypedDict, Literal

class AgentState(TypedDict, total=False):
    # run control
    status: Literal["running", "done", "error"]
    error: str
    step: int
    max_steps: int

    # planning/executing mode
    mode: Literal["planning", "executing", "replanning"]

    # goal & plan
    goal: str
    plan: Dict[str, Any]
    goal_steps: List[Dict[str, Any]]
    current_step_idx: int

    # conversation
    messages: List[Dict[str, Any]]

    # tools
    tools_schema: List[Dict[str, Any]]
    tool_name_map: Dict[str, Any]

    # tool calls/results
    pending_tool_calls: List[Dict[str, Any]]
    last_tool_results: List[Dict[str, Any]]
    trace: List[Dict[str, Any]]

    # app/device
    package_name: str
    device_id: str

    # UI observation cache
    last_screenshot_b64: str
    last_screenshot_mime: str
    last_screenshot_path: str

    # unity caching
    last_unity_buttons: List[Dict[str, Any]]
    target_button_name: Optional[str]
    max_find_attempts: int
    find_attempts: int

    # repeat detection (optional)
    last_tool_name: str
    last_tool_args: Dict[str, Any]
    repeat_count: int
