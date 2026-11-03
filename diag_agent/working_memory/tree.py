"""Diagnosis tree (doc 04 §1, §9): node model and helpers.

Nodes are dicts so they can be checkpointed as-is:
  id, parent, kind, cycle, t_agent, label, detail, mass, posterior, taken, evidence
"""

from __future__ import annotations

from typing import Any, Iterable

KINDS = ("root", "frame", "generate", "hypothesis", "pruned", "analogy", "evaluate", "question", "check",
         "answer", "synthesis", "decision", "verify")


def next_id(tree: Iterable[dict], extra: int = 0) -> str:
    n = 0
    for node in tree:
        try:
            n = max(n, int(node["id"][1:]))
        except (ValueError, KeyError):
            continue
    return f"n{n + 1 + extra:02d}"


class TreeBuilder:
    """Allocates node ids on top of the current tree and collects new nodes."""

    def __init__(self, tree: list[dict], cycle: int, t_agent: float):
        self.base = list(tree)
        self.cycle = cycle
        self.t = t_agent
        self.new: list[dict] = []
        self._next = 0
        for node in self.base:
            try:
                self._next = max(self._next, int(node["id"][1:]))
            except (ValueError, KeyError):
                pass

    def add(self, kind: str, label: str, *, parent: str | None, detail: dict | None = None,
            mass: float | None = None, posterior: dict | None = None, taken: bool = True,
            evidence: list[str] | None = None, node_id: str | None = None) -> str:
        assert kind in KINDS, kind
        if node_id is None:
            self._next += 1
            node_id = f"n{self._next:02d}"
        self.new.append({"id": node_id, "parent": parent, "kind": kind, "cycle": self.cycle,
                         "t_agent": round(float(self.t), 3), "label": label, "detail": detail or {},
                         "mass": None if mass is None else round(float(mass), 6),
                         "posterior": posterior, "taken": taken, "evidence": evidence or []})
        return node_id

    def update(self, node: dict, **changes: Any) -> None:
        n = dict(node)
        n.update(changes)
        self.new.append(n)

    def all_nodes(self) -> list[dict]:
        out = {n["id"]: n for n in self.base}
        for n in self.new:
            out[n["id"]] = n
        return list(out.values())


def find(tree: list[dict], **match: Any) -> list[dict]:
    return [n for n in tree if all(n.get(k) == v for k, v in match.items())]


def root_id(tree: list[dict]) -> str | None:
    for n in tree:
        if n["kind"] == "root":
            return n["id"]
    return None


def children(tree: list[dict], node_id: str) -> list[dict]:
    return [n for n in tree if n.get("parent") == node_id]


def last_of(tree: list[dict], kind: str) -> dict | None:
    out = [n for n in tree if n["kind"] == kind]
    return out[-1] if out else None
