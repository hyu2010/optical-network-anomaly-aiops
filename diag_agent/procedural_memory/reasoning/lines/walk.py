"""Abduction walk over the KG (doc 04 §5.1).

From every frame symptom, walk the KB backwards: reverse cascade rules give
virtual upstream symptoms, reverse effect roles give candidate fault
locations, and documented modes at those locations are scored by preview.
"""

from __future__ import annotations

from typing import Any

MAX_CANDIDATES = 25
MAX_PRUNED = 8


def _virtual_items(sem, items: list[tuple[str, str, str]], depth: int) -> list[tuple[str, str, str]]:
    out = list(items)
    seen = set(items)
    frontier = list(items)
    for _ in range(depth):
        nxt = []
        for cond, res, layer in frontier:
            for r in sem.cascade_rules():
                if r["to_layer"] != layer or not sem.cond_subsumes(r["to_condition"], cond):
                    continue
                for a in sem.walk_reverse(r["pattern"], res):
                    v = (r["from_condition"], a, r["from_layer"])
                    if v not in seen:
                        seen.add(v)
                        nxt.append(v)
        out += nxt
        frontier = nxt
    return out


def candidate_locations(sem, frame: dict, *, depth: int = 3, wide: bool = False) -> tuple[set[tuple[str, str]], set[str]]:
    seeds = []
    for it in frame["items"]:
        if not it["relevant"]:
            continue
        for lay in it["layers"] or [it["layer"]]:
            seeds.append((it["condition"], it["resource"], lay))
    for x in frame.get("ppe", []):
        seeds.append(({"step": "ProfileStep", "slope": "ProfileSlope", "rx_offset": "RxOffset", "null": "ProfileNull",
                       "clean": "ProfileClean"}.get(x["kind"], "ProfileClean"), x["och"], "PPE"))
    virtual = _virtual_items(sem, seeds, depth)
    visited: set[str] = set()
    cands: set[tuple[str, str]] = set()
    for cond, res, _layer in virtual:
        visited.add(res)
        for mid, m in sem.modes().items():
            for eff in m["effects"]:
                if not sem.cond_subsumes(eff["condition"], cond):
                    continue
                for loc in sem.role_reverse(eff["role"], res):
                    visited.add(loc)
                    if m["applies_to"] in sem.classes_of(loc):
                        cands.add((mid, loc))
    if wide:
        for it in frame["items"]:
            for term in ("this_unit", "this_ne", "upstream_ne", "upstream_tx_unit", "upstream_amplifier"):
                for loc in sem.resolve_location(term, it["resource"]):
                    visited.add(loc)
                    for mid in sem.documented_modes(loc):
                        cands.add((mid, loc))
        for e in frame.get("shared", []):
            visited.add(e["resource"])
            for mid in sem.documented_modes(e["resource"]):
                cands.add((mid, e["resource"]))
    return cands, visited


def run(sem, evaluator, frame: dict, ctx: dict, *, pass_no: int = 0) -> dict[str, Any]:
    depth = 3 * (2 ** pass_no)
    cands, visited = candidate_locations(sem, frame, depth=min(depth, 12), wide=pass_no > 0)
    scored = [evaluator.preview(m, loc, ctx) for m, loc in sorted(cands)]
    pos = sorted([s for s in scored if s["score"] > 0], key=lambda s: (-s["score"], s["mode"], s["location"]))
    neg = sorted([s for s in scored if s["score"] <= 0], key=lambda s: (-s["score"], s["mode"], s["location"]))
    pos = pos[:MAX_CANDIDATES]
    if not pos:
        return {"line": "walk", "status": "abstain", "dist": {}, "residual": 1.0, "hyps": {}, "visited": sorted(visited),
                "pruned": [{"key": f"{s['mode']}@{s['location']}", "reason": s["reason"], "score": s["score"]}
                           for s in neg[:MAX_PRUNED]], "detail": {"candidates": len(scored)}}
    best = pos[0]
    share = len(best["unexplained"]) / max(best["n_relevant"], 1)
    residual = min(0.5, 0.05 + 0.4 * share)
    z = sum(s["score"] for s in pos)
    dist = {f"{s['mode']}@{s['location']}": (1 - residual) * s["score"] / z for s in pos}
    hyps = {f"{s['mode']}@{s['location']}": {"mode": s["mode"], "location": s["location"],
                                             "provenance": {"walk": {"score": s["score"], "explained": s["explained"],
                                                                     "missing": s["missing"]}}} for s in pos}
    return {"line": "walk", "status": "ok", "dist": dist, "residual": residual, "hyps": hyps, "visited": sorted(visited),
            "pruned": [{"key": f"{s['mode']}@{s['location']}", "reason": s["reason"], "score": s["score"]}
                       for s in neg[:MAX_PRUNED]],
            "detail": {"candidates": len(scored), "depth": depth, "unexplained_share": round(share, 3),
                       "scores": {f"{s['mode']}@{s['location']}": s["score"] for s in pos}}}
