"""R1–R4 weighted correlation rules.

Verbatim from `Design/08_root_cause_rules.md` §2 and
`Library/Alarm correlation logical rules.txt`. Each pair gets one
`Correlated` edge per rule that fires, tagged with `source = "rule:Rn"` and
the rule's published weight.

    R1  time-cluster (≤ 6 min span, ≤ 5 min consecutive)            w = 0.1
    R2  OTU alarm + OCh alarm carried by that OTU                    w = 0.2
    R3  ODU alarm + ODU alarm sharing a client service               w = 0.4
    R4  OCh alarm + OCh alarm whose paths share an OTS span          w = 0.3

R1 is gated on the `AllClose` compact flag for this incident (cheap and
exact for simulator data); when temporal is materialised pairwise the
caller can recompute R1 from the `Close` / `WithinSix` triples instead.
"""
from __future__ import annotations
from itertools import combinations

from simulator.simulator.topology import Topology

from .schema import Triple, CORRELATED, alarm_uri

W_R1 = 0.1
W_R2 = 0.2
W_R3 = 0.4
W_R4 = 0.3


def _och_spans(topo: Topology) -> dict[str, set[str]]:
    """OCh tunnel name -> set of optical-topo spans on its ERO."""
    out: dict[str, set[str]] = {}
    for tn in topo.wdm_tunnels.values():
        spans: set[str] = set()
        for hop in tn.ero:
            if hop in topo.optical_links:
                spans.add(hop)
            elif hop in topo.otn_links and topo.otn_links[hop].supporting_link_id:
                spans.add(topo.otn_links[hop].supporting_link_id)
        out[tn.name] = spans
    return out


def _och_otus(topo: Topology) -> dict[str, set[str]]:
    """OCh tunnel name -> set of OTU pipe link-ids that ride over it.

    Walks every ODU tunnel that names this OCh as its `supporting-tunnel`
    and collects the otn-topo hops from that ODU's ERO.
    """
    out: dict[str, set[str]] = {}
    for tn in topo.otn_tunnels.values():
        if not tn.supporting_tunnel:
            continue
        s = out.setdefault(tn.supporting_tunnel, set())
        for hop in tn.ero:
            if hop in topo.otn_links:
                s.add(hop)
    return out


def _alarm_och(row: dict, topo: Topology) -> str | None:
    """Resolve an alarm to a single OCh tunnel name, or None if ambiguous.

    The v3 simulator does not emit OCh-tunnel-tagged alarms directly, but ODU
    alarms carry the ODU tunnel name in `container_id` — its
    `SupportingTunnel` is the OCh tunnel we want for R4. Returns None for
    alarms whose container is a span or OTU pipe (those touch every OCh
    sharing that physical resource, so R4 can't pick one canonical OCh).
    """
    c = row.get("container_id") or ""
    if c in topo.wdm_tunnels:
        return c
    if c in topo.otn_tunnels:
        return topo.otn_tunnels[c].supporting_tunnel or None
    return None


def derive_correlation_triples(incident_id: int, rows: list[dict],
                               topo: Topology, *, all_close: bool) -> list[Triple]:
    och_spans = _och_spans(topo)
    och_otus  = _och_otus(topo)
    odu_svc   = {tn.name: tn.client_service_name for tn in topo.otn_tunnels.values()}

    out: list[Triple] = []

    for a, b in combinations(rows, 2):
        au = alarm_uri(incident_id, a["id"])
        bu = alarm_uri(incident_id, b["id"])
        la, lb = a.get("layer"), b.get("layer")
        ca = a.get("container_id") or ""
        cb = b.get("container_id") or ""

        # R1 — time cluster (compact: applies to every pair if the entire
        # incident fits in the 5-min window).
        if all_close:
            out.append(Triple(au, CORRELATED, bu, "rule:R1", W_R1, incident_id))

        # R2 — OTU layer + OCH layer sharing an OTU pipe.
        if {la, lb} == {"OTU", "OCH"}:
            otu_a, otu_b = (ca if la == "OTU" else None), (cb if lb == "OTU" else None)
            och_a, och_b = (ca if la == "OCH" else None), (cb if lb == "OCH" else None)
            otu = otu_a or otu_b
            och = och_a or och_b
            fires = False
            if otu and och:
                # Canonical Design/08 case: och is a WDM tunnel name.
                if och in och_otus and otu in och_otus[och]:
                    fires = True
                # v3 simulator emits OCH alarms with container=otu link id
                # (the OCh signal's OTU pipe), so same-container also fires R2.
                elif och == otu and otu in topo.otn_links:
                    fires = True
            if fires:
                out.append(Triple(au, CORRELATED, bu, "rule:R2", W_R2, incident_id))

        # R3 — both ODU layer, different ODUs, same client service.
        if la == "ODU" and lb == "ODU" and ca and cb and ca != cb:
            s1 = odu_svc.get(ca, "")
            s2 = odu_svc.get(cb, "")
            if s1 and s1 == s2:
                out.append(Triple(au, CORRELATED, bu, "rule:R3", W_R3, incident_id))

        # R4 — alarms on different OCh paths whose paths share an OTS span.
        # Per Design/08 §7 R4 is stated at the OCH layer; in v3 simulator data
        # the carrying OCh is identifiable on ODU-layer alarms (via the ODU's
        # SupportingTunnel) and on direct OCh-tunnel-tagged alarms.  Span /
        # OTU-pipe-tagged alarms are not resolvable to a single OCh, so we
        # leave them out — the literal R4 doesn't apply there.
        och_a = _alarm_och(a, topo)
        och_b = _alarm_och(b, topo)
        if och_a and och_b and och_a != och_b:
            sp_a = och_spans.get(och_a, set())
            sp_b = och_spans.get(och_b, set())
            if sp_a & sp_b:
                out.append(Triple(au, CORRELATED, bu, "rule:R4", W_R4, incident_id))

    return out
