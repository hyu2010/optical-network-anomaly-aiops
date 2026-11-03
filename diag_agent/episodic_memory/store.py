"""Episodic memory: closed diagnoses with their role-form signature (doc 02 §5).

An episode stores the frame's role form (no instance ids), the committed
conclusion as a *relation* to the frame (mode + location term + anchor
signature) so it can be re-resolved on a new incident, the outcome, and a
verification weight:
    repair_confirmed 1.0 · operator_confirmed 0.9 · self (unverified) 0.5 · rejected 0.0
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

VERIFICATION_WEIGHT = {"repair_confirmed": 1.0, "operator_confirmed": 0.9, "self": 0.5, "rejected": 0.0}
FEATURE_WEIGHTS = {"item": 3.0, "quiet": 1.0, "ppe": 1.0, "svc": 1.0, "layer": 1.0, "anchor": 1.0, "shared": 1.0}


def signature_tokens(role_form: dict) -> dict[str, float]:
    toks: dict[str, float] = {}
    for x in role_form.get("items", []):
        toks[f"item:{x}"] = FEATURE_WEIGHTS["item"]
    for x in role_form.get("quiet_roles", []):
        toks[f"quiet:{x}"] = FEATURE_WEIGHTS["quiet"]
    for x in role_form.get("ppe_kinds", []):
        toks[f"ppe:{x}"] = FEATURE_WEIGHTS["ppe"]
    for k, n in (role_form.get("services") or {}).items():
        if n:
            toks[f"svc:{k}:{min(n, 3)}"] = FEATURE_WEIGHTS["svc"]
    for x in role_form.get("shared_kinds", []):
        toks[f"shared:{x}"] = FEATURE_WEIGHTS["shared"]
    if role_form.get("earliest_layer"):
        toks[f"layer:{role_form['earliest_layer']}"] = FEATURE_WEIGHTS["layer"]
    if role_form.get("anchor_condition"):
        toks[f"anchor:{role_form['anchor_condition']}"] = FEATURE_WEIGHTS["anchor"]
    return toks


def similarity(a: dict, b: dict) -> float:
    ta, tb = signature_tokens(a), signature_tokens(b)
    keys = set(ta) | set(tb)
    if not keys:
        return 0.0
    inter = sum(min(ta.get(k, 0), tb.get(k, 0)) for k in keys)
    union = sum(max(ta.get(k, 0), tb.get(k, 0)) for k in keys)
    return inter / union if union else 0.0


class EpisodeStore:
    """Concrete `EpisodicMemory` over SQLite."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS episodes (
              id TEXT PRIMARY KEY, incident_no TEXT, created_at TEXT, recorded REAL,
              signature TEXT, conclusion TEXT, outcome TEXT, verification TEXT, weight REAL,
              summary TEXT, source TEXT);
            CREATE TABLE IF NOT EXISTS followups (
              episode_id TEXT, kind TEXT, payload TEXT, recorded REAL);
        """)
        self._conn.commit()

    def write_episode(self, episode: dict) -> str:
        eid = episode.get("id") or f"ep-{uuid.uuid4().hex[:12]}"
        ver = episode.get("verification", "self")
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO episodes VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                eid, episode.get("incident_no", ""), episode.get("created_at", ""), time.time(),
                json.dumps(episode.get("signature", {}), sort_keys=True), json.dumps(episode.get("conclusion", {}), sort_keys=True),
                episode.get("outcome", ""), ver, float(episode.get("weight", VERIFICATION_WEIGHT.get(ver, 0.5))),
                episode.get("summary", ""), episode.get("source", "")))
            self._conn.commit()
        return eid

    def add_followup(self, episode_id: str, kind: str, payload: dict) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO followups VALUES (?,?,?,?)",
                               (episode_id, kind, json.dumps(payload, sort_keys=True), time.time()))
            if kind == "verification" and payload.get("verification") in VERIFICATION_WEIGHT:
                v = payload["verification"]
                self._conn.execute("UPDATE episodes SET verification = ?, weight = ? WHERE id = ?",
                                   (v, VERIFICATION_WEIGHT[v], episode_id))
            self._conn.commit()

    def all(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT id, incident_no, created_at, signature, conclusion, outcome, verification, "
                                      "weight, summary, source FROM episodes ORDER BY id").fetchall()
        return [{"id": r[0], "incident_no": r[1], "created_at": r[2], "signature": json.loads(r[3]),
                 "conclusion": json.loads(r[4]), "outcome": r[5], "verification": r[6], "weight": r[7],
                 "summary": r[8], "source": r[9]} for r in rows]

    def retrieve(self, role_signature: dict, k: int = 20) -> list[tuple[dict, float]]:
        scored = [(ep, similarity(role_signature, ep["signature"])) for ep in self.all() if ep["weight"] > 0]
        scored.sort(key=lambda x: (-x[1], x[0]["id"]))
        return scored[:k]

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0])

    def close(self) -> None:
        self._conn.close()


class NoEpisodes:
    def retrieve(self, role_signature: dict, k: int = 20) -> list:
        return []

    def write_episode(self, episode: dict) -> str:
        return ""

    def add_followup(self, episode_id: str, kind: str, payload: dict) -> None:
        return None

    def count(self) -> int:
        return 0
