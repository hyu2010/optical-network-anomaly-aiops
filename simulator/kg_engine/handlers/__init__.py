"""Handler dispatch table — kind -> Callable(event, state) -> Delta.

R-1 wires `alarm.raise` and `incident.boundary`. Topology and alarm.clear /
alarm.update handlers are R-2 / R-3 work and are intentionally absent so an
unhandled event becomes a debug log (not an error).
"""
from __future__ import annotations

from typing import Callable

from ..events import Delta, Event, EventKind
from ..state import StateStore
from . import alarms, incidents, ppe, topology
from ..subscribers.rule_engine import RULE_ROOT_PICK, handle_rule_root_pick


def default_handlers() -> dict[str, Callable[[Event, StateStore], Delta]]:
    return {
        EventKind.ALARM_RAISE:         alarms.handle_alarm_raise,
        EventKind.ALARM_CLEAR:         alarms.handle_alarm_clear,
        EventKind.ALARM_UPDATE:        alarms.handle_alarm_update,
        EventKind.INCIDENT_BOUNDARY:   incidents.handle_incident_boundary,
        EventKind.PPE_UPDATE:          ppe.handle_ppe_update,
        EventKind.TOPOLOGY_SNAPSHOT:   topology.handle_topology_snapshot,
        # Granular topology events are R-5 — registered as no-op stubs so
        # an unhandled message becomes an empty Delta, not a missing handler.
        EventKind.TOPOLOGY_ADD_NE:       topology.handle_topology_change_stub,
        EventKind.TOPOLOGY_REMOVE_NE:    topology.handle_topology_change_stub,
        EventKind.TOPOLOGY_ADD_BOARD:    topology.handle_topology_change_stub,
        EventKind.TOPOLOGY_REMOVE_BOARD: topology.handle_topology_change_stub,
        EventKind.TOPOLOGY_ADD_PORT:     topology.handle_topology_change_stub,
        EventKind.TOPOLOGY_ADD_SPAN:     topology.handle_topology_change_stub,
        EventKind.TOPOLOGY_REMOVE_SPAN:  topology.handle_topology_change_stub,
        EventKind.TOPOLOGY_ADD_TUNNEL:   topology.handle_topology_change_stub,
        EventKind.TOPOLOGY_REMOVE_TUNNEL: topology.handle_topology_change_stub,
        # R-3 rule-engine root picks
        RULE_ROOT_PICK:                  handle_rule_root_pick,
    }
