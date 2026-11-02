"""Stage 1 — extract static topology triples from the YANG model.

Reads a `Topology` object (already populated by `src.simulator.yang_loader`)
and emits the entity-relationship edges per Design/09 §3.2.

Output is small and constant per topology shape; ~250 triples on the v3 lab
mesh.
"""
from __future__ import annotations

from simulator.simulator.topology import Topology

from .schema import (
    Triple,
    HOSTS, CONNECTED, CARRIED_BY, RIDES_OVER, SUPPORTING_TUNNEL, CARRIES,
    ne_uri, board_uri, port_uri, span_uri, otu_uri, och_uri, odu_uri, svc_uri,
)


def build_topology_triples(topo: Topology) -> list[Triple]:
    out: list[Triple] = []

    # Hosts: NE → Board, Board → Port
    for ne_id, ne in topo.nes.items():
        for board in ne.boards:
            out.append(Triple(ne_uri(ne_id), HOSTS, board_uri(board.uuid), "topology"))
            for port in board.ports:
                out.append(Triple(board_uri(board.uuid), HOSTS,
                                  port_uri(port.uuid), "topology"))

    # Connected: Port → Port via optical-topo links (resolve TP → inventory port)
    for link in topo.optical_links.values():
        src_tp = topo._find_tp("optical-topo", link.from_ne, link.from_tp)
        dst_tp = topo._find_tp("optical-topo", link.to_ne,   link.to_tp)
        if src_tp and dst_tp and src_tp.inventory_id and dst_tp.inventory_id:
            out.append(Triple(port_uri(src_tp.inventory_id), CONNECTED,
                              port_uri(dst_tp.inventory_id), "topology"))

    # CarriedBy: OTUPipe → Span (otn-topo link's supporting-link)
    for otn in topo.otn_links.values():
        if otn.supporting_link_id:
            out.append(Triple(otu_uri(otn.link_id), CARRIED_BY,
                              span_uri(otn.supporting_link_id), "topology"))

    # CarriedBy: OChTunnel → Span (one per ERO hop; route via supporting-link
    # when the hop is an otn-topo link rather than an optical span)
    for tunl in topo.wdm_tunnels.values():
        seen: set[str] = set()
        for hop in tunl.ero:
            span_id = None
            if hop in topo.optical_links:
                span_id = hop
            elif hop in topo.otn_links and topo.otn_links[hop].supporting_link_id:
                span_id = topo.otn_links[hop].supporting_link_id
            if span_id and span_id not in seen:
                seen.add(span_id)
                out.append(Triple(och_uri(tunl.name), CARRIED_BY,
                                  span_uri(span_id), "topology"))

    # RidesOver: ODUTunnel → OTUPipe (per ERO hop on the otn-topo layer)
    for tunl in topo.otn_tunnels.values():
        seen = set()
        for hop in tunl.ero:
            if hop in topo.otn_links and hop not in seen:
                seen.add(hop)
                out.append(Triple(odu_uri(tunl.name), RIDES_OVER,
                                  otu_uri(hop), "topology"))

    # SupportingTunnel: ODUTunnel → OChTunnel
    for tunl in topo.otn_tunnels.values():
        if tunl.supporting_tunnel and tunl.supporting_tunnel in topo.wdm_tunnels:
            out.append(Triple(odu_uri(tunl.name), SUPPORTING_TUNNEL,
                              och_uri(tunl.supporting_tunnel), "topology"))

    # Carries: ODUTunnel → Service
    for tunl in topo.otn_tunnels.values():
        if tunl.client_service_name:
            out.append(Triple(odu_uri(tunl.name), CARRIES,
                              svc_uri(tunl.client_service_name), "topology"))

    return out
