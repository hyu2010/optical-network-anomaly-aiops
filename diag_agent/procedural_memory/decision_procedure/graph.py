"""LangGraph StateGraph of the decision procedure (doc 05 §2)."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from ...working_memory.state import DiagnosisState
from .nodes import Deps, Nodes

STEP_NODES = ["frame", "pool", "evaluate", "synthesize", "decide", "plan", "execute_check", "update_incident",
              "await_closure"]


def build_graph(deps: Deps, *, checkpointer=None, step: bool = False):
    n = Nodes(deps)
    g = StateGraph(DiagnosisState)
    for name in ("ingest", "frame", "raise_incident", "retrieve_episodes", "pool", "evaluate", "synthesize",
                 "decide", "plan", "execute_check", "wait", "widen", "narrate", "update_incident",
                 "verify_gate", "await_closure", "learn"):
        g.add_node(name, getattr(n, name))
    lines = list(deps.cfg["lines"])
    for line in lines:
        g.add_node(f"line_{line}", getattr(n, f"line_{line}"))

    g.add_edge(START, "ingest")
    g.add_conditional_edges("ingest", n.after_ingest, {"frame": "frame", "evaluate": "evaluate", "__end__": END})
    g.add_edge("frame", "raise_incident")
    g.add_edge("raise_incident", "retrieve_episodes")
    g.add_conditional_edges("retrieve_episodes", n.fan_out, [f"line_{l}" for l in lines])
    g.add_conditional_edges("widen", n.fan_out, [f"line_{l}" for l in lines])
    for line in lines:
        g.add_edge(f"line_{line}", "pool")
    g.add_edge("pool", "evaluate")
    g.add_edge("evaluate", "synthesize")
    g.add_edge("synthesize", "decide")
    g.add_conditional_edges("decide", n.route_decision,
                            {"narrate": "narrate", "plan": "plan", "widen": "widen", "update_incident": "update_incident"})
    g.add_conditional_edges("plan", n.route_plan, {"execute_check": "execute_check", "narrate": "narrate"})
    g.add_edge("execute_check", "ingest")
    g.add_edge("wait", "ingest")
    g.add_edge("narrate", "update_incident")
    g.add_edge("update_incident", "verify_gate")
    g.add_conditional_edges("verify_gate", n.route_verify, {"wait": "wait", "await_closure": "await_closure"})
    g.add_edge("await_closure", "learn")
    g.add_edge("learn", END)
    return g.compile(checkpointer=checkpointer, interrupt_after=STEP_NODES if step else None)


def recursion_limit(cfg: dict) -> int:
    a = cfg["advisor"]
    return int(a["max_checks"] * 8 + (a["max_returns"] + 1) * 12 + 80)
