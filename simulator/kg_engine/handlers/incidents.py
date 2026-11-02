"""Incident lifecycle handlers — Design/10 §7.3, §7.4.

R-1 simplification: one incident per simulator incident. The adapter emits
`incident.boundary` when a incident's last alarm has been delivered;
the boundary handler runs the label / locality / temporal derivers over the
accumulated rows and returns the resulting triples as a single delta.

Cross-incident sliding-window grouping (Design/10 §7.3 — R1-R4 over a 6-min
window) is R-3 work and intentionally absent here.
"""
from __future__ import annotations

from simulator.kg import alarm_graph, locality, temporal

from ..events import Delta, Event
from ..state import StateStore


def handle_incident_boundary(event: Event, state: StateStore) -> Delta:
    incident_id: int = event.payload["incident_id"]
    inc = state.incidents.get(incident_id)
    if inc is None or not inc.alarm_rows:
        return Delta()

    info = event.payload.get("info")
    if info:
        inc.info.update(info)

    rows = inc.alarm_rows
    delta = Delta()
    delta.adds.extend(alarm_graph.alarm_label_triples(incident_id, rows,
                                                      state.topology))
    delta.adds.extend(locality.derive_locality_triples(incident_id, rows,
                                                       state.topology))
    delta.adds.extend(temporal.derive_temporal_triples(incident_id, rows,
                                                       compact=True))
    inc.closed_at = event.at
    return delta
