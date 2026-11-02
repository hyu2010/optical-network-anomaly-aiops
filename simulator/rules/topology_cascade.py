"""Design/08 §4 — topology-grounded cascade rules.

Cross-port propagation that fires only because the topology says so:

  • Peer-port LOS: OTS_LOS at one end of a span and R_LOS at the other end of
    the same span are causally linked.
  • Supporting-link cascade: an OTS_LOS at a span causes OTU-layer alarms on
    every OTU pipe whose `supporting-link` is that span.
  • Per-tunnel propagation: ODU_PM_SSF at a client TP whose underlay OCh
    traverses the faulted span is Secondary to that span's OTS_LOS.
"""
from __future__ import annotations

from simulator.simulator.topology import Topology

from .schema import Inferred


def _peer_port_map(topo: Topology) -> dict[str, str]:
    out: dict[str, str] = {}
    for link in topo.optical_links.values():
        s = topo._find_tp("optical-topo", link.from_ne, link.from_tp)
        d = topo._find_tp("optical-topo", link.to_ne,   link.to_tp)
        if s and d and s.inventory_id and d.inventory_id:
            out[s.inventory_id] = d.inventory_id
            out[d.inventory_id] = s.inventory_id
    return out


def _otus_supported_by(topo: Topology, span_id: str) -> set[str]:
    return {l.link_id for l in topo.otn_links_supported_by(span_id)}


def _odus_traversing(topo: Topology, span_id: str) -> set[str]:
    """ODU tunnel names whose underlay OCh's ERO contains this span."""
    affected_otus = _otus_supported_by(topo, span_id)
    out = set()
    for tn in topo.otn_tunnels.values():
        if set(tn.ero) & affected_otus:
            out.add(tn.name)
    return out


def apply(alarms: list[dict], topo: Topology) -> list[Inferred]:
    out: list[Inferred] = []
    peer_of = _peer_port_map(topo)

    ots_los_alarms = [r for r in alarms
                      if r["alarm_name"] == "OTS_LOS" and r.get("container_id")]
    if not ots_los_alarms:
        return out

    # Build lookups so we don't scan the alarm list once per rule firing.
    by_port: dict[str, list[dict]] = {}
    by_container: dict[str, list[dict]] = {}
    for r in alarms:
        if r.get("port"):
            by_port.setdefault(r["port"], []).append(r)
        if r.get("container_id"):
            by_container.setdefault(r["container_id"], []).append(r)

    for ots in ots_los_alarms:
        span = ots.get("container_id") or ""
        if span not in topo.optical_links:
            continue
        ots_port = ots.get("port") or ""

        # Peer-port R_LOS at the other end of the span.
        peer = peer_of.get(ots_port)
        if peer:
            for r in by_port.get(peer, ()):
                if r["alarm_name"] == "R_LOS" and r["id"] != ots["id"]:
                    out.append(Inferred("Secondary", r["id"], ots["id"],
                                        source="§4.peer_port"))

        # Supporting-link cascade: every OTU-layer alarm on a pipe that runs
        # over this span is Secondary to the OTS_LOS.
        otu_pipes = _otus_supported_by(topo, span)
        for otu_id in otu_pipes:
            for r in by_container.get(otu_id, ()):
                if r["id"] == ots["id"]:
                    continue
                if r.get("layer") in ("OCH", "OTU"):
                    out.append(Inferred("Secondary", r["id"], ots["id"],
                                        source="§4.supporting_link"))

        # Per-tunnel: ODU_PM_SSF at any ODU whose OCh underlay traverses the span.
        odu_names = _odus_traversing(topo, span)
        for odu_id in odu_names:
            for r in by_container.get(odu_id, ()):
                if r["alarm_name"] in ("ODU_PM_SSF", "ODU_PM_BDI"):
                    out.append(Inferred("Secondary", r["id"], ots["id"],
                                        source="§4.per_tunnel"))

    return out
