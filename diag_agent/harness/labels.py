"""Label access for the scorer and the FieldOracle only (doc 01 §5.3).

Every read is logged with the reading component, so the leakage test (A7)
can prove that no other component touched the labels.
"""

from __future__ import annotations

import sqlite3
import threading


class LabelReader:
    def __init__(self, db_path: str, incident_id: int, component: str):
        if component not in ("scorer", "field_oracle"):
            raise PermissionError(f"component {component!r} may not read labels")
        self.db_path, self.incident_id, self.component = db_path, int(incident_id), component
        self._log: list[dict] = []
        self._lock = threading.Lock()

    def _q(self, sql: str, params: tuple, what: str) -> list:
        with self._lock:
            self._log.append({"component": self.component, "what": what})
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            try:
                return list(conn.execute(sql, params))
            finally:
                conn.close()

    def truth(self) -> dict:
        rows = self._q("SELECT fault_type, fault_target, target_kind FROM incidents WHERE id = ?",
                       (self.incident_id,), "fault_type,fault_target")
        if not rows:
            return {"fault_type": "", "args": [], "target_kind": ""}
        ft, target, kind = rows[0]
        args = [] if target in (None, "", "(none)") else str(target).split(" ")
        return {"fault_type": ft, "args": args, "target_kind": kind, "fault_target": target}

    def root_ppe_position(self, span: str) -> float | None:
        rows = self._q("""SELECT a.offset_in_span_km FROM ppe_anomalies a JOIN ppe_traces t ON a.trace_id = t.id
                          WHERE t.incident_id = ? AND a.is_root = 1 AND a.span_id = ? AND a.offset_in_span_km IS NOT NULL
                          ORDER BY a.id LIMIT 1""", (self.incident_id, span), "ppe_anomalies.is_root=1")
        return float(rows[0][0]) if rows else None

    def access_log(self) -> list[dict]:
        return list(self._log)
