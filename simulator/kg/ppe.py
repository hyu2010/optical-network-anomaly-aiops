"""PPE → KG triples (Design/12 §8).

For each incident, emit:

  trace = (PpeTrace, MeasuredOn, OChTunnel)
        + (PpeTrace, MeasuresSpan, Span)
        + (PpeTrace, MeasuredBy, Board)        — drop OTU
        + (PpeTrace, HasAnomaly, PpeAnomaly)*

  anomaly = (PpeAnomaly, LocatedAt, FibrePosition)?   when z_km is not NULL
          + (FibrePosition, OnSpan, Span)
          + (PpeAnomaly, PpeRootCause, "true")    when is_root = 1
          + (PpeAnomaly, PpeIndicates, Alarm)*     when spatially co-located

The `PpeIndicates` edge is the cross-modal fusion link: a PPE anomaly on
span S is linked to every alarm in the same incident whose container is S
(or whose alarm sits on a port whose span is S). It's emitted with
source="derived" so consumers can opt in/out by source filter.
"""
from __future__ import annotations

import sqlite3

from simulator.simulator.topology import Topology

from .schema import (
    HAS_ANOMALY, LOCATED_AT, MEASURED_BY, MEASURED_ON, MEASURES_SPAN,
    ON_SPAN, PPE_INDICATES, PPE_ROOT_CAUSE,
    Triple, alarm_uri, board_uri, fpos_uri, och_uri, ppe_anom_uri,
    ppe_trace_uri, span_uri,
)


def _load_traces(conn: sqlite3.Connection, incident_id: int) -> list[dict]:
    cur = conn.execute(
        """SELECT id, och, drop_ne, drop_board, total_length_km, group_id,
                  t_offset_ms, t_observed_ms, delta_z_m, n_samples, degraded
           FROM ppe_traces WHERE incident_id=? ORDER BY id""",
        (incident_id,),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _load_anomalies(conn: sqlite3.Connection, trace_id: int) -> list[dict]:
    cur = conn.execute(
        """SELECT id, kind, z_km, z_end_km, span_id, offset_in_span_km,
                  magnitude_db, confidence, is_root
           FROM ppe_anomalies WHERE trace_id=? ORDER BY id""",
        (trace_id,),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def build_ppe_triples(
    conn: sqlite3.Connection,
    incident_id: int,
    alarms: list[dict],
    topo: Topology,
) -> list[Triple]:
    """Emit all PPE triples for one incident.

    Returns an empty list when the incident has no PPE traces (PPE is
    strictly additive — alarm-only incidents stay alarm-only).
    """
    traces = _load_traces(conn, incident_id)
    if not traces:
        return []

    out: list[Triple] = []
    fpos_seen: set[str] = set()  # emit `OnSpan` once per FibrePosition

    # Pre-index alarms by container_id so PpeIndicates lookup is O(1) per
    # anomaly. Multiple alarms can share a container — we want all of them.
    alarms_by_container: dict[str, list[dict]] = {}
    for a in alarms:
        c = a.get("container_id") or ""
        if c:
            alarms_by_container.setdefault(c, []).append(a)

    for tr in traces:
        ch    = tr["och"]
        t_uri = ppe_trace_uri(incident_id, ch)

        # 1. Trace's structural triples
        out.append(Triple(t_uri, MEASURED_ON, och_uri(ch),
                          source="ppe", incident_id=incident_id))
        # MeasuresSpan: one edge per span on the OCh's route (the receiver
        # reconstructs the whole path, so its trace measures every span).
        route_spans = []
        tn = topo.wdm_tunnels.get(ch)
        if tn is not None:
            route_spans = [s for s in tn.ero if s in topo.optical_links]
        for s in route_spans:
            out.append(Triple(t_uri, MEASURES_SPAN, span_uri(s),
                              source="ppe", incident_id=incident_id))
        if tr["drop_board"]:
            out.append(Triple(t_uri, MEASURED_BY, board_uri(tr["drop_board"]),
                              source="ppe", incident_id=incident_id))

        # 2. Per-anomaly triples
        anomalies = _load_anomalies(conn, tr["id"])
        for idx, an in enumerate(anomalies):
            a_uri = ppe_anom_uri(incident_id, ch, idx)
            out.append(Triple(t_uri, HAS_ANOMALY, a_uri,
                              source="ppe", incident_id=incident_id))

            # Spatial localisation — reverse-mapped to (span, offset).
            an_span = an.get("span_id") or ""
            if an["z_km"] is not None and an_span:
                off = an.get("offset_in_span_km")
                fp = fpos_uri(an_span, off if off is not None else an["z_km"])
                out.append(Triple(a_uri, LOCATED_AT, fp,
                                  source="ppe", incident_id=incident_id))
                if fp not in fpos_seen:
                    out.append(Triple(fp, ON_SPAN, span_uri(an_span),
                                      source="topology",
                                      incident_id=incident_id))
                    fpos_seen.add(fp)

            # Ground-truth root label (parallels alarm RootCause)
            if an["is_root"]:
                out.append(Triple(a_uri, PPE_ROOT_CAUSE, "true",
                                  source="ppe", incident_id=incident_id))

            # Cross-modal fusion edges: link anomaly to alarms whose container
            # matches the anomaly's span or the OCh tunnel itself (OTU-layer
            # alarms on the channel).
            candidates = list(alarms_by_container.get(an_span, ())) if an_span else []
            candidates += alarms_by_container.get(ch, ())
            for alm in candidates:
                out.append(Triple(a_uri, PPE_INDICATES,
                                  alarm_uri(incident_id, alm["id"]),
                                  source="derived",
                                  incident_id=incident_id))

    return out
