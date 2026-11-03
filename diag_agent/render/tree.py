"""Diagnosis-tree renderers (doc 04 §9): ASCII, Mermaid and canonical JSON.

Decision view: the question/check/answer path with the decision and
verification.  Trace view: every node, in creation order, nested by parent.
"""

from __future__ import annotations

import json

DECISION_KINDS = {"root", "question", "check", "answer", "decision", "verify"}


def _children(nodes: list[dict]) -> dict[str | None, list[dict]]:
    ch: dict[str | None, list[dict]] = {}
    for n in nodes:
        ch.setdefault(n.get("parent"), []).append(n)
    for v in ch.values():
        v.sort(key=lambda n: int(n["id"][1:]))
    return ch


def view_nodes(tree: list[dict], view: str = "decision") -> list[dict]:
    if view == "trace":
        return list(tree)
    keep = [n for n in tree if n["kind"] in DECISION_KINDS]
    ids = {n["id"] for n in keep}
    out = []
    for n in keep:
        p = n.get("parent")
        while p is not None and p not in ids:
            p = next((x.get("parent") for x in tree if x["id"] == p), None)
        out.append(dict(n, parent=p))
    return out


def ascii_tree(tree: list[dict], view: str = "decision", *, width: int = 160) -> str:
    nodes = view_nodes(tree, view)
    ch = _children(nodes)
    lines: list[str] = []

    def label(n: dict) -> str:
        s = n["label"]
        if view == "trace":
            s = f"[{n['kind']}] {s}"
        return s if len(s) <= width else s[: width - 1] + "…"

    def rec(n: dict, prefix: str, last: bool, top: bool) -> None:
        if top:
            lines.append(label(n))
            child_prefix = ""
        else:
            dashed = n["kind"] in ("answer", "pruned") and not n.get("taken", True)
            conn = ("└┄ " if last else "├┄ ") if dashed else ("└── " if last else "├── ")
            lines.append(prefix + conn + label(n))
            child_prefix = prefix + ("    " if last else "│   ")
        kids = ch.get(n["id"], [])
        for i, k in enumerate(kids):
            rec(k, child_prefix, i == len(kids) - 1, False)

    for r in ch.get(None, []):
        rec(r, "", True, True)
    return "\n".join(lines)


def _mm(s: str) -> str:
    return s.replace('"', "'").replace("\n", " ")[:120]


def mermaid_tree(tree: list[dict], view: str = "decision") -> str:
    nodes = view_nodes(tree, view)
    out = ["flowchart TD"]
    shape = {"root": ('["', '"]'), "question": ('{"', '"}'), "check": ('[/"', '"/]'), "answer": ('("', '")'),
             "decision": ('(["', '"])'), "verify": ('(["', '"])'), "frame": ('["', '"]'), "generate": ('["', '"]'),
             "hypothesis": ('("', '")'), "pruned": ('("', '")'), "analogy": ('("', '")'), "evaluate": ('["', '"]'),
             "synthesis": ('["', '"]')}
    for n in nodes:
        a, b = shape.get(n["kind"], ('["', '"]'))
        out.append(f"  {n['id']}{a}{_mm(n['label'])}{b}")
    for n in nodes:
        if n.get("parent"):
            dashed = n["kind"] in ("answer", "pruned") and not n.get("taken", True)
            out.append(f"  {n['parent']} {'-.->' if dashed else '-->'} {n['id']}")
    return "\n".join(out) + "\n"


def json_tree(tree: list[dict], view: str = "trace") -> str:
    nodes = sorted(view_nodes(tree, view), key=lambda n: int(n["id"][1:]))
    return json.dumps(nodes, sort_keys=True, ensure_ascii=False, indent=1) + "\n"


def render(tree: list[dict], view: str = "decision", fmt: str = "ascii") -> str:
    if fmt == "mermaid":
        return mermaid_tree(tree, view)
    if fmt == "json":
        return json_tree(tree, view)
    return ascii_tree(tree, view)
