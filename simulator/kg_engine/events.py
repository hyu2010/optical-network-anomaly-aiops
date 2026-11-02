"""Event, Delta, EventKind, and source constants — the engine's lingua franca.

`Triple` is imported verbatim from `src.kg.schema` so streaming output is
type-identical to batch output (Design/10 §14 — schema compatibility).

Source provenance (Design/10 §16, Q5 decision):

  - SOURCE_ALARM            alarm location facts (IsLayer/AtNE/AtSpan/…) read
                            from the alarms row (no root-cause ground truth —
                            RootCause/Secondary are rule-derived, source="rule")
  - SOURCE_TOPOLOGY         topology triples (entity hierarchy + connectivity)
  - SOURCE_DERIVED          locality + temporal predicates computed by the engine
  - SOURCE_RULE_FINGERPRINT rule engine's RootCause pick (R-3+)
  - SOURCE_RULE_R{1..4}     weighted correlation edges (R-3+)
  - SOURCE_MODEL_RGCN       GNN inference output (future)

R-1 only emits SOURCE_ALARM and SOURCE_DERIVED; the rest are reserved.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from simulator.kg.schema import Triple

__all__ = [
    "Triple", "Event", "Delta",
    "EventKind", "new_event_id",
    "SOURCE_ALARM", "SOURCE_TOPOLOGY", "SOURCE_DERIVED", "SOURCE_PPE",
    "SOURCE_RULE_FINGERPRINT", "SOURCE_RULE_R1", "SOURCE_RULE_R2",
    "SOURCE_RULE_R3", "SOURCE_RULE_R4", "SOURCE_MODEL_RGCN",
]


# ---------------------------------------------------------------------------
# Event kinds (Design/10 §4)
# ---------------------------------------------------------------------------

class EventKind:
    # Topology
    TOPOLOGY_SNAPSHOT     = "topology.snapshot"
    TOPOLOGY_ADD_NE       = "topology.add_ne"
    TOPOLOGY_REMOVE_NE    = "topology.remove_ne"
    TOPOLOGY_ADD_BOARD    = "topology.add_board"
    TOPOLOGY_REMOVE_BOARD = "topology.remove_board"
    TOPOLOGY_ADD_PORT     = "topology.add_port"
    TOPOLOGY_ADD_SPAN     = "topology.add_span"
    TOPOLOGY_REMOVE_SPAN  = "topology.remove_span"
    TOPOLOGY_ADD_TUNNEL   = "topology.add_tunnel"
    TOPOLOGY_REMOVE_TUNNEL= "topology.remove_tunnel"

    # Alarms
    ALARM_RAISE   = "alarm.raise"
    ALARM_CLEAR   = "alarm.clear"
    ALARM_UPDATE  = "alarm.update"

    # PPE (Design/12 §7.1) — one event per (channel, span, drop_ne) snapshot.
    PPE_UPDATE = "ppe.update"

    # Engine-internal
    INCIDENT_BOUNDARY = "incident.boundary"


# ---------------------------------------------------------------------------
# Source constants
# ---------------------------------------------------------------------------

SOURCE_ALARM             = "alarm"
SOURCE_TOPOLOGY          = "topology"
SOURCE_DERIVED           = "derived"
SOURCE_PPE               = "ppe"
SOURCE_RULE_FINGERPRINT  = "rule:fingerprint"
SOURCE_RULE_R1           = "rule:R1"
SOURCE_RULE_R2           = "rule:R2"
SOURCE_RULE_R3           = "rule:R3"
SOURCE_RULE_R4           = "rule:R4"
SOURCE_MODEL_RGCN        = "model:rgcn"


# ---------------------------------------------------------------------------
# Event + Delta dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Event:
    """One immutable event consumed by the engine.

    `event_id` is a sortable, unique-per-process string used for idempotency
    (replay safety) and ordering. `payload` is kind-specific — see
    Design/10 §4 for the schema per kind.
    """
    event_id: str
    kind: str
    at: float                       # ingest timestamp (epoch ms)
    payload: dict[str, Any]
    source: str                     # adapter id, e.g. "simulator", "netconf"


@dataclass(slots=True)
class Delta:
    """Triples produced by handling one event."""
    adds: list[Triple] = field(default_factory=list)
    removes: list[Triple] = field(default_factory=list)

    def extend(self, other: "Delta") -> None:
        self.adds.extend(other.adds)
        self.removes.extend(other.removes)

    def empty(self) -> bool:
        return not self.adds and not self.removes


# ---------------------------------------------------------------------------
# ULID-ish event id (sortable, unique per process, no extra deps)
# ---------------------------------------------------------------------------

def new_event_id() -> str:
    """Sortable unique id: high-resolution timestamp + random suffix.

    Not a real ULID — we don't need Crockford base32 — but the sort order is
    by wall-clock time and collisions are 1-in-2^40 per ns. Avoids pulling
    in `ulid-py` for R-1.
    """
    ns = time.time_ns()
    return f"{ns:020d}-{secrets.token_hex(5)}"
