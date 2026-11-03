"""Analogy line (doc 04 §5.4): retrieve similar closed episodes and re-resolve
their conclusion relations (mode + location term + anchor signature) onto
this incident's frame."""

from __future__ import annotations

from typing import Any

MIN_SIM = 0.25


def anchor_signature(item: dict) -> str:
    return f"{item['condition']}[{item['layer']}]@{item['role']}"


def conclusion_relations(sem, frame: dict, mode: str, location: str, limit: int = 3) -> list[dict]:
    """Express a located conclusion relative to the frame, for storage in an episode."""
    rels: list[dict] = []
    order = sorted(frame["items"], key=lambda it: (it["id"] != frame.get("anchor"), int(it["id"][1:])))
    for it in order:
        if not it["relevant"]:
            continue
        for term in sorted(sem.location_terms()):
            if term == "shared":
                continue
            if location in sem.resolve_location(term, it["resource"]):
                rel = {"term": term, "anchor_sig": anchor_signature(it)}
                if rel not in rels:
                    rels.append(rel)
                break
        if len(rels) >= limit:
            break
    for e in frame.get("shared", []):
        if e["resource"] == location:
            rels.append({"term": "shared", "anchor_kind": e["kind"]})
            break
    if location == "" or not rels:
        rels.append({"term": "self", "resource": location})
    return rels


def resolve_relation(sem, frame: dict, mode: str, rel: dict) -> list[str]:
    applies = sem.mode(mode)["applies_to"] if mode in sem.modes() else None
    out: list[str] = []
    if rel["term"] == "shared":
        out = [e["resource"] for e in frame.get("shared", []) if e["kind"] == rel.get("anchor_kind")][:1]
    elif rel["term"] == "self":
        out = [rel["resource"]] if any(it["resource"] == rel.get("resource") for it in frame["items"]) else []
    else:
        for it in frame["items"]:
            if anchor_signature(it) == rel.get("anchor_sig"):
                out = sem.resolve_location(rel["term"], it["resource"])
                if out:
                    break
    return [l for l in out if applies is None or applies in sem.classes_of(l)]


def run(sem, episodic, frame: dict, *, k: int = 20) -> dict[str, Any]:
    base = {"line": "analogy", "dist": {}, "hyps": {}, "pruned": [], "residual": 1.0}
    if episodic is None or episodic.count() == 0:
        return dict(base, status="abstain", detail={"reason": "episodic memory is empty"})
    retrieved = episodic.retrieve(frame["role_form"], k=k)
    votes: dict[str, float] = {}
    hyps: dict[str, dict] = {}
    used = []
    best = 0.0
    for ep, sim in retrieved:
        if sim < MIN_SIM:
            continue
        concl = ep["conclusion"]
        mode = concl.get("mode")
        if mode not in sem.modes():
            continue
        locs: list[str] = []
        for rel in concl.get("relations", []):
            locs = resolve_relation(sem, frame, mode, rel)
            if locs:
                break
        if not locs:
            continue
        w = sim * ep["weight"]
        best = max(best, sim)
        used.append({"episode": ep["id"], "incident_no": ep["incident_no"], "similarity": round(sim, 3),
                     "weight": ep["weight"], "mode": mode, "resolved": locs, "summary": ep.get("summary", "")})
        for l in locs:
            key = f"{mode}@{l}"
            votes[key] = votes.get(key, 0.0) + w / len(locs)
            hyps.setdefault(key, {"mode": mode, "location": l, "provenance": {"analogy": []}})
            hyps[key]["provenance"]["analogy"].append({"episode": ep["id"], "similarity": round(sim, 3)})
    if not votes:
        return dict(base, status="abstain", detail={"reason": "no retrieved episode resolves onto this frame",
                                                   "retrieved": len(retrieved)})
    residual = min(0.6, 0.05 + 0.5 * (1 - best))
    z = sum(votes.values())
    dist = {key: (1 - residual) * v / z for key, v in votes.items()}
    return {"line": "analogy", "status": "ok", "dist": dist, "hyps": hyps, "pruned": [], "residual": residual,
            "detail": {"retrieved": len(retrieved), "used": used[:10], "best_similarity": round(best, 3)}}
