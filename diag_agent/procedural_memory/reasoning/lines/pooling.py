"""Linear opinion pool of the generation lines (doc 04 §5.5).

prior(h) = sum_l w_l p_l(h) / sum_l w_l over the lines that did not abstain;
NOTA receives the weighted residuals.  Hypotheses below `floor` are folded
into NOTA, and at most `max_hyps` are kept.
"""

from __future__ import annotations

from typing import Any

FLOOR = 0.01
MAX_HYPS = 12


def pool(results: dict[str, dict], weights: dict[str, float], *, existing: dict[str, dict] | None = None,
         floor: float = FLOOR, max_hyps: int = MAX_HYPS) -> dict[str, Any]:
    active = {l: r for l, r in results.items() if r.get("status") == "ok"}
    wsum = sum(weights.get(l, 1.0) for l in active)
    mass: dict[str, float] = {}
    info: dict[str, dict] = {}
    nota = 0.0
    if wsum <= 0:
        return {"prior": {"NOTA": 1.0}, "hyps": {}, "lines": {}, "folded": []}
    for l, r in active.items():
        w = weights.get(l, 1.0) / wsum
        nota += w * r["residual"]
        for key, p in r["dist"].items():
            mass[key] = mass.get(key, 0.0) + w * p
            h = info.setdefault(key, {"mode": r["hyps"][key]["mode"], "location": r["hyps"][key]["location"],
                                      "lines": {}, "provenance": {}})
            h["lines"][l] = p
            h["provenance"].update(r["hyps"][key].get("provenance", {}))
    for key, h in (existing or {}).items():
        if key not in mass:
            mass[key] = 0.0
            info[key] = {"mode": h["mode"], "location": h["location"], "lines": {}, "provenance": h.get("provenance", {})}
    ranked = sorted(mass, key=lambda k: (-mass[k], k))
    keep, folded = [], []
    for k in ranked:
        if len(keep) < max_hyps and (mass[k] >= floor / 2 or k in (existing or {})):
            keep.append(k)
        else:
            folded.append(k)
            nota += mass[k]
    prior = {k: max(mass[k], floor / 4) for k in keep}
    z = sum(prior.values()) + nota
    prior = {k: v / z for k, v in prior.items()}
    prior["NOTA"] = nota / z
    return {"prior": prior, "hyps": {k: info[k] for k in keep}, "folded": folded,
            "weights": {l: weights.get(l, 1.0) / wsum for l in active},
            "abstained": sorted(l for l in results if l not in active)}
