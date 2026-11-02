"""In-memory state store — entity registry + triple index.

Design/10 §6. R-1 uses only `alarms`, `incidents`, and a `topology` reference
loaded once from `src.simulator.yang_loader`; the other entity registries are
declared so R-2 can fill them in without restructuring.

The `TripleIndex` uses sets for membership and lists for the three by-* indexes.
Idempotent add/remove (Design/10 §9 — at-least-once → handlers must be
idempotent → duplicate adds are no-ops).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from simulator.kg.schema import Triple
from simulator.simulator.topology import Topology


# ---------------------------------------------------------------------------
# Incident — the streaming counterpart to a batch `incident`.
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Incident:
    """One root-cause grouping of alarms.

    In R-1 `incident_id` is the simulator's incident_id (1:1 mapping).
    R-3 will introduce ULID-keyed incidents created by sliding-window grouping.
    """
    incident_id: int
    opened_at: float                            # epoch ms
    closed_at: float | None = None
    alarm_rows: list[dict] = field(default_factory=list)
    # `alarm_rows` keeps the raw dict per alarm for end-of-incident derivers
    # (locality + temporal need the full row, not just the URI).
    info: dict = field(default_factory=dict)
    # `info` carries adapter-supplied incident metadata (fault_type,
    # fault_target, target_kind, suppressed, topology_id) so subscribers
    # can render a manifest row at incident.boundary.


# ---------------------------------------------------------------------------
# Triple index
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class TripleIndex:
    by_subject:   dict[str, list[Triple]] = field(default_factory=lambda: defaultdict(list))
    by_object:    dict[str, list[Triple]] = field(default_factory=lambda: defaultdict(list))
    by_predicate: dict[str, list[Triple]] = field(default_factory=lambda: defaultdict(list))
    by_incident:  dict[int, list[Triple]] = field(default_factory=lambda: defaultdict(list))
    all_triples:  set[Triple] = field(default_factory=set)

    def add(self, t: Triple) -> bool:
        """Add a triple; return True if newly added, False if duplicate."""
        if t in self.all_triples:
            return False
        self.all_triples.add(t)
        self.by_subject[t.subject].append(t)
        self.by_object[t.object].append(t)
        self.by_predicate[t.predicate].append(t)
        if t.incident_id:
            self.by_incident[t.incident_id].append(t)
        return True

    def remove(self, t: Triple) -> bool:
        """Remove a triple; return True if it was present, False otherwise."""
        if t not in self.all_triples:
            return False
        self.all_triples.discard(t)
        # The by-* lists may contain the triple; remove first occurrence.
        for index, key in (
            (self.by_subject, t.subject),
            (self.by_object, t.object),
            (self.by_predicate, t.predicate),
        ):
            try:
                index[key].remove(t)
            except ValueError:
                pass
        if t.incident_id:
            try:
                self.by_incident[t.incident_id].remove(t)
            except ValueError:
                pass
        return True

    def drop_incident(self, incident_id: int) -> int:
        """Drop every triple bound to an incident. Returns count removed.

        Used at incident closure (Design/10 §7.4) to keep live memory bounded.
        """
        triples = list(self.by_incident.pop(incident_id, ()))
        for t in triples:
            self.remove(t)
        return len(triples)


# ---------------------------------------------------------------------------
# State store
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class StateStore:
    topology: Topology
    triples: TripleIndex = field(default_factory=TripleIndex)
    incidents: dict[int, Incident] = field(default_factory=dict)
    # R-2 fills these from topology events; R-1 leaves them empty and
    # relies on the loaded `topology` object for all topology questions.
    nes: dict[str, Any] = field(default_factory=dict)
    boards: dict[str, Any] = field(default_factory=dict)
    ports: dict[str, Any] = field(default_factory=dict)
    spans: dict[str, Any] = field(default_factory=dict)
    otu_pipes: dict[str, Any] = field(default_factory=dict)
    och_tunnels: dict[str, Any] = field(default_factory=dict)
    odu_tunnels: dict[str, Any] = field(default_factory=dict)
    services: dict[str, Any] = field(default_factory=dict)
    # Last event_id whose Delta was applied — used by snapshot/restore
    # (Design/10 §6.3 recovery sequence: load snapshot, replay events with
    # event_id > last_event_id).
    last_event_id: str = ""
    # ULID-ish topology fingerprint, bumped on each topology.snapshot apply.
    # Used by subscribers (e.g. SSE) to invalidate cached views.
    topology_version: int = 0
