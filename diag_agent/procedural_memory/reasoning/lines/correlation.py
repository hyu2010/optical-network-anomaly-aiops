"""Alarm-correlation line (doc 04 §5.2).

The earliest root-capable symptom of the lowest layer names its documented
possible causes; locations come from the reverse roles of that symptom; a
quiet-instrument filter drops candidates whose Required effects were not
seen although their window has closed.
"""

from __future__ import annotations

from typing import Any

SET_MASS = 0.9


def run(sem, evaluator, frame: dict, ctx: dict, *, set_mass: float = SET_MASS) -> dict[str, Any]:
    anchor = next((it for it in frame["items"] if it["id"] == frame.get("anchor")), None)
    if anchor is None or not anchor["possible_causes"]:
        return {"line": "correlation", "status": "abstain", "dist": {}, "residual": 1.0, "hyps": {}, "pruned": [],
                "detail": {"reason": "no root-capable symptom"}}
    res = anchor["resource"]
    upstream = {res}
    for r in sem.cascade_rules():
        for lay in anchor["layers"]:
            if r["to_layer"] == lay and sem.cond_subsumes(r["to_condition"], anchor["condition"]):
                upstream.update(sem.walk_reverse(r["pattern"], res))
    cands = set()
    for mid in anchor["possible_causes"]:
        m = sem.mode(mid)
        for eff in m["effects"]:
            if not sem.cond_subsumes(eff["condition"], anchor["condition"]):
                continue
            for r0 in upstream:
                for loc in sem.role_reverse(eff["role"], r0):
                    if m["applies_to"] in sem.classes_of(loc):
                        cands.add((mid, loc))
    kept, pruned = [], []
    for mid, loc in sorted(cands):
        pv = evaluator.preview(mid, loc, ctx)
        ev = evaluator.evaluate("Hx", mid, loc, ctx)
        req_missing = [s for s in ev["states"].values() if s["strength"] == "Required" and s["layer"] != "PPE"
                       and s["state"] in ("MISSING", "CONTRADICTED")]
        if req_missing:
            s = req_missing[0]
            pruned.append({"key": f"{mid}@{loc}", "reason": f"quiet instrument: {s['condition']} @ {s['resource']}"})
        else:
            kept.append((mid, loc, pv))
    if not kept:
        return {"line": "correlation", "status": "abstain", "dist": {}, "residual": 1.0, "hyps": {}, "pruned": pruned,
                "detail": {"anchor": anchor["id"], "causes": anchor["possible_causes"]}}
    each = set_mass / len(kept)
    dist = {f"{m}@{l}": each for m, l, _ in kept}
    hyps = {f"{m}@{l}": {"mode": m, "location": l, "provenance": {"correlation": {"anchor": anchor["id"],
                                                                                  "alarms": anchor["root_names"]}}}
            for m, l, _ in kept}
    return {"line": "correlation", "status": "ok", "dist": dist, "residual": 1 - set_mass, "hyps": hyps,
            "pruned": pruned, "detail": {"anchor": anchor["id"], "causes": anchor["possible_causes"]}}
