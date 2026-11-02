"""SQLite recorder for (root-cause event -> alarm cascade) samples.

Schema is per Design/02_simulator_design.md §9 (alarms) and
Design/12_ppe_simulation.md §5 (PPE traces).  One row per incident in
`incidents`; many rows per incident in `alarms` (with `layer` and
`container_id` columns so the correlation rules in Design/04 can be
expressed as plain SQL queries); and optionally many `ppe_traces` per
incident (when the generator runs with `--with-ppe`).

Terminology: an **incident** is one triggered fault and its whole alarm
cascade — the unit the simulator records and the operator triages.  (This
was formerly called a "scenario"; the rename unified it with the
operator-facing term used across the webui, KG and docs.)
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

import numpy as np

from .faults import Alarm

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_DB = os.path.join(ROOT, "data", "incidents.db")

# Blob dtype for PPE samples / baselines.  Little-endian float32 so blobs are
# byte-identical across Windows / Linux x86_64 / Apple Silicon — and so
# `numpy.frombuffer(blob, dtype='<f4')` reconstructs the array exactly.
PPE_BLOB_DTYPE = "<f4"


SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    fault_type   TEXT NOT NULL,
    fault_target TEXT NOT NULL,
    target_kind  TEXT NOT NULL,
    suppressed   INTEGER NOT NULL,
    topology_id  TEXT,
    note         TEXT
);
CREATE TABLE IF NOT EXISTS alarms (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id   INTEGER NOT NULL,
    alarm_name    TEXT NOT NULL,
    ne            TEXT,
    board         TEXT,
    port          TEXT,
    severity      TEXT,
    layer         TEXT,
    container_id  TEXT,
    t_offset_ms   INTEGER,
    t_observed_ms INTEGER,
    -- NOTE: no root-cause ground truth here (prompt 12).  incidents.db is a
    -- pure alarm collection, like a real NMS dump.  RootCause / Secondary are
    -- derived at KG-build time by the rule reasoner (simulator/rules), not
    -- stored.  (PPE anomalies keep their own is_root — a separate signal.)
    FOREIGN KEY (incident_id) REFERENCES incidents(id)
);
CREATE INDEX IF NOT EXISTS idx_alarms_incident ON alarms(incident_id);

-- Per-OCH PPE rework (Design/00 §4.4): one trace per dropping OCh over the
-- whole route, keyed (incident, och, drop_ne).  `total_length_km` is the
-- cumulative ERO length; samples/baseline are float32[n_samples] over 0..L.
CREATE TABLE IF NOT EXISTS ppe_traces (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id     INTEGER NOT NULL,
    och             TEXT NOT NULL,            -- e.g. 'och_ch2'
    drop_ne         TEXT NOT NULL,
    drop_board      TEXT,
    total_length_km REAL NOT NULL DEFAULT 0,  -- cumulative ERO length
    group_id        TEXT NOT NULL DEFAULT '', -- aligned with alarms.group_id
    t_offset_ms     INTEGER NOT NULL,
    t_observed_ms   INTEGER,
    delta_z_m       INTEGER NOT NULL,         -- spatial resolution
    n_samples       INTEGER NOT NULL,
    samples_blob    BLOB NOT NULL,            -- float32[n_samples], <f4, dBm
    baseline_blob   BLOB NOT NULL,            -- float32[n_samples], <f4, dBm
    degraded        INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (incident_id) REFERENCES incidents(id)
);
CREATE INDEX IF NOT EXISTS idx_ppe_incident ON ppe_traces(incident_id);

-- Anomalies carry both the path coordinate (`z_km`) and the reverse-mapped
-- location (`span_id` + `offset_in_span_km`) so the KG can answer "which OMS".
CREATE TABLE IF NOT EXISTS ppe_anomalies (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id          INTEGER NOT NULL,
    kind              TEXT NOT NULL,          -- step | slope | null | rx_offset
    z_km              REAL,
    z_end_km          REAL,
    span_id           TEXT,
    offset_in_span_km REAL,
    magnitude_db      REAL,
    confidence        REAL NOT NULL,
    is_root           INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (trace_id) REFERENCES ppe_traces(id)
);
CREATE INDEX IF NOT EXISTS idx_ppe_anom_trace ON ppe_anomalies(trace_id);
"""


def _migrate_rename(conn: sqlite3.Connection) -> None:
    """Pre-rename legacy DBs (table `scenarios`, `*.scenario_id` columns) in
    place to `incidents` / `incident_id`.

    Runs BEFORE ``executescript(SCHEMA)`` so the new indexes (which reference
    `incident_id`) build against the renamed columns.  SQLite supports
    RENAME COLUMN (>=3.25) and RENAME TABLE; do columns first.
    """
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "scenarios" not in tables or "incidents" in tables:
        return
    alarm_cols = {row[1] for row in conn.execute("PRAGMA table_info(alarms)")}
    if "scenario_id" in alarm_cols:
        conn.execute("ALTER TABLE alarms RENAME COLUMN scenario_id TO incident_id")
    ppe_cols0 = {row[1] for row in conn.execute("PRAGMA table_info(ppe_traces)")}
    if "scenario_id" in ppe_cols0:
        conn.execute("ALTER TABLE ppe_traces RENAME COLUMN scenario_id TO incident_id")
    conn.execute("DROP INDEX IF EXISTS idx_alarms_scenario")
    conn.execute("DROP INDEX IF EXISTS idx_ppe_scenario")
    conn.execute("ALTER TABLE scenarios RENAME TO incidents")
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent column-level migration (t_observed_ms, per-OCh PPE).
    Runs after the tables exist."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(alarms)")}
    if "t_observed_ms" not in cols:
        conn.execute("ALTER TABLE alarms ADD COLUMN t_observed_ms INTEGER")
        # Backfill: for legacy rows we have no polling info, so observed = physical.
        conn.execute("UPDATE alarms SET t_observed_ms = t_offset_ms "
                     "WHERE t_observed_ms IS NULL")
        conn.commit()
    # NOTE: the alarms.is_root / alarms.group_id ground-truth columns were
    # removed (prompt 12) — incidents.db no longer stores root-cause labels.
    # A legacy db may still carry those columns; they are simply left unwritten
    # and ignored by all readers (the KG builder derives labels by rule).

    # Per-OCH PPE rework: the old ppe_traces was keyed (channel, span) with a
    # NOT NULL `span_id` column.  The new per-OCh schema has neither.  PPE data
    # is fully regenerated (`--with-ppe`), so the cleanest, idempotent
    # migration is to drop the legacy PPE tables and let CREATE rebuild them.
    ppe_cols = {row[1] for row in conn.execute("PRAGMA table_info(ppe_traces)")}
    if ppe_cols and "och" not in ppe_cols:
        conn.execute("DROP TABLE IF EXISTS ppe_anomalies")
        conn.execute("DROP TABLE IF EXISTS ppe_traces")
        conn.commit()
        conn.executescript(SCHEMA)


def open_db(path: str | None = None) -> sqlite3.Connection:
    path = path or DEFAULT_DB
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    _migrate_rename(conn)      # legacy scenario->incident, before SCHEMA
    conn.executescript(SCHEMA)
    _migrate(conn)             # column-level additions, after tables exist
    return conn


def record(conn: sqlite3.Connection, *,
           fault_type: str, fault_target: str, target_kind: str,
           suppressed: bool, topology_id: str, alarms: list[Alarm],
           ppe_traces=(), note: str = "", created_at: str | None = None) -> int:
    """Insert one incident row + one alarm row per cascade entry, plus (when
    `ppe_traces` is non-empty) one row per PpeTrace + one row per
    PpeAnomaly.  Returns the incident id.

    `ppe_traces` accepts any iterable of `simulator.ppe.PpeTrace` objects.
    Callers that omit it keep the v1 alarm-only behavior unchanged.

    `created_at` (ISO-8601 UTC) sets the incident's wall-clock base time; when
    omitted it defaults to now().  An alarm's / trace's calendar time is this
    base plus its relative `t_observed_ms`.  Passing it lets a caller keep the
    two raw/suppressed twins of one trigger on a single shared base time.
    """
    now = created_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = conn.execute(
        """INSERT INTO incidents
           (created_at, fault_type, fault_target, target_kind,
            suppressed, topology_id, note)
           VALUES (?,?,?,?,?,?,?)""",
        (now, fault_type, fault_target, target_kind,
         1 if suppressed else 0, topology_id, note),
    )
    iid = cur.lastrowid
    conn.executemany(
        """INSERT INTO alarms
           (incident_id, alarm_name, ne, board, port,
            severity, layer, container_id, t_offset_ms, t_observed_ms)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        [(iid, a.name, a.ne, a.board, a.port,
          a.severity, a.layer, a.container_id, a.t_offset_ms,
          a.t_observed_ms if a.t_observed_ms is not None else a.t_offset_ms)
         for a in alarms],
    )

    for tr in ppe_traces:
        samples_blob = np.asarray(tr.samples, dtype=PPE_BLOB_DTYPE).tobytes()
        baseline_blob = np.asarray(tr.baseline, dtype=PPE_BLOB_DTYPE).tobytes()
        tcur = conn.execute(
            """INSERT INTO ppe_traces
               (incident_id, och, drop_ne, drop_board, total_length_km,
                group_id, t_offset_ms, t_observed_ms,
                delta_z_m, n_samples, samples_blob, baseline_blob, degraded)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (iid, tr.och, tr.drop_ne, tr.drop_board, tr.total_length_km,
             tr.group_id or "R1", tr.t_offset_ms, tr.t_observed_ms,
             tr.delta_z_m, len(tr.samples), samples_blob, baseline_blob,
             1 if tr.degraded else 0),
        )
        trace_id = tcur.lastrowid
        if tr.anomalies:
            conn.executemany(
                """INSERT INTO ppe_anomalies
                   (trace_id, kind, z_km, z_end_km, span_id, offset_in_span_km,
                    magnitude_db, confidence, is_root)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [(trace_id, a.kind, a.z_km, a.z_end_km, a.span_id,
                  a.offset_in_span_km, a.magnitude_db,
                  a.confidence, 1 if a.is_root else 0)
                 for a in tr.anomalies],
            )

    conn.commit()
    return iid


def load_trace_samples(conn: sqlite3.Connection, trace_id: int) -> tuple[np.ndarray, np.ndarray]:
    """Return `(samples, baseline)` numpy arrays for one PPE trace row.

    Convenience for downstream consumers (rule engine, KG builder, plot
    scripts) so they don't have to re-implement the dtype convention.
    """
    row = conn.execute(
        "SELECT samples_blob, baseline_blob FROM ppe_traces WHERE id=?",
        (trace_id,),
    ).fetchone()
    if not row:
        raise KeyError(f"ppe trace #{trace_id} not found")
    return (np.frombuffer(row[0], dtype=PPE_BLOB_DTYPE).copy(),
            np.frombuffer(row[1], dtype=PPE_BLOB_DTYPE).copy())
