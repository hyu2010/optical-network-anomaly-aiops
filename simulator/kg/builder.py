"""Orchestrator — turns YANG + incidents.db into the full KG.

Returns a dict with topology triples, per-incident alarm triples, a manifest
row per incident, and the loaded `Topology` object. The CLI in
`src.kg.__main__` writes this to `data/kg/*.csv`.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from simulator.simulator import db as simdb
from simulator.simulator import yang_loader
from simulator.simulator.topology import Topology

from . import alarm_graph, correlation, locality, ppe as ppe_kg, temporal, topology_graph
from .schema import ALL_CLOSE, ROOT_CAUSE, Triple


def _load_incident_rows(conn: sqlite3.Connection, incident_id: int) -> list[dict]:
    # No is_root / group_id: incidents.db carries no root-cause ground truth
    # (prompt 12).  RootCause / Secondary are derived by rule in alarm_graph.
    cur = conn.execute(
        """SELECT id, alarm_name, ne, board, port, severity, layer, container_id,
                  t_offset_ms, t_observed_ms
           FROM alarms WHERE incident_id=? ORDER BY id""",
        (incident_id,),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def build_kg(
    db_path: Optional[str] = None,
    *,
    max_incidents: Optional[int] = None,
    include_derived: bool = True,
    include_correlation: bool = True,
    include_ppe: bool = True,
    compact_temporal: bool = True,
    yang_dir: Optional[str] = None,
) -> dict:
    """Build the KG.

    Parameters
    ----------
    db_path             path to incidents.db (default: data/incidents.db)
    max_incidents       cap the number of incidents processed (None = all)
    include_derived     emit locality + temporal predicates
    include_correlation emit R1–R4 weighted Correlated edges
    include_ppe         emit PPE-trace/anomaly triples (Design/12 §8)
    compact_temporal    use the AllClose / AllWithinSix per-incident flag
                        instead of N² pairwise temporal edges
    yang_dir            override the YANG directory (default: data/yang)

    Returns
    -------
    dict with keys: topology_triples, alarm_triples, ppe_triples, manifest,
                    topology, alarm_names (URI -> alarm_name lookup).
    """
    topo = yang_loader.load(yang_dir)
    topo_triples = topology_graph.build_topology_triples(topo)

    alarm_triples: list[Triple] = []
    ppe_triples: list[Triple] = []
    manifest: list[dict] = []
    alarm_names: dict[str, str] = {}      # alarm URI -> alarm_name

    conn = simdb.open_db(db_path)
    sce_rows = conn.execute(
        """SELECT id, fault_type, fault_target, target_kind, suppressed, topology_id
           FROM incidents ORDER BY id"""
    ).fetchall()
    if max_incidents is not None:
        sce_rows = sce_rows[:max_incidents]

    for sid, ft, tgt, kind, sup, top_id in sce_rows:
        alarms = _load_incident_rows(conn, sid)
        scen_ppe = ppe_kg.build_ppe_triples(conn, sid, alarms, topo) if include_ppe else []
        # A incident with neither alarms nor PPE rows is empty — skip it.
        # connector_dirty incidents can have zero alarms but non-empty PPE;
        # those are kept so the KG still records the PPE evidence.
        if not alarms and not scen_ppe:
            continue

        # Build the URI -> alarm_name map for this incident so the CSV writer
        # can populate the human-readable name columns.
        for a in alarms:
            alarm_names[f"alarm:{sid}:{a['id']}"] = a["alarm_name"]

        triples: list[Triple] = []
        if alarms:
            triples += alarm_graph.build_alarm_triples(sid, alarms, topo)

        all_close = False
        if include_derived and alarms:
            triples += locality.derive_locality_triples(sid, alarms, topo)
            t_triples = temporal.derive_temporal_triples(sid, alarms,
                                                        compact=compact_temporal)
            triples += t_triples
            if compact_temporal:
                all_close = any(t.predicate == ALL_CLOSE for t in t_triples)
            else:
                # Pairwise mode: R1 will be inferred per-edge from Close triples
                # when the rule engine consumes them; for the Correlated emit
                # here we conservatively skip R1.
                all_close = False

        if include_correlation and alarms:
            triples += correlation.derive_correlation_triples(
                sid, alarms, topo, all_close=all_close,
            )

        alarm_triples.extend(triples)
        ppe_triples.extend(scen_ppe)

        manifest.append({
            "incident_id":  sid,
            "fault_type":   ft,
            "fault_target": tgt,
            "target_kind":  kind,
            "suppressed":   bool(sup),
            "topology_id":  top_id,
            "n_alarms":     len(alarms),
            # n_root is now the count of rule-derived RootCause triples (0 or 1
            # per incident; 0 e.g. for connector_dirty, which has no root alarm).
            "n_root":       sum(1 for t in triples if t.predicate == ROOT_CAUSE),
            "n_triples":    len(triples),
            "n_ppe_triples": len(scen_ppe),
        })

    conn.close()

    return {
        "topology_triples": topo_triples,
        "alarm_triples":    alarm_triples,
        "ppe_triples":      ppe_triples,
        "manifest":         manifest,
        "topology":         topo,
        "alarm_names":      alarm_names,
    }
