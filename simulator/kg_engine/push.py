"""Push API — inject a fresh PPE reading into the live KG-Engine (Design/00 §5).

The KG is explicitly **never complete**: it holds history, and fresh PPE from
the field must extend it in real time.  The call flow the design describes is:

    ppe_agent ──(fresh per-OCh P(z) + anomalies)──► knowledge_gateway
                                                        │
                                                        ▼
                                                KG-Engine push API  ← THIS MODULE
                                                (PPE_UPDATE event)
                                                        │
                                       handlers/ppe.py → Delta(add/remove triples)
                                                        │
                              ┌─────────────────────────┼───────────────┐
                              ▼                          ▼               ▼
                         TripleIndex                 csv_mirror      SSE → dashboard
                         (live graph)              (data/kg/*.csv)  (live KG update)

So when `diag_agent` re-queries the KG, the freshly-pushed PPE evidence is
already materialised as triples.  This module is the single entry point
`knowledge_gateway` calls; it builds a `PPE_UPDATE` event in exactly the shape
`handlers/ppe.handle_ppe_update` expects (the same shape `adapters/simulator`
emits), so streaming output stays byte-identical to the batch builder.
"""
from __future__ import annotations

import time

from .events import Event, EventKind, new_event_id


def build_ppe_update_event(*, och: str, drop_ne: str = "", drop_board: str = "",
                           total_length_km: float = 0.0, delta_z_m: int = 200,
                           n_samples: int = 0, degraded: bool = False,
                           anomalies: list[dict] | None = None,
                           incident_id: int | None = None,
                           source: str = "gateway") -> Event:
    """Build a `PPE_UPDATE` event from raw per-OCh fields.

    `anomalies` is a list of dicts with keys
    ``{kind, z_km, z_end_km, span_id, offset_in_span_km, magnitude_db,
       confidence, is_root}`` — the same columns `adapters/simulator` reads.
    """
    trace = {
        "och": och, "drop_ne": drop_ne, "drop_board": drop_board,
        "total_length_km": total_length_km, "delta_z_m": delta_z_m,
        "n_samples": n_samples, "degraded": 1 if degraded else 0,
    }
    payload: dict = {"trace": trace, "anomalies": list(anomalies or [])}
    if incident_id is not None:
        payload["incident_id"] = incident_id
    return Event(
        event_id=new_event_id(), kind=EventKind.PPE_UPDATE,
        at=time.time() * 1000.0, source=source, payload=payload,
    )


def event_from_trace(trace, *, incident_id: int | None = None,
                     source: str = "gateway") -> Event:
    """Build a `PPE_UPDATE` event from a `simulator.simulator.ppe.PpeTrace`.

    Convenience for the gateway / ppe_agent, which hold a PpeTrace returned by
    the PPE MCP server.
    """
    anomalies = [{
        "kind": a.kind, "z_km": a.z_km, "z_end_km": a.z_end_km,
        "span_id": getattr(a, "span_id", ""),
        "offset_in_span_km": getattr(a, "offset_in_span_km", None),
        "magnitude_db": a.magnitude_db, "confidence": a.confidence,
        "is_root": 1 if a.is_root else 0,
    } for a in trace.anomalies]
    return build_ppe_update_event(
        och=trace.och, drop_ne=trace.drop_ne, drop_board=trace.drop_board,
        total_length_km=trace.total_length_km, delta_z_m=trace.delta_z_m,
        n_samples=len(trace.samples), degraded=trace.degraded,
        anomalies=anomalies, incident_id=incident_id, source=source,
    )


async def push_ppe_reading(engine, trace, *, incident_id: int | None = None,
                           source: str = "gateway") -> Event:
    """Submit a fresh PPE reading (a PpeTrace) into a running engine.

    Returns the submitted event so callers can correlate.  The engine handles
    it under its lock, applies the delta to the live `TripleIndex`, and
    broadcasts to every subscriber (csv_mirror, SSE).
    """
    ev = event_from_trace(trace, incident_id=incident_id, source=source)
    await engine.submit(ev)
    return ev


__all__ = ["build_ppe_update_event", "event_from_trace", "push_ppe_reading"]
