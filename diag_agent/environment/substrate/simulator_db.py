"""Replay substrate over the simulator's incidents.db (doc 02 §6.2).

The agent sees an alarm only when its observed time (created_at +
t_observed_ms) is at or before the agent clock.  Every query goes through the
allowlist and is recorded in the access log (acceptance A7).
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .allowlist import AUDIT_PREDICATE, check_columns

ALARM_COLS = ["id", "alarm_name", "ne", "board", "port", "severity", "layer", "container_id", "t_observed_ms"]
TRACE_COLS = ["id", "och", "drop_ne", "drop_board", "total_length_km", "t_observed_ms", "delta_z_m", "n_samples",
              "degraded"]
ANOM_COLS = ["id", "trace_id", "kind", "z_km", "z_end_km", "span_id", "offset_in_span_km", "magnitude_db",
             "confidence"]


class SimulatorDBSubstrate:
    """`Substrate` for one incident of a simulator database."""

    def __init__(self, db_path: str | Path, incident_id: int, component: str = "substrate"):
        self.db_path = str(db_path)
        self.incident_id = int(incident_id)
        self.component = component
        self._log: list[dict] = []
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        row = self._select("incidents", ["id", "created_at", "suppressed", "topology_id"], "id = ?", (self.incident_id,))
        if not row:
            raise KeyError(f"incident {incident_id} not found in {db_path}")
        r = row[0]
        self.created_at = _iso_z(r["created_at"])
        self.suppressed = bool(r["suppressed"])
        self.topology_id = r["topology_id"]
        self._alarms: list[dict] | None = None

    # ------------------------------------------------------------------ core
    def _select(self, table: str, columns: list[str], where: str, params: tuple, *, order: str = "",
                predicate_note: str | None = None) -> list[sqlite3.Row]:
        check_columns(table, columns)
        sql = f"SELECT {', '.join(columns)} FROM {table} WHERE {where}" + (f" ORDER BY {order}" if order else "")
        with self._lock:
            self._log.append({"component": self.component, "table": table, "columns": list(columns),
                              "where": where, "note": predicate_note})
            return list(self._conn.execute(sql, params))

    def access_log(self) -> list[dict]:
        return list(self._log)

    # ------------------------------------------------------------------ alarms
    def _all_alarms(self) -> list[dict]:
        if self._alarms is None:
            rows = self._select("alarms", ALARM_COLS, "incident_id = ?", (self.incident_id,), order="t_observed_ms, id")
            self._alarms = [self._alarm(r) for r in rows]
        return self._alarms

    def _alarm(self, r) -> dict:
        return {"row": int(r["id"]), "event_id": f"alm-{self.incident_id}-{int(r['id'])}", "name": r["alarm_name"],
                "ne": r["ne"] or "", "board": r["board"] or "", "port": r["port"] or "",
                "severity": r["severity"] or "", "layer": r["layer"] or "other",
                "container": r["container_id"] or "", "t_obs": float(r["t_observed_ms"]) / 1000.0}

    def alarms_until(self, t_s: float) -> list[dict]:
        return [a for a in self._all_alarms() if a["t_obs"] <= t_s + 1e-9]

    def first_alarm_time(self) -> float | None:
        alarms = self._all_alarms()
        return alarms[0]["t_obs"] if alarms else None

    def last_alarm_time(self) -> float | None:
        alarms = self._all_alarms()
        return alarms[-1]["t_obs"] if alarms else None

    def audit(self, *, ne: str | None = None, port: str | None = None, t_s: float) -> list[dict]:
        """CHK_ALARM_AUDIT: an immediate poll of one NE (or one port).

        Returns the alarms physically standing at `t_s` at that place, with
        their observed time set to `t_s` (what a resynchronisation shows).
        """
        where, params = ["incident_id = ?", AUDIT_PREDICATE], [self.incident_id, int(t_s * 1000)]
        if ne:
            where.append("ne = ?")
            params.append(ne)
        if port:
            where.append("port = ?")
            params.append(port)
        rows = self._select("alarms", ALARM_COLS, " AND ".join(where), tuple(params), order="id",
                            predicate_note="audit: physical-time predicate evaluated in the substrate")
        out = []
        for r in rows:
            a = self._alarm(r)
            a["t_obs"] = min(a["t_obs"], t_s)
            a["via"] = "audit"
            out.append(a)
        return out

    # ------------------------------------------------------------------ PPE
    def _anomalies(self, trace_id: int) -> list[dict]:
        rows = self._select("ppe_anomalies", ANOM_COLS, "trace_id = ? AND is_root = 0", (trace_id,), order="id",
                            predicate_note="detector rows only")
        return [{"kind": r["kind"], "z_km": r["z_km"], "z_end_km": r["z_end_km"], "span_id": r["span_id"] or "",
                 "offset_in_span_km": r["offset_in_span_km"], "magnitude_db": r["magnitude_db"],
                 "confidence": r["confidence"]} for r in rows]

    def _trace(self, r) -> dict:
        tid = int(r["id"])
        return {"trace_id": tid, "event_id": f"ppe-{self.incident_id}-{r['och']}-{tid}", "och": r["och"],
                "drop_ne": r["drop_ne"], "drop_board": r["drop_board"] or "",
                "total_length_km": float(r["total_length_km"] or 0), "t_obs": float(r["t_observed_ms"] or 0) / 1000.0,
                "delta_z_m": int(r["delta_z_m"]), "n_samples": int(r["n_samples"]), "degraded": bool(r["degraded"]),
                "anomalies": self._anomalies(tid)}

    def ppe_traces_until(self, t_s: float) -> list[dict]:
        rows = self._select("ppe_traces", TRACE_COLS, "incident_id = ? AND t_observed_ms <= ?",
                            (self.incident_id, int(t_s * 1000)), order="t_observed_ms, id")
        return [self._trace(r) for r in rows]

    def ppe_trace(self, och: str, t_s: float) -> dict | None:
        rows = self._select("ppe_traces", TRACE_COLS, "incident_id = ? AND och = ? AND t_observed_ms <= ?",
                            (self.incident_id, och, int(t_s * 1000)), order="t_observed_ms DESC, id DESC")
        return self._trace(rows[0]) if rows else None

    def close(self) -> None:
        self._conn.close()


def _iso_z(s: str) -> str:
    s = str(s)
    if s.endswith("+00:00"):
        s = s[:-6] + "Z"
    if not s.endswith("Z") and "+" not in s[10:]:
        s += "Z"
    return s


def list_incidents(db_path: str | Path, *, suppressed: bool | None = False) -> list[dict]:
    """Incident ids and base times (allowlisted columns only)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    q = "SELECT id, created_at, suppressed, topology_id FROM incidents"
    if suppressed is not None:
        q += f" WHERE suppressed = {1 if suppressed else 0}"
    rows = [dict(r) for r in conn.execute(q + " ORDER BY id")]
    conn.close()
    return rows
