"""Production alarm-stream adapter — Design/10 §3, §11 (R-5).

NOT IMPLEMENTED.  This file is the seam.

WHY NOT YET:
  - The shape of the input is deployment-specific. Real NMS alarm streams
    are delivered by one of: SNMP traps, Kafka topics, syslog, or vendor
    HTTP webhooks. Each has a different transport library and message
    schema.
  - The Huawei NMS dump format in `Library/Alm_format.txt` shows the
    target *record* shape — `NUM`, `TYPE`, `ALMNAME`, etc. — but doesn't
    pin the transport.

WHAT IT SHOULD DO when implemented:

  1. Connect to the configured source — Kafka consumer, SNMP receiver,
     syslog listener, or HTTPS webhook.
  2. For each message:
       - Parse into the Huawei `ALM-CUR-DATA-ALL` record shape (or
         equivalent vendor record).
       - Normalise field names to the engine's `alarm.raise` payload:
           alarm_uid     <- vendor stable id (Huawei `NUM`)
           name          <- ALMNAME (after rate-suffix normalisation per
                            src/simulator/engine.py::normalise)
           layer         <- from alarm_catalog_9800.json lookup
           ne, board, port, container_id  <- from LOC parsing
           severity      <- ALMSEVERITY
           t_observed_ms <- ALMRAISEDTIME
       - For clears: emit `alarm.clear` with `alarm_uid`.
       - For severity/location updates: emit `alarm.update`.
  3. Heartbeats: emit no event, but log the last message timestamp so the
     engine knows the link is alive (R-6 will add a back-pressure / health
     signal here).

The engine's `state.alarms` will need an `alarm_uid -> alarm_uri` index so
clears find the original raise. R-1 doesn't have that index yet because
the simulator's alarm_row_id is unique by construction.

REFERENCE SHAPE: adapters/simulator.py implements the same
`__call__(engine) -> Awaitable[None]` protocol the engine expects; this
module should follow it.
"""
from __future__ import annotations


class AlarmStreamAdapter:
    """Placeholder for SNMP / Kafka / syslog / webhook alarm ingestion.

    Constructor raises NotImplementedError until a target source is chosen.
    """

    def __init__(self, *_args, **_kwargs) -> None:
        raise NotImplementedError(
            "Alarm-stream adapter is R-5 work. See "
            "src/kg_engine/adapters/alarm_stream.py for the implementation "
            "outline and Design/10 §11 for the contract."
        )
