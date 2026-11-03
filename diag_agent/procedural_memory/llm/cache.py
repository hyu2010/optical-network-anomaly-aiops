"""SQLite LLM cache / cassette (doc 05 §6.4).

The key is a hash of (model, role, messages, tools, json_mode, params); the API
key and base URL are never part of it, so cassettes recorded against DeepSeek
replay offline and can be committed.

Modes:  read_write  hit -> cached; miss -> call and store
        read_only   hit -> cached; miss -> CassetteMiss (the line abstains)
        off         always call
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path


class CassetteMiss(RuntimeError):
    pass


def cache_key(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class LLMCache:
    def __init__(self, path: str | Path, mode: str = "read_write"):
        self.mode = mode
        self.path = Path(path)
        self._lock = threading.Lock()
        self._conn = None
        if mode != "off":
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
            self._conn.execute("CREATE TABLE IF NOT EXISTS llm_cache (key TEXT PRIMARY KEY, model TEXT, role TEXT, "
                               "request TEXT, response TEXT, created REAL)")
            self._conn.commit()

    def get(self, key: str) -> dict | None:
        if self._conn is None:
            return None
        with self._lock:
            row = self._conn.execute("SELECT response FROM llm_cache WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, model: str, role: str, request: dict, response: dict) -> None:
        if self._conn is None or self.mode != "read_write":
            return
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO llm_cache VALUES (?, ?, ?, ?, ?, ?)",
                               (key, model, role, json.dumps(request, ensure_ascii=False),
                                json.dumps(response, ensure_ascii=False), time.time()))
            self._conn.commit()

    def count(self) -> int:
        if self._conn is None:
            return 0
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0])
