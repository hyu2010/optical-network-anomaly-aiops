"""Topology event handlers — Design/10 §7.1.

R-2 implements `topology.snapshot` only — the watcher reloads the whole
YANG bundle on any file change and we diff the resulting triple sets.
Granular `topology.add_*` / `topology.remove_*` are R-5 territory (when an
actual controller can produce per-entity events).

The diff is intentionally simple: compute the new topology's full triple
set, subtract the old set for removes, subtract the other way for adds.
At v3 lab-mesh scale (~250 topology triples) this is cheap and correct.
"""
from __future__ import annotations

from simulator.kg import topology_graph
from simulator.simulator.topology import Topology

from ..events import Delta, Event
from ..state import StateStore


def handle_topology_snapshot(event: Event, state: StateStore) -> Delta:
    """Replace the current topology with the one in the event payload.

    Payload schema:
        topology: src.simulator.topology.Topology    (the new state)

    Emits adds for every topology triple in the new set that isn't already
    in the live index, and removes for every currently-indexed topology
    triple that isn't in the new set. Bumps `state.topology_version` so
    subscribers can invalidate cached projections.

    Note: `old` is read from the index (triples whose `source == "topology"`)
    rather than from `state.topology`. This is what makes the very first
    snapshot emit *all* topology triples (the index starts empty), and what
    keeps subsequent snapshots correct after the topology drifts.
    """
    new_topo: Topology = event.payload["topology"]
    old_triples = {t for t in state.triples.all_triples if t.source == "topology"}
    new_triples = set(topology_graph.build_topology_triples(new_topo))

    delta = Delta(
        adds=list(new_triples - old_triples),
        removes=list(old_triples - new_triples),
    )
    state.topology = new_topo
    state.topology_version += 1
    return delta


def handle_topology_change_stub(event: Event, state: StateStore) -> Delta:
    """Stub for granular topology.add_*/remove_* events — R-5 work.

    Routed here so unhandled granular events become a no-op delta instead
    of a missing-handler debug log.
    """
    return Delta()
