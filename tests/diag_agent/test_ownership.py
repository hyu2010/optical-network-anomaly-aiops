"""Each LangGraph node writes only the state slots it owns (doc 05 §3)."""

from __future__ import annotations

import functools

from diag_agent.procedural_memory.decision_procedure.nodes import Nodes
from diag_agent.working_memory.state import OWNERSHIP

from .conftest import run_scenario


def test_nodes_write_only_owned_slots(settings, tmp_path, monkeypatch):
    seen: dict[str, set[str]] = {}
    for name in OWNERSHIP:
        orig = getattr(Nodes, name)

        def wrap(orig=orig, name=name):
            @functools.wraps(orig)
            def f(self, state):
                out = orig(self, state)
                if isinstance(out, dict):
                    seen.setdefault(name, set()).update(out)
                return out
            return f
        monkeypatch.setattr(Nodes, name, wrap())
    agent, st = run_scenario(settings, tmp_path, no_llm=True)
    agent.close()
    assert st["decision"] == "commit"
    bad = {n: keys - OWNERSHIP[n] for n, keys in seen.items() if keys - OWNERSHIP[n]}
    assert not bad
