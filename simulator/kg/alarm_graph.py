"""Stage 2 — per-incident alarm-location triples and label triples.

For each alarm row in the incident:
  • emit IsLayer / AtNE / AtBoard / AtPort
  • classify `container_id` and emit AtSpan / OnPipe / AtChannel / Container

The RootCause / Secondary LABELS are no longer read from incidents.db (it
carries no root-cause ground truth — prompt 12).  They are DERIVED at KG-build
time by the rule reasoner (`simulator/rules`, the §3–§8 rules of
Design/08_root_cause_rules.md) — the same way correlation R1–R4 is already
derived by rule.  ``alarm_label_triples`` runs the reasoner over the incident's
full alarm set + topology and emits:
  • RootCause(r, r)     for the rule-picked root r
  • Secondary(a, p)     for each alarm a a rule attributes to cause p
Both the batch builder and the streaming engine call this one function, so
their label triples stay in lock-step (the parity invariant).
"""
from __future__ import annotations

from simulator.simulator.topology import Topology

from .schema import (
    Triple,
    IS_LAYER, AT_NE, AT_BOARD, AT_PORT, AT_SPAN, ON_PIPE, AT_CHANNEL, CONTAINER,
    ROOT_CAUSE, SECONDARY,
    alarm_uri, ne_uri, board_uri, port_uri, layer_uri, container_uri,
    classify_container,
)

# Provenance tag for the rule-derived root/secondary triples (was "alarm" when
# they came straight from the simulator's is_root/group_id ground truth).
RULE_SOURCE = "rule"


def alarm_location_triples(incident_id: int, row: dict,
                           topo: Topology) -> list[Triple]:
    """Per-row location triples — IsLayer, AtNE/Board/Port, container edge.

    Extracted so both the batch builder and the streaming alarm handler in
    `src/kg_engine/handlers/alarms.py` use the same code path. The label
    triples (RootCause / Secondary) are emitted by `alarm_label_triples`
    instead, because they require knowing every root in the incident.
    """
    out: list[Triple] = []
    a = alarm_uri(incident_id, row["id"])
    if row.get("layer"):
        out.append(Triple(a, IS_LAYER, layer_uri(row["layer"]),
                          "alarm", 1.0, incident_id))
    if row.get("ne"):
        out.append(Triple(a, AT_NE, ne_uri(row["ne"]),
                          "alarm", 1.0, incident_id))
    if row.get("board"):
        out.append(Triple(a, AT_BOARD, board_uri(row["board"]),
                          "alarm", 1.0, incident_id))
    if row.get("port"):
        out.append(Triple(a, AT_PORT, port_uri(row["port"]),
                          "alarm", 1.0, incident_id))

    cid = row.get("container_id") or ""
    if cid:
        kind = classify_container(cid, topo)
        uri = container_uri(cid, kind)
        if kind == "span":
            pred = AT_SPAN
        elif kind == "otu":
            pred = ON_PIPE
        elif kind in ("och", "odu"):
            pred = AT_CHANNEL
        else:
            pred = CONTAINER
        out.append(Triple(a, pred, uri, "alarm", 1.0, incident_id))
    return out


def alarm_label_triples(incident_id: int, rows: list[dict],
                        topo: Topology) -> list[Triple]:
    """Rule-derived RootCause + Secondary triples for one incident.

    Runs the rule reasoner (``simulator.rules.infer_root`` — the
    Design/08 §3–§8 rules) over the incident's full alarm set + topology and
    turns its verdict into KG triples:

      • one ``RootCause(r, r)`` self-loop for the picked root r;
      • one ``Secondary(a, p)`` edge for each alarm a the rules attribute to an
        upstream cause p (deduped; the root itself is never marked Secondary).

    Needs the whole row set (the reasoner reasons over the incident as a whole)
    + the topology.  Called by the batch builder per incident and by the
    streaming engine at incident close — the same code path, so both stay in
    parity.
    """
    if not rows:
        return []
    # Lazy import keeps simulator.kg importable without the rules package on
    # the path until labelling is actually requested.
    from simulator.rules import infer_root

    inf = infer_root(incident_id, rows, topo)
    out: list[Triple] = []
    root_id = inf.root_alarm_id
    if root_id:
        r_uri = alarm_uri(incident_id, root_id)
        out.append(Triple(r_uri, ROOT_CAUSE, r_uri, RULE_SOURCE, 1.0, incident_id))

    seen: set[tuple[str, str]] = set()
    for x in inf.inferred:
        if x.kind != "Secondary":
            continue
        if x.alarm_id == root_id:           # never demote the chosen root
            continue
        child = alarm_uri(incident_id, x.alarm_id)
        parent = alarm_uri(incident_id, x.parent_id)
        key = (child, parent)
        if key in seen:
            continue
        seen.add(key)
        out.append(Triple(child, SECONDARY, parent, RULE_SOURCE, 1.0, incident_id))
    return out


def build_alarm_triples(incident_id: int, rows: list[dict],
                        topo: Topology) -> list[Triple]:
    out: list[Triple] = []
    for r in rows:
        out.extend(alarm_location_triples(incident_id, r, topo))
    out.extend(alarm_label_triples(incident_id, rows, topo))
    return out
