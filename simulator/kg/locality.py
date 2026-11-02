"""Stage 3a — derived locality predicates between alarms in one incident.

Pairwise edges: OnSameNE, OnSameBoard, OnSamePort, OnSameContainer, SameSpan,
OnSameLink (= ports are peer endpoints of an optical span). Emits one
directed edge per pair (a→b with a.id < b.id) to keep the triple count
manageable; downstream consumers can densify by adding the reverse edge.
"""
from __future__ import annotations
from itertools import combinations

from simulator.simulator.topology import Topology

from .schema import (
    Triple,
    ON_SAME_NE, ON_SAME_BOARD, ON_SAME_PORT, ON_SAME_CONTAINER,
    SAME_SPAN, ON_SAME_LINK,
    alarm_uri, classify_container,
)


def _peer_port_map(topo: Topology) -> dict[str, str]:
    """inventory port uuid -> peer inventory port uuid (other end of the span)."""
    out: dict[str, str] = {}
    for link in topo.optical_links.values():
        s = topo._find_tp("optical-topo", link.from_ne, link.from_tp)
        d = topo._find_tp("optical-topo", link.to_ne,   link.to_tp)
        if s and d and s.inventory_id and d.inventory_id:
            out[s.inventory_id] = d.inventory_id
            out[d.inventory_id] = s.inventory_id
    return out


def derive_locality_triples(incident_id: int, rows: list[dict],
                            topo: Topology) -> list[Triple]:
    out: list[Triple] = []
    peer_of = _peer_port_map(topo)

    for a, b in combinations(rows, 2):
        au = alarm_uri(incident_id, a["id"])
        bu = alarm_uri(incident_id, b["id"])
        ne1, ne2 = a.get("ne"),    b.get("ne")
        bd1, bd2 = a.get("board"), b.get("board")
        p1, p2   = a.get("port"),  b.get("port")
        c1, c2   = a.get("container_id") or "", b.get("container_id") or ""

        if ne1 and ne1 == ne2:
            out.append(Triple(au, ON_SAME_NE, bu, "derived", 1.0, incident_id))
        if bd1 and bd1 == bd2:
            out.append(Triple(au, ON_SAME_BOARD, bu, "derived", 1.0, incident_id))
        if p1 and p1 == p2:
            out.append(Triple(au, ON_SAME_PORT, bu, "derived", 1.0, incident_id))
        if c1 and c1 == c2:
            out.append(Triple(au, ON_SAME_CONTAINER, bu, "derived", 1.0, incident_id))
            if classify_container(c1, topo) == "span":
                out.append(Triple(au, SAME_SPAN, bu, "derived", 1.0, incident_id))
        if p1 and p2 and peer_of.get(p1) == p2:
            out.append(Triple(au, ON_SAME_LINK, bu, "derived", 1.0, incident_id))

    return out
