# qa_agent/graph/router.py
from langgraph.graph import END

def router(state):
    if state.get("status") in ("done", "error"):
        return END

    mode = state.get("mode", "executing")
    if mode == "planning":
        return "planner"
    if mode == "replanning":
        return "replan"

    # executing
    steps = state.get("goal_steps", []) or []
    idx = state.get("current_step_idx", 0)
    if idx >= len(steps):
        state["status"] = "done"
        return END

    return "executor"
