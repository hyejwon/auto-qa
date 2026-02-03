# qa_agent/graph/build_graph.py
from langgraph.graph import StateGraph
from qa_agent.state import AgentState
from .router import router

def build_graph(planner_node, replan_node, executor_node):
    g = StateGraph(AgentState)

    g.add_node("planner", planner_node)
    g.add_node("replan", replan_node)
    g.add_node("executor", executor_node)

    g.add_conditional_edges("planner", router)
    g.add_conditional_edges("replan", router)
    g.add_conditional_edges("executor", router)

    g.set_entry_point("planner")
    return g.compile()
