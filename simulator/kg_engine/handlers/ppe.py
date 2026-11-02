"""PPE handler (Design/12 §7.2).

Snapshot-and-replace semantics: at most one trace's worth of triples per
``(channel, span, drop_ne)`` survives in live state. When a new ``ppe.update``
arrives, the previous snapshot's triples for that key are GC'd as `removes`.

Triples emitted follow the batch builder's schema verbatim
(`src/kg/ppe.py::build_ppe_triples` + `src/kg/schema.py`), so streaming and
batch outputs are byte-identical down to the URI strings.
"""
from __future__ import annotations

from simulator.kg.schema import (
    HAS_ANOMALY, LOCATED_AT, MEASURED_BY, MEASURED_ON, MEASURES_SPAN,
    ON_SPAN, PPE_INDICATES, PPE_ROOT_CAUSE,
    Triple, alarm_uri, board_uri, fpos_uri, och_uri, ppe_anom_uri,
    ppe_trace_uri, span_uri,
)

from ..events import Delta, Event, SOURCE_DERIVED, SOURCE_PPE, SOURCE_TOPOLOGY
from ..state import StateStore

# Predicates that originate on a PpeTrace subject. Used by the GC step to
# locate the previous snapshot's triples without re-deriving them.
_TRACE_PREDICATES = frozenset({
    MEASURED_ON, MEASURES_SPAN, MEASURED_BY, HAS_ANOMALY,
})
# Predicates that originate on a PpeAnomaly subject.
_ANOM_PREDICATES = frozenset({
    LOCATED_AT, PPE_ROOT_CAUSE, PPE_INDICATES,
})


def handle_ppe_update(event: Event, state: StateStore) -> Delta:
    """Apply one PPE snapshot to state.

    Payload shape (see SimulatorAdapter / Design/12 §7.1):
        {
          "trace": {channel, span_id, drop_ne, drop_board, group_id,
                    t_offset_ms, t_observed_ms, delta_z_m, n_samples,
                    degraded, id},
          "anomalies": [{kind, z_km, z_end_km, magnitude_db, confidence,
                         is_root, id}, ...],
          "incident_id": <incident_id>  (optional in sliding-window mode)
        }
    """
    payload = event.payload or {}
    tr = payload.get("trace") or {}
    anomalies = payload.get("anomalies") or []
    incident_id = payload.get("incident_id", 0)

    channel = tr.get("och") or ""
    if not channel:
        return Delta()

    sid = int(incident_id) if incident_id else 0
    t_uri = ppe_trace_uri(sid, channel)

    # Route spans (one MeasuresSpan edge each) from the live topology — the
    # receiver reconstructs the whole path, so its trace measures every span.
    route_spans: list[str] = []
    tn = state.topology.wdm_tunnels.get(channel)
    if tn is not None:
        route_spans = [s for s in tn.ero if s in state.topology.optical_links]

    delta = Delta()

    # --- GC the previous snapshot for the same (channel, span, drop_ne) ----
    # Snapshot-and-replace: any triple whose subject is this trace URI, or
    # whose subject is a previous anomaly under this trace, is removed.
    for old in list(state.triples.by_subject.get(t_uri, ())):
        delta.removes.append(old)
    for old in list(state.triples.by_subject.get(t_uri, ())):
        # The HasAnomaly edges land in by_subject[t_uri]; from each we can
        # walk to the anomaly URI and GC its outgoing edges too.
        if old.predicate == HAS_ANOMALY:
            for anom_edge in list(state.triples.by_subject.get(old.object, ())):
                delta.removes.append(anom_edge)

    # --- Trace's structural triples ----------------------------------------
    delta.adds.append(Triple(t_uri, MEASURED_ON, och_uri(channel),
                             source=SOURCE_PPE, incident_id=sid))
    for s in route_spans:
        delta.adds.append(Triple(t_uri, MEASURES_SPAN, span_uri(s),
                                 source=SOURCE_PPE, incident_id=sid))
    if tr.get("drop_board"):
        delta.adds.append(Triple(t_uri, MEASURED_BY, board_uri(tr["drop_board"]),
                                 source=SOURCE_PPE, incident_id=sid))

    # --- Per-anomaly triples -----------------------------------------------
    # Pre-index this incident's alarms by container so PpeIndicates lookup is
    # O(1) per anomaly. R-1: an "incident" maps 1:1 to a incident and its
    # alarms have already arrived via alarm.raise, so they're in state.
    incident = state.incidents.get(sid)
    by_container: dict[str, list[dict]] = {}
    if incident is not None:
        for a in incident.alarm_rows:
            c = a.get("container_id") or ""
            if c:
                by_container.setdefault(c, []).append(a)

    fpos_emitted_this_event: set[str] = set()
    for idx, an in enumerate(anomalies):
        a_uri = ppe_anom_uri(sid, channel, idx)
        delta.adds.append(Triple(t_uri, HAS_ANOMALY, a_uri,
                                 source=SOURCE_PPE, incident_id=sid))

        # Spatial localisation — reverse-mapped to (span, offset).
        an_span = an.get("span_id") or ""
        if an.get("z_km") is not None and an_span:
            off = an.get("offset_in_span_km")
            fp = fpos_uri(an_span, off if off is not None else an["z_km"])
            delta.adds.append(Triple(a_uri, LOCATED_AT, fp,
                                     source=SOURCE_PPE, incident_id=sid))
            if fp not in fpos_emitted_this_event:
                delta.adds.append(Triple(fp, ON_SPAN, span_uri(an_span),
                                         source=SOURCE_TOPOLOGY,
                                         incident_id=sid))
                fpos_emitted_this_event.add(fp)

        if an.get("is_root"):
            delta.adds.append(Triple(a_uri, PPE_ROOT_CAUSE, "true",
                                     source=SOURCE_PPE, incident_id=sid))

        # Cross-modal fusion edges to alarms whose container matches the
        # anomaly's span or the OCh channel (same heuristic as the batch
        # builder).
        candidates = list(by_container.get(an_span, ())) if an_span else []
        candidates += by_container.get(channel, ())
        for alm in candidates:
            delta.adds.append(Triple(a_uri, PPE_INDICATES,
                                     alarm_uri(sid, alm["id"]),
                                     source=SOURCE_DERIVED,
                                     incident_id=sid))

    return delta


__all__ = ["handle_ppe_update"]
