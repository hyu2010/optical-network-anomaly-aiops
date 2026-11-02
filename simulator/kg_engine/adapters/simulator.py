"""Simulator adapter — tails `data/incidents.db` and emits engine events.

R-1 strategy (Design/10 §3, §11): the simulator commits one transaction per
GUI click (one incident row + N alarm rows in a single commit), and
`incidents.db` uses SQLite's default DELETE journal mode. A polling
`SELECT id FROM incidents WHERE id > ? ORDER BY id` is correct and trivial;
WAL tailing / inotify would be over-engineering for the current write rate.

For each new incident_id (R-1 maps `incident_id := incident_id`):
  1. Load every alarm row for that incident.
  2. Emit one `alarm.raise` event per row.
  3. Emit one synthetic `incident.boundary` event to close the incident.

`--once` mode drains every incident then stops — used by the parity test.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from typing import Awaitable, Callable

from simulator.simulator import db as simdb

from ..events import Event, EventKind, new_event_id

# ---------------------------------------------------------------------------

ALARM_COLS = (
    "id", "alarm_name", "ne", "board", "port", "severity",
    "layer", "container_id", "t_offset_ms", "t_observed_ms",
    # no is_root / group_id — incidents.db has no root-cause ground truth
    # (prompt 12); RootCause / Secondary are derived by the rule reasoner.
)

PPE_TRACE_COLS = (
    "id", "och", "drop_ne", "drop_board", "total_length_km", "group_id",
    "t_offset_ms", "t_observed_ms", "delta_z_m", "n_samples", "degraded",
)
PPE_ANOM_COLS = (
    "id", "kind", "z_km", "z_end_km", "span_id", "offset_in_span_km",
    "magnitude_db", "confidence", "is_root",
)


def _open_db(db_path: str | None) -> sqlite3.Connection:
    return simdb.open_db(db_path)


def _load_alarms(conn: sqlite3.Connection, incident_id: int) -> list[dict]:
    cur = conn.execute(
        f"SELECT {', '.join(ALARM_COLS)} FROM alarms "
        "WHERE incident_id=? ORDER BY id",
        (incident_id,),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _load_ppe_traces(conn: sqlite3.Connection,
                     incident_id: int) -> list[dict]:
    """Return one dict per PPE trace, each with its `anomalies` list nested.

    Returns an empty list when the incident has no PPE rows (alarms-only
    incidents stay alarms-only — PPE is strictly additive per Design/12 §5).
    """
    cur = conn.execute(
        f"SELECT {', '.join(PPE_TRACE_COLS)} FROM ppe_traces "
        "WHERE incident_id=? ORDER BY id",
        (incident_id,),
    )
    tcols = [d[0] for d in cur.description]
    traces = [dict(zip(tcols, r)) for r in cur.fetchall()]
    if not traces:
        return []

    for tr in traces:
        acur = conn.execute(
            f"SELECT {', '.join(PPE_ANOM_COLS)} FROM ppe_anomalies "
            "WHERE trace_id=? ORDER BY id",
            (tr["id"],),
        )
        acols = [d[0] for d in acur.description]
        tr["anomalies"] = [dict(zip(acols, r)) for r in acur.fetchall()]
    return traces


def _load_incident_info(conn: sqlite3.Connection, incident_id: int) -> dict:
    """Return per-incident metadata for manifest rendering. Empty dict if
    the columns aren't present (older DB schemas)."""
    row = conn.execute(
        "SELECT fault_type, fault_target, target_kind, suppressed, topology_id "
        "FROM incidents WHERE id=?",
        (incident_id,),
    ).fetchone()
    if not row:
        return {}
    return {
        "fault_type":   row[0],
        "fault_target": row[1],
        "target_kind":  row[2],
        "suppressed":   bool(row[3]) if row[3] is not None else False,
        "topology_id":  row[4],
    }


# ---------------------------------------------------------------------------

class SimulatorAdapter:
    """Polls `incidents.db` and pushes events into `engine.queue`.

    Parameters
    ----------
    db_path : optional override; defaults to `data/incidents.db`
    poll_interval_s : how long to sleep between empty polls (default 0.5 s)
    once : if True, drain all existing incidents then stop (parity-test mode)
    max_incidents : optional cap on how many incidents to drain
    """

    def __init__(self, db_path: str | None = None,
                 *, poll_interval_s: float = 0.5,
                 once: bool = False,
                 max_incidents: int | None = None,
                 sliding_window: bool = False,
                 start_from_now: bool = False,
                 with_ppe: bool = True) -> None:
        """sliding_window: when True, do NOT supply `incident_id` in event
        payloads and do NOT emit `incident.boundary` — the engine's grouper
        and closure tick own incident assignment instead.

        start_from_now: when True, initialise the cursor at the current
        max(id) in incidents so only NEW incidents are processed. Useful for
        the web demo (`python -m simulator.kg_engine web`) where you want the UI
        to start empty and react to new fault triggers in real time.

        with_ppe: when True (default), also emit one `ppe.update` event per
        ppe_traces row for the incident. Incidents with no PPE rows skip the
        PPE step silently. Set False to keep the engine alarms-only.
        """
        self.db_path = db_path
        self.poll_interval_s = poll_interval_s
        self.once = once
        self.max_incidents = max_incidents
        self.sliding_window = sliding_window
        self.start_from_now = start_from_now
        self.with_ppe = with_ppe
        self._last_id = 0

    def __call__(self, engine) -> Awaitable[None]:
        return self._run(engine)

    async def _run(self, engine) -> None:
        conn = _open_db(self.db_path)
        try:
            if self.start_from_now:
                row = conn.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM incidents"
                ).fetchone()
                self._last_id = row[0] if row else 0
            drained = 0
            while True:
                rows = conn.execute(
                    "SELECT id FROM incidents WHERE id > ? ORDER BY id",
                    (self._last_id,),
                ).fetchall()
                hit_cap = False
                if rows:
                    for (sid,) in rows:
                        await self._emit_incident(engine, conn, sid)
                        self._last_id = sid
                        drained += 1
                        if self.max_incidents and drained >= self.max_incidents:
                            hit_cap = True
                            break

                if hit_cap:
                    break
                if self.once:
                    # Nothing new this poll; in --once mode, exit.
                    break

                await asyncio.sleep(self.poll_interval_s)
        finally:
            conn.close()
            # In bounded modes (once or max_incidents), signal the engine to
            # drain and stop. In long-running modes the adapter never exits.
            if self.once or self.max_incidents:
                # Give the engine a moment to flush the queue before stop.
                while not engine.queue.empty():
                    await asyncio.sleep(0.05)
                await engine.stop()

    async def _emit_incident(self, engine, conn: sqlite3.Connection,
                             incident_id: int) -> None:
        alarm_rows = _load_alarms(conn, incident_id)
        ppe_traces = _load_ppe_traces(conn, incident_id) if self.with_ppe else []
        # Skip only when the incident is fully empty. connector_dirty
        # incidents can have zero alarms but non-empty PPE — those still
        # need to flow through the engine so PPE handlers can react.
        if not alarm_rows and not ppe_traces:
            return
        now_ms = time.time() * 1000.0
        for row in alarm_rows:
            payload = {"row": row}
            if not self.sliding_window:
                payload["incident_id"] = incident_id
            await engine.submit(Event(
                event_id=new_event_id(),
                kind=EventKind.ALARM_RAISE,
                at=now_ms,
                source="simulator",
                payload=payload,
            ))

        # PPE snapshots arrive between the alarm batch and the incident
        # boundary so the handler can attach trace triples to the same
        # incident. In real hardware they often arrive *before* the alarms
        # (sub-second DSP vs 5-30s alarm polling); R-1's per-incident order
        # is a simplification — see Design/12 §6.3. `ppe_traces` was already
        # loaded above to gate the empty-incident skip.
        if self.with_ppe:
            for tr in ppe_traces:
                payload = {
                    "trace": {k: tr[k] for k in PPE_TRACE_COLS},
                    "anomalies": tr.get("anomalies", []),
                }
                if not self.sliding_window:
                    payload["incident_id"] = incident_id
                await engine.submit(Event(
                    event_id=new_event_id(),
                    kind=EventKind.PPE_UPDATE,
                    at=now_ms,
                    source="simulator",
                    payload=payload,
                ))

        if not self.sliding_window:
            await engine.submit(Event(
                event_id=new_event_id(),
                kind=EventKind.INCIDENT_BOUNDARY,
                at=now_ms,
                source="simulator",
                payload={
                    "incident_id": incident_id,
                    "info": _load_incident_info(conn, incident_id),
                },
            ))
