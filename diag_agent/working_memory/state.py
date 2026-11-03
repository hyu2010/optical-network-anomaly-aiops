"""Working memory: the LangGraph state of one diagnosis thread (doc 05 §1).

All slots hold plain JSON-compatible data so that every checkpoint can be
serialised, replayed and inspected.  Each slot is owned by one node
(doc 02 §2); the ownership map below is checked by a unit test.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


def merge_dict(left: dict | None, right: dict | None) -> dict:
    out = dict(left or {})
    out.update(right or {})
    return out


def append_nodes(left: list | None, right: list | None) -> list:
    """Append tree deltas; a node with an existing id replaces it in place."""
    out = list(left or [])
    pos = {n["id"]: i for i, n in enumerate(out)}
    for n in right or []:
        if n["id"] in pos:
            out[pos[n["id"]]] = n
        else:
            pos[n["id"]] = len(out)
            out.append(n)
    return out


class DiagnosisState(TypedDict, total=False):
    question: dict                     # anchor, t_question, operator text
    clock: float                       # agent time, s after created_at
    incident_no: str
    raw_incident_id: int
    created_at: str
    frame: dict | None
    windows: dict                      # prediction id -> window and state
    predictions: dict                  # hypothesis id -> [prediction]
    line_results: Annotated[dict, merge_dict]
    hypotheses: dict                   # H# -> hypothesis
    ledger: dict
    tree: Annotated[list, append_nodes]
    plan: dict | None
    budget: dict
    obs_log: Annotated[list, operator.add]
    pending_check: dict | None
    decision: str | None
    decision_info: dict
    incident: dict
    pass_no: int
    cycle: int
    mode: str                          # diagnose | verify | closed
    walk_visited: list
    closure: dict
    task: dict


OWNERSHIP: dict[str, set[str]] = {
    "ingest": {"obs_log", "clock", "question", "closure"},
    "frame": {"frame", "tree", "cycle", "clock", "mode", "budget", "pass_no"},
    "raise_incident": {"incident", "incident_no", "task", "tree", "closure"},
    "retrieve_episodes": {"line_results", "tree"},
    "line_walk": {"line_results", "tree"},
    "line_correlation": {"line_results", "tree"},
    "line_analogy": {"line_results", "tree"},
    "line_llm": {"line_results", "tree", "walk_visited", "budget"},
    "pool": {"hypotheses", "ledger", "tree", "budget", "walk_visited"},
    "evaluate": {"predictions", "windows", "tree", "frame", "cycle"},
    "synthesize": {"ledger", "tree"},
    "decide": {"decision", "decision_info", "tree", "mode"},
    "plan": {"plan", "tree", "decision", "decision_info", "budget"},
    "execute_check": {"obs_log", "clock", "budget", "tree", "pending_check"},
    "wait": {"clock", "tree"},
    "widen": {"pass_no", "budget", "tree", "line_results"},
    "narrate": {"incident", "budget", "tree"},
    "update_incident": {"incident", "task"},
    "verify_gate": set(),
    "await_closure": {"closure", "clock", "incident", "tree", "obs_log", "mode", "task"},
    "learn": {"closure", "tree"},
}


def initial_state(*, raw_incident_id: int, created_at: str, question: dict | None = None,
                  clock: float = 0.0) -> dict[str, Any]:
    return {
        "question": question or {"anchor": "incident", "text": "What is the root cause of this incident?"},
        "clock": clock,
        "raw_incident_id": raw_incident_id,
        "created_at": created_at,
        "frame": None,
        "windows": {},
        "predictions": {},
        "line_results": {},
        "hypotheses": {},
        "ledger": {},
        "tree": [],
        "plan": None,
        "budget": {"checks": 0, "effort_h": 0.0, "returns": 0, "llm_calls": 0, "tokens": 0, "sites": [],
                   "remote_checks": 0, "field_checks": 0, "checks_done": []},
        "obs_log": [],
        "pending_check": None,
        "decision": None,
        "decision_info": {},
        "incident": {},
        "pass_no": 0,
        "cycle": 0,
        "mode": "diagnose",
        "walk_visited": [],
        "closure": {},
        "task": {},
    }
