"""Sliding-window incident grouping — Design/10 §7.3.

Decides which open incident a new alarm joins, per the R1-R4 conditions:

  R1: within 6 minutes of any alarm in the incident (time clustering)
  R2: same OTU pipe as an existing OTU-layer alarm
  R3: same client-service-name as an existing ODU-layer alarm
  R4: carrying OCh shares a span with an existing OCh-touching alarm

If multiple incidents match, the oldest is picked (deterministic). If none
match, a new incident_id is allocated and returned.

This module is used in `--sliding-window` mode only; incident-bounded mode
(R-1 default) skips it and uses the simulator-supplied incident_id directly.
"""
from __future__ import annotations

import itertools

from simulator.simulator.topology import Topology

from ..state import Incident, StateStore

# 6-minute R1 window in milliseconds (matches src.kg.temporal.WITHIN_SIX_MS)
WITHIN_SIX_MS = 6 * 60 * 1000

# Monotonically-allocated incident ids start at 1_000_000_000 so they don't
# collide with simulator incident_ids (which start at 1) in mixed event logs.
_INCIDENT_SEQ = itertools.count(1_000_000_000)


def _next_incident_id() -> int:
    return next(_INCIDENT_SEQ)


def _och_for_alarm(row: dict, topo: Topology) -> str | None:
    """Resolve an alarm to its carrying OCh tunnel name, or None.

    Mirrors `src.kg.correlation._alarm_och`. ODU alarms carry their ODU's
    `supporting_tunnel`; direct OCh-tunnel-tagged alarms return self.
    """
    c = row.get("container_id") or ""
    if c in topo.wdm_tunnels:
        return c
    if c in topo.otn_tunnels:
        return topo.otn_tunnels[c].supporting_tunnel or None
    return None


def _och_spans(topo: Topology, och: str) -> set[str]:
    out: set[str] = set()
    tn = topo.wdm_tunnels.get(och)
    if not tn:
        return out
    for hop in tn.ero:
        if hop in topo.optical_links:
            out.add(hop)
        elif hop in topo.otn_links and topo.otn_links[hop].supporting_link_id:
            out.add(topo.otn_links[hop].supporting_link_id)
    return out


def _alarm_time_ms(row: dict) -> int:
    t = row.get("t_observed_ms")
    if t is None:
        t = row.get("t_offset_ms") or 0
    return int(t)


def _odu_client_service(row: dict, topo: Topology) -> str | None:
    c = row.get("container_id") or ""
    if c in topo.otn_tunnels:
        return topo.otn_tunnels[c].client_service_name or None
    return None


def _matches(incident: Incident, new_row: dict, topo: Topology) -> bool:
    """True if the new alarm joins this incident under R1-R4."""
    new_t = _alarm_time_ms(new_row)
    new_layer = new_row.get("layer") or ""
    new_container = new_row.get("container_id") or ""
    new_och = _och_for_alarm(new_row, topo) if new_layer in ("OCH", "ODU") else None
    new_och_spans = _och_spans(topo, new_och) if new_och else set()
    new_svc = _odu_client_service(new_row, topo) if new_layer == "ODU" else None

    for r in incident.alarm_rows:
        # R1 — time clustering against any member of the incident.
        if abs(_alarm_time_ms(r) - new_t) <= WITHIN_SIX_MS:
            # Also need any of R2/R3/R4 OR another time-cluster member —
            # R1 alone is weight 0.1, which is below the SameIncident
            # threshold of 0.5 in Design/08 §10. We require co-membership
            # via R2/R3/R4 here; R1 by itself does NOT merge incidents.
            r_layer = r.get("layer") or ""
            r_container = r.get("container_id") or ""

            # R2 — OTU + OCH sharing the OTU pipe.
            if ({new_layer, r_layer} == {"OTU", "OCH"}
                    and new_container and r_container
                    and (new_container == r_container)):
                return True

            # R3 — two ODUs with the same client service.
            if (new_layer == "ODU" and r_layer == "ODU"
                    and new_svc and new_svc == _odu_client_service(r, topo)):
                return True

            # R4 — two OCh-touching alarms whose OCh paths share a span.
            if new_och and r_layer in ("OCH", "ODU"):
                r_och = _och_for_alarm(r, topo)
                if r_och and r_och != new_och:
                    if new_och_spans & _och_spans(topo, r_och):
                        return True

            # Same container — strong locality signal, treat as same incident.
            if new_container and new_container == r_container:
                return True
    return False


def assign_incident(state: StateStore, new_row: dict, at_ms: float) -> int:
    """Pick the incident this alarm joins; create one if none matches.

    Returns the chosen `incident_id`. Mutates `state.incidents` only when a
    new incident is created (handlers add the row themselves).
    """
    matches: list[Incident] = []
    for inc in state.incidents.values():
        if inc.closed_at is not None:
            continue
        if _matches(inc, new_row, state.topology):
            matches.append(inc)

    if matches:
        matches.sort(key=lambda i: i.opened_at)
        return matches[0].incident_id

    incident_id = _next_incident_id()
    state.incidents[incident_id] = Incident(incident_id=incident_id,
                                            opened_at=at_ms)
    return incident_id


def close_stale_incidents(state: StateStore, now_ms: float) -> list[int]:
    """Return ids of incidents whose youngest alarm is older than the
    R1 window. Caller emits incident.boundary for each.

    Per Design/10 §7.4 — runs from a background tick every ~30 s.
    """
    closed: list[int] = []
    for inc_id, inc in state.incidents.items():
        if inc.closed_at is not None or not inc.alarm_rows:
            continue
        youngest_ms = max(_alarm_time_ms(r) for r in inc.alarm_rows)
        if (now_ms - youngest_ms) > WITHIN_SIX_MS:
            closed.append(inc_id)
    return closed
