"""YANG instance data (data/yang/*.json) -> RDF (doc 03 §3.1).

The simulator's `yang_loader` is reused purely as a JSON parser of the
published YANG instance files (doc 03 §3.1, doc 01 §5.3); no fault, cascade
or PPE generator is imported.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import quote

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

from ..namespaces import DX, ONM, OPTX, net_ns

_BOARD_CLASS = {"OTU": OPTX.OTUBoard, "OA": OPTX.OABoard, "FIU": OPTX.FIUBoard,
                "SCC": OPTX.SCCBoard, "FAN": OPTX.FanBoard}
_SPAN_RE = re.compile(r"^span_(OMS\d+)_([A-Z])([A-Z])$")


def _import_yang_loader(repo_root: Path):
    sim = str(repo_root / "simulator")
    if sim not in sys.path:
        sys.path.insert(0, sim)
    from simulator import yang_loader  # noqa: PLC0415 - parser only
    return yang_loader


def load_topology(yang_dir: Path, repo_root: Path):
    yl = _import_yang_loader(repo_root)
    return yl.load(str(yang_dir))


def topology_to_graph(topo) -> tuple[Graph, str]:
    """Return (graph, topology_id)."""
    tid = topo.topology_id()
    NET = net_ns(tid)
    g = Graph()

    def iri(yang_id: str) -> URIRef:
        return NET[quote(yang_id, safe="._-~")]

    def res(yang_id: str, cls, path: str | None = None) -> URIRef:
        node = iri(yang_id)
        g.add((node, RDF.type, cls))
        g.add((node, OPTX.yangId, Literal(yang_id)))
        if path:
            g.add((node, OPTX.yangPath, Literal(path)))
        return node

    optical = res("optical-topo", OPTX.Network)
    otn = res("otn-topo", OPTX.Network)

    # ---------------------------------------------------------------- inventory
    for ne_id, ne in topo.nes.items():
        ne_node = res(ne_id, OPTX.NetworkElement,
                      f"/inventory/network-element[uuid='{ne_id}']")
        for b in ne.boards:
            cls = _BOARD_CLASS.get(b.type, OPTX.Board)
            bn = res(b.uuid, cls, f"/inventory/network-element[uuid='{ne_id}']/component[uuid='{b.uuid}']")
            g.add((bn, OPTX.boardType, Literal(b.type)))
            g.add((ne_node, OPTX.hostsBoard, bn))
            g.add((bn, ONM.partOf, ne_node))
            if b.type == "OTU":
                g.add((bn, OPTX.ppeEnabled, Literal(bool(b.ppe_enabled))))
                g.add((bn, OPTX.ppeDeltaZm, Literal(int(b.ppe_delta_z_m))))
                g.add((bn, OPTX.ppePollMs, Literal(int(b.ppe_poll_interval_ms))))
            for p in b.ports:
                pn = res(p.uuid, OPTX.Port)
                g.add((bn, OPTX.hasPort, pn))
                g.add((pn, ONM.partOf, bn))

    def tp_port(network: str, ne_id: str, tp_id: str) -> URIRef | None:
        tp = topo._find_tp(network, ne_id, tp_id)
        if tp is None or not tp.inventory_id:
            return None
        return iri(tp.inventory_id)

    def board_of(ne_id: str, btype: str) -> URIRef | None:
        ne = topo.nes.get(ne_id)
        if not ne:
            return None
        for b in ne.boards:
            if b.type == btype:
                return iri(b.uuid)
        return None

    # ---------------------------------------------------------------- optical spans
    for link_id, l in topo.optical_links.items():
        s = res(link_id, OPTX.OmsSpan,
                f"/ietf-network:networks/network[network-id='optical-topo']/ietf-network-topology:link[link-id='{link_id}']")
        g.add((s, OPTX.inNetwork, optical))
        g.add((s, OPTX.sourceNE, iri(l.from_ne)))
        g.add((s, OPTX.destNE, iri(l.to_ne)))
        tx, rx = tp_port("optical-topo", l.from_ne, l.from_tp), tp_port("optical-topo", l.to_ne, l.to_tp)
        if tx is not None:
            g.add((s, OPTX.txPort, tx))
        if rx is not None:
            g.add((s, OPTX.rxPort, rx))
        g.add((s, OPTX.lengthKm, Literal(float(l.length_km), datatype=XSD.decimal)))
        g.add((s, OPTX.spanCount, Literal(int(l.span_count))))
        g.add((s, OPTX.alphaDbPerKm, Literal(float(l.alpha_db_per_km), datatype=XSD.decimal)))
        g.add((s, OPTX.fibreType, Literal(l.fibre_type)))
        m = _SPAN_RE.match(link_id)
        if m:
            sec_id, a, b = m.groups()
            sec = res(sec_id, OPTX.OmsSection)
            g.add((s, OPTX.partOfSection, sec))
            rev = f"span_{sec_id}_{b}{a}"
            if rev in topo.optical_links:
                g.add((s, OPTX.reverseOf, iri(rev)))
        oa = board_of(l.from_ne, "OA")
        if oa is not None:
            amp = res(f"amp_{l.from_ne}_{link_id}", OPTX.Amplifier)
            g.add((amp, OPTX.amplifierBoard, oa))
            g.add((s, OPTX.launchAmplifier, amp))

    # ---------------------------------------------------------------- OTN links
    for link_id, l in topo.otn_links.items():
        o = res(link_id, OPTX.OtuLink,
                f"/ietf-network:networks/network[network-id='otn-topo']/ietf-network-topology:link[link-id='{link_id}']")
        g.add((o, OPTX.inNetwork, otn))
        g.add((o, OPTX.sourceNE, iri(l.from_ne)))
        g.add((o, OPTX.destNE, iri(l.to_ne)))
        tx, rx = tp_port("otn-topo", l.from_ne, l.from_tp), tp_port("otn-topo", l.to_ne, l.to_tp)
        if tx is not None:
            g.add((o, OPTX.txPort, tx))
        if rx is not None:
            g.add((o, OPTX.rxPort, rx))
        if l.supporting_link_id:
            g.add((o, OPTX.supportedBy, iri(l.supporting_link_id)))

    # ---------------------------------------------------------------- tunnels
    for name, t in topo.wdm_tunnels.items():
        o = res(name, OPTX.OchTunnel, f"/ietf-te:te/tunnels/tunnel[name='{name}']")
        g.add((o, OPTX.sourceNE, iri(t.src_ne)))
        g.add((o, OPTX.destNE, iri(t.dst_ne)))
        drop = board_of(t.dst_ne, "OTU")
        if drop is not None:
            g.add((o, OPTX.dropBoard, drop))
        g.add((o, OPTX.txChannelPowerDbm, Literal(float(t.tx_channel_power_dbm), datatype=XSD.decimal)))
        z = 0.0
        for i, hop in enumerate(t.ero):
            h = BNode()
            g.add((o, OPTX.hasHop, h))
            g.add((h, RDF.type, OPTX.Hop))
            g.add((h, OPTX.hopIndex, Literal(i)))
            g.add((h, OPTX.hopLink, iri(hop)))
            g.add((h, OPTX.hopStartKm, Literal(round(z, 3), datatype=XSD.decimal)))
            g.add((o, OPTX.traverses, iri(hop)))
            link = topo.optical_links.get(hop)
            z += float(link.length_km) if link else 0.0
        g.add((o, OPTX.lengthKm, Literal(round(z, 3), datatype=XSD.decimal)))

    for name, t in topo.otn_tunnels.items():
        u = res(name, OPTX.OduTunnel, f"/ietf-te:te/tunnels/tunnel[name='{name}']")
        g.add((u, OPTX.sourceNE, iri(t.src_ne)))
        g.add((u, OPTX.destNE, iri(t.dst_ne)))
        if t.rate_type:
            g.add((u, OPTX.rateType, Literal(t.rate_type)))
        if t.supporting_tunnel:
            g.add((u, OPTX.supportingTunnel, iri(t.supporting_tunnel)))
        for hop in t.ero:
            g.add((u, OPTX.traverses, iri(hop)))
        cp = tp_port("otn-topo", t.dst_ne, t.dst_tp_id)
        if cp is not None:
            g.add((u, OPTX.clientPort, cp))
        sp = tp_port("otn-topo", t.src_ne, t.src_tp_id)
        if sp is not None:
            g.add((u, OPTX.sourceClientPort, sp))
        if t.client_service_name:
            svc = res(t.client_service_name, OPTX.ClientService)
            g.add((u, OPTX.carries, svc))

    _materialise_depends_on(g)
    return g, tid


def _materialise_depends_on(g: Graph) -> None:
    """dx:dependsOn closure from each service / tunnel to every resource on its path."""
    def ports_boards_nes(link) -> set:
        out = set()
        for p in (OPTX.txPort, OPTX.rxPort):
            for port in g.objects(link, p):
                out.add(port)
                for b in g.objects(port, ONM.partOf):
                    out.add(b)
                    out.update(g.objects(b, ONM.partOf))
        for p in (OPTX.sourceNE, OPTX.destNE):
            out.update(g.objects(link, p))
        return out

    def och_deps(o) -> set:
        out = set()
        for s in g.objects(o, OPTX.traverses):
            out.add(s)
            out.update(g.objects(s, OPTX.partOfSection))
            out |= ports_boards_nes(s)
            for amp in g.objects(s, OPTX.launchAmplifier):
                out.add(amp)
                out.update(g.objects(amp, OPTX.amplifierBoard))
        for b in g.objects(o, OPTX.dropBoard):
            out.add(b)
        return out

    def odu_deps(u) -> set:
        out = set()
        for l in g.objects(u, OPTX.traverses):
            out.add(l)
            out |= ports_boards_nes(l)
            for s in g.objects(l, OPTX.supportedBy):
                out.add(s)
        for o in g.objects(u, OPTX.supportingTunnel):
            out.add(o)
            out |= och_deps(o)
        for p in (OPTX.clientPort, OPTX.sourceClientPort):
            for port in g.objects(u, p):
                out.add(port)
                for b in g.objects(port, ONM.partOf):
                    out.add(b)
        return out

    for o in list(g.subjects(RDF.type, OPTX.OchTunnel)):
        for r in och_deps(o):
            g.add((o, DX.dependsOn, r))
    for u in list(g.subjects(RDF.type, OPTX.OduTunnel)):
        deps = odu_deps(u)
        for r in deps:
            g.add((u, DX.dependsOn, r))
        for svc in g.objects(u, OPTX.carries):
            g.add((svc, DX.dependsOn, u))
            for r in deps:
                g.add((svc, DX.dependsOn, r))
