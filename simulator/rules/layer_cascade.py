"""Design/08 §3 — layer-cascade rules (within a single port).

Upstream→downstream layer order in the propagation sense:

    OTS -> OMS -> OCH -> OTU -> SDH/FEC/ODU/OPU -> client

When two alarms sit at the same physical port and their layers form an
upstream→downstream pair, the downstream-layer alarm is Secondary.

Two refinements: OTS_LOS and MUT_LOS dominate every other alarm at the same
port (the LOS implies the bit stream is gone, so bit-error counts on
overlying layers are meaningless).
"""
from __future__ import annotations

from .schema import Inferred

LAYER_ORDER = {
    "OTS":    0,
    "OMS":    1,
    "OCH":    2,
    "OTU":    3,
    "SDH":    4,
    "FEC":    4,
    "ODU":    4,
    "OPU":    4,
    "client": 5,
}

DOMINATING_NAMES = {"OTS_LOS", "MUT_LOS"}


def apply(alarms: list[dict]) -> list[Inferred]:
    out: list[Inferred] = []
    # Group by port so we don't do N²: at most ~10 alarms per port.
    by_port: dict[str, list[dict]] = {}
    for r in alarms:
        p = r.get("port") or ""
        if not p:
            continue
        by_port.setdefault(p, []).append(r)

    for port_id, group in by_port.items():
        # Pass 1: dominating alarms (OTS_LOS / MUT_LOS) suppress everything else.
        dominators = [r for r in group if r["alarm_name"] in DOMINATING_NAMES]
        if dominators:
            dom = dominators[0]   # if multiple, pick one (deterministic by id)
            for r in group:
                if r["id"] == dom["id"]:
                    continue
                out.append(Inferred("Secondary", r["id"], dom["id"],
                                    source=f"§3.dominates.{dom['alarm_name']}"))
            continue

        # Pass 2: layer-order cascade.
        for i, a1 in enumerate(group):
            for a2 in group[i + 1:]:
                l1 = LAYER_ORDER.get(a1.get("layer") or "")
                l2 = LAYER_ORDER.get(a2.get("layer") or "")
                if l1 is None or l2 is None or l1 == l2:
                    continue
                if l1 < l2:
                    parent, child = a1, a2
                else:
                    parent, child = a2, a1
                out.append(Inferred("Secondary", child["id"], parent["id"],
                                    source="§3.layer_cascade"))
    return out
