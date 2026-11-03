"""Incident store (doc 06 §7): published incident views, notification log, tasks.

The LangGraph thread remains the source of truth for the reasoning; this
store holds only what the Incident Server publishes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path


class IncidentStore:
    def __init__(self, path: str | Path, start_no: int = 1001):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS incidents (
              incident_no TEXT PRIMARY KEY, name TEXT, type TEXT, qualifier TEXT, raw_incident_id INTEGER,
              status TEXT, thread_id TEXT, view TEXT, tree TEXT, ledger TEXT);
            CREATE TABLE IF NOT EXISTS notifications (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, incident_no TEXT, kind TEXT, event_time TEXT,
              payload TEXT);
            CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, incident_no TEXT, record TEXT);
        """)
        if self._get_meta("next_no") is None:
            self._set_meta("next_no", str(start_no))
        self._conn.commit()

    def _get_meta(self, key: str) -> str | None:
        r = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return r[0] if r else None

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    def next_incident_no(self) -> str:
        with self._lock:
            n = int(self._get_meta("next_no"))
            self._set_meta("next_no", str(n + 1))
            self._conn.commit()
            return str(n)

    def key_in_use(self, name: str, type_: str, qualifier: str) -> bool:
        with self._lock:
            r = self._conn.execute("SELECT 1 FROM incidents WHERE name = ? AND type = ? AND qualifier = ? "
                                   "AND status != 'cleared'", (name, type_, qualifier)).fetchone()
        return r is not None

    def save_view(self, view: dict, thread_id: str = "") -> None:
        with self._lock:
            cur = self._conn.execute("SELECT thread_id, tree, ledger FROM incidents WHERE incident_no = ?",
                                     (view["incident_no"],)).fetchone()
            tid = thread_id or (cur[0] if cur else "")
            self._conn.execute("INSERT OR REPLACE INTO incidents VALUES (?,?,?,?,?,?,?,?,?,?)", (
                view["incident_no"], view["name"], view.get("type", ""), view.get("qualifier", ""),
                view.get("raw_incident_id"), view.get("status", "raised"), tid, json.dumps(view),
                cur[1] if cur else None, cur[2] if cur else None))
            self._conn.commit()

    def save_reasoning(self, incident_no: str, tree: list | None = None, ledger: dict | None = None) -> None:
        with self._lock:
            if tree is not None:
                self._conn.execute("UPDATE incidents SET tree = ? WHERE incident_no = ?", (json.dumps(tree), incident_no))
            if ledger is not None:
                self._conn.execute("UPDATE incidents SET ledger = ? WHERE incident_no = ?",
                                   (json.dumps(ledger), incident_no))
            self._conn.commit()

    def view(self, incident_no: str) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT view FROM incidents WHERE incident_no = ?", (str(incident_no),)).fetchone()
        return json.loads(r[0]) if r else None

    def views(self, statuses: list[str] | None = None) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT view, status FROM incidents ORDER BY CAST(incident_no AS INTEGER)").fetchall()
        return [json.loads(v) for v, s in rows if not statuses or s in statuses]

    def reasoning(self, incident_no: str) -> tuple[list | None, dict | None, str]:
        with self._lock:
            r = self._conn.execute("SELECT tree, ledger, thread_id FROM incidents WHERE incident_no = ?",
                                   (str(incident_no),)).fetchone()
        if not r:
            return None, None, ""
        return (json.loads(r[0]) if r[0] else None, json.loads(r[1]) if r[1] else None, r[2] or "")

    def log_notification(self, incident_no: str, kind: str, event_time: str, payload: dict) -> int:
        with self._lock:
            cur = self._conn.execute("INSERT INTO notifications (incident_no, kind, event_time, payload) VALUES (?,?,?,?)",
                                     (incident_no, kind, event_time, json.dumps(payload)))
            self._conn.commit()
            return int(cur.lastrowid)

    def notifications(self, incident_no: str | None = None, after: int = 0) -> list[dict]:
        q = "SELECT seq, incident_no, kind, event_time, payload FROM notifications WHERE seq > ?"
        args: list = [after]
        if incident_no:
            q += " AND incident_no = ?"
            args.append(str(incident_no))
        with self._lock:
            rows = self._conn.execute(q + " ORDER BY seq", args).fetchall()
        return [{"seq": r[0], "incident_no": r[1], "kind": r[2], "event_time": r[3], "payload": json.loads(r[4])}
                for r in rows]

    def save_task(self, task: dict) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO tasks VALUES (?,?,?)",
                               (task["task_id"], str(task["incident_no"]), json.dumps(task)))
            self._conn.commit()

    def tasks(self, incident_no: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT record FROM tasks WHERE incident_no = ? ORDER BY task_id",
                                      (str(incident_no),)).fetchall()
        return [json.loads(r[0]) for r in rows]
