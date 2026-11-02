"""Stage 3b — temporal predicates: Close (≤5 min), WithinSix (≤6 min), Earlier.

Design/09 §4 Stage 3 notes that for simulator-generated cascades the entire
alarm set is typically within the 5-minute window, so emitting N² pairwise
edges is wasteful. Default mode emits a compact per-incident flag instead:

    incident:{sid}   AllClose       true
    incident:{sid}   AllWithinSix   true

Set `compact=False` to materialise pairwise Close / WithinSix / Earlier edges
when the polling-jitter window matters as a per-edge feature.

Time source: prefer `t_observed_ms` (post poll-jitter — what the NMS sees),
fall back to `t_offset_ms` for legacy rows.
"""
from __future__ import annotations
from itertools import combinations

from .schema import (
    Triple, EARLIER, CLOSE, WITHIN_SIX, ALL_CLOSE, ALL_WITHIN_SIX, alarm_uri,
)

CLOSE_MS      = 5 * 60 * 1000
WITHIN_SIX_MS = 6 * 60 * 1000


def _time_of(r: dict) -> int:
    t = r.get("t_observed_ms")
    if t is None:
        t = r.get("t_offset_ms") or 0
    return int(t)


def derive_temporal_triples(incident_id: int, rows: list[dict],
                            compact: bool = True) -> list[Triple]:
    if not rows:
        return []

    if compact:
        ts = [_time_of(r) for r in rows]
        spread = max(ts) - min(ts)
        out: list[Triple] = []
        sce = f"incident:{incident_id}"
        if spread <= CLOSE_MS:
            out.append(Triple(sce, ALL_CLOSE, "true", "derived", 1.0, incident_id))
        if spread <= WITHIN_SIX_MS:
            out.append(Triple(sce, ALL_WITHIN_SIX, "true", "derived", 1.0, incident_id))
        return out

    times = [(r["id"], _time_of(r)) for r in rows]
    out = []
    for (id_a, t_a), (id_b, t_b) in combinations(times, 2):
        au = alarm_uri(incident_id, id_a)
        bu = alarm_uri(incident_id, id_b)
        if t_a < t_b:
            out.append(Triple(au, EARLIER, bu, "derived", 1.0, incident_id))
        elif t_b < t_a:
            out.append(Triple(bu, EARLIER, au, "derived", 1.0, incident_id))
        d = abs(t_a - t_b)
        if d <= CLOSE_MS:
            out.append(Triple(au, CLOSE, bu, "derived", 1.0, incident_id))
        if d <= WITHIN_SIX_MS:
            out.append(Triple(au, WITHIN_SIX, bu, "derived", 1.0, incident_id))
    return out
