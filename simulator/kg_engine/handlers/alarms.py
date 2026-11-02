"""Alarm event handlers — Design/10 §7.2.

`alarm.raise` payload schema:
    incident_id : int   (optional — when present, incident-bounded mode;
                          when absent, the grouper in handlers/grouping.py
                          picks the incident under R1-R4)
    row         : dict  (the SQLite alarms row — see src/simulator/db.py)

`alarm.clear` and `alarm.update` are still stubbed; the lifecycle around
clears is the rest of R-3 / R-5 work.
"""
from __future__ import annotations

from simulator.kg import alarm_graph

from ..events import Delta, Event
from ..state import Incident, StateStore
from .grouping import assign_incident


def handle_alarm_raise(event: Event, state: StateStore) -> Delta:
    row: dict = event.payload["row"]
    incident_id = event.payload.get("incident_id")

    if incident_id is None:
        # Sliding-window mode — let the grouper decide.
        incident_id = assign_incident(state, row, event.at)

    inc = state.incidents.get(incident_id)
    if inc is None:
        inc = Incident(incident_id=incident_id, opened_at=event.at)
        state.incidents[incident_id] = inc
    inc.alarm_rows.append(row)

    return Delta(adds=alarm_graph.alarm_location_triples(incident_id, row,
                                                          state.topology))


def handle_alarm_clear(event: Event, state: StateStore) -> Delta:
    """R-3+ stub — alarm-lifecycle handling for live NMS streams."""
    return Delta()


def handle_alarm_update(event: Event, state: StateStore) -> Delta:
    """R-3+ stub — severity / location refinement for live NMS streams."""
    return Delta()
