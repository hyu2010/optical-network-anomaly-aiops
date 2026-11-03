"""Prediction propagator (doc 04 §6.1).

A hypothesis (mode at location) is expanded into located, layered and timed
predictions: the mode's primary effects resolved through their roles, then
closed under the signal-cascade rules.  Same-location suppression children
and cause-keyword alarms are not predictions; they are handled by the
explanation rule (`explains`).
"""

from __future__ import annotations

from typing import Any

STRENGTH_RANK = {"Required": 0, "Expected": 1, "Possible": 2}
SERVICE_CONDS = {"ServiceDown", "ServiceDegraded"}
PPE_CONDS = {"ProfileStep", "ProfileSlope", "ProfileNull", "RxOffset", "ProfileClean"}


def _weaker(a: str, b: str) -> str:
    return a if STRENGTH_RANK[a] >= STRENGTH_RANK[b] else b


def _layer_for_port(sem, layer: str, condition: str, resource: str) -> str:
    """Loss of light at an OTU line receiver is reported in the OCh layer."""
    if layer == "OTS" and condition == "LossOfSignal" and sem.kind_of(resource) == "port":
        board = sem.board_of_port(resource)
        if board and sem.is_a(board, "OTUBoard"):
            return "OCH"
    return layer


def ppe_spans(sem, location: str, och: str) -> list[str]:
    """Spans of an OCh route on which a mode at `location` places its PPE signature."""
    route = [s for _, s, _, _ in sem.hops(och)]
    k = sem.kind_of(location)
    if k == "span":
        return [s for s in route if s == location]
    if k == "section":
        return [s for s in route if sem.value(s, "partOfSection") == location]
    if k == "board":
        out = []
        for s in route:
            for amp in sem.values(s, "launchAmplifier"):
                if sem.value(amp, "amplifierBoard") == location:
                    out.append(s)
        return out
    if k == "otu":
        sup = sem.values(location, "supportedBy")
        return [s for s in route if s in sup]
    return []


def propagate(sem, hid: str, mode_id: str, location: str) -> dict[str, Any]:
    mode = sem.mode(mode_id)
    preds: dict[tuple, dict] = {}
    keyword_res: set[str] = set()
    dead: set[str] = set()

    def add(cond, res, layer, strength, delay, source, *, no_cascade=False, signature=None, rule=None):
        key = (cond, res, layer)
        cur = preds.get(key)
        if cur is not None:
            if STRENGTH_RANK[strength] < STRENGTH_RANK[cur["strength"]]:
                cur["strength"] = strength
            cur["delay_ms"] = min(cur["delay_ms"], delay)
            return None
        p = {"condition": cond, "resource": res, "layer": layer, "strength": strength, "delay_ms": int(delay),
             "source": source, "rule": rule, "no_cascade": no_cascade, "signature": signature,
             "ppe": cond in PPE_CONDS, "service": cond in SERVICE_CONDS}
        if p["ppe"]:
            p["spans"] = ppe_spans(sem, location, res)
        preds[key] = p
        return p

    queue = []
    for eff in mode["effects"]:
        targets = sem.role_walk(eff["role"], location)
        for t in targets:
            layer = _layer_for_port(sem, eff["layer"], eff["condition"], t)
            p = add(eff["condition"], t, layer, eff["strength"], eff["delay_ms"], "effect",
                    no_cascade=eff["no_cascade"], signature=eff["signature"], rule=eff["role"])
            if eff["keyword_anchor"]:
                keyword_res.add(t)
            if eff["condition"] == "EquipmentDown":
                dead.add(t)
            if p is not None and not eff["no_cascade"] and not p["ppe"]:
                queue.append(p)

    rules = sem.cascade_rules()
    while queue:
        p = queue.pop(0)
        for r in rules:
            if r["from_layer"] != p["layer"] or not sem.cond_subsumes(r["from_condition"], p["condition"]):
                continue
            for t in sem.walk(r["pattern"], p["resource"]):
                q = add(r["to_condition"], t, r["to_layer"], _weaker(p["strength"], r["transfer"]),
                        p["delay_ms"] + r["add_delay_ms"], "cascade", rule=r["id"])
                if q is not None:
                    queue.append(q)

    out = []
    for i, p in enumerate(sorted(preds.values(), key=lambda p: (p["delay_ms"], STRENGTH_RANK[p["strength"]],
                                                                 p["layer"], p["resource"], p["condition"]))):
        p["id"] = f"{hid}.p{i + 1}"
        p["hyp"] = hid
        out.append(p)

    dead_closure = set(dead)
    for d in dead:
        if sem.kind_of(d) == "ne":
            for b in sem.values(d, "hostsBoard"):
                dead_closure.add(b)
    return {"predictions": out, "keyword_resources": sorted(keyword_res), "keywords": mode["keywords"],
            "dead": sorted(dead_closure)}


def instrument_alive(sem, resource: str, dead: set[str]) -> bool:
    if not dead:
        return True
    kind = sem.kind_of(resource)
    if kind == "port":
        b = sem.board_of_port(resource)
        ne = sem.hosting_ne(resource)
        return not (resource in dead or b in dead or ne in dead)
    return True
