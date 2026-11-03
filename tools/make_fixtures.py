#!/usr/bin/env python3
"""Freeze simulator incidents into SQLite fixtures (doc 07 §2).

This is harness code: it calls the simulator's generators exactly as
`/api/trigger` does (fault rule -> cascade -> polling delay -> suppression ->
PPE), but with a seeded RNG so the result is reproducible.  diag_agent itself
never imports these generators.

  python tools/make_fixtures.py fibre-cut          # tests/diag_agent/fixtures/fiber_cut_oms3.db (incidents 42/43)
  python tools/make_fixtures.py variants           # tests/diag_agent/fixtures/variants.db (one per fault type)
  python tools/make_fixtures.py history            # tests/diag_agent/fixtures/history.db (warm-memory sources)
  python tools/make_fixtures.py dataset --per-fault 4 --out data/diag_agent/dataset.db
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

from simulator import db, engine, faults, ppe, yang_loader  # noqa: E402

FIXTURES = ROOT / "tests" / "diag_agent" / "fixtures"
BASE = datetime(2026, 11, 2, 9, 30, 0, tzinfo=timezone.utc)

# One representative trigger per fault type (simulator argument conventions).
VARIANTS: list[tuple[str, list]] = [
    ("fiber_cut", ["span_OMS3_BC"]),
    ("fiber_attenuation_high", ["span_OMS3_BC"]),
    ("connector_dirty", ["span_OMS3_BC", 21.0, 6.0]),
    ("b1_exc_high", ["span_OMS3_BC"]),
    ("b2_exc_high", ["span_OMS5_DB"]),
    ("fec_pre_cor_high", ["span_OMS2_AB"]),
    ("rx_power_drift", ["C", "C.FIU", "C.FIU.line_from_B"]),
    ("config_mismatch", ["C", "C.OTU", "C.OTU.line_from_B"]),
    ("ne_power_off", ["B"]),
    ("board_power_off", ["B", "B.OTU"]),
    ("board_power_off", ["B", "B.FIU"]),
    ("fan_failure", ["E"]),
]


# Past incidents for the warm episodic memory (doc 07 §2 step 4): none involves OMS3.
HISTORY: list[tuple[str, list]] = [
    ("fiber_cut", ["span_OMS6_DE"]),
    ("fiber_cut", ["span_OMS1_AC"]),
    ("fiber_attenuation_high", ["span_OMS2_AB"]),
    ("connector_dirty", ["span_OMS5_BD", 12.0, 6.0]),
    ("fec_pre_cor_high", ["span_OMS6_DE"]),
    ("rx_power_drift", ["D", "D.FIU", "D.FIU.line_from_B"]),
    ("config_mismatch", ["F", "F.OTU", "F.OTU.line_from_E"]),
    ("ne_power_off", ["E"]),
    ("board_power_off", ["D", "D.OTU"]),
    ("board_power_off", ["A", "A.FIU"]),
    ("fan_failure", ["A"]),
    ("fiber_attenuation_high", ["span_OMS7_EF"]),
]


def _topology():
    return yang_loader.load(str(ROOT / "data" / "yang"))


def _targets(topo, fault_type: str) -> list[list]:
    spans = sorted(topo.optical_links)
    nes = sorted(topo.nes)
    if fault_type in ("fiber_cut", "fiber_attenuation_high", "b1_exc_high", "b2_exc_high", "fec_pre_cor_high"):
        return [[s] for s in spans]
    if fault_type == "connector_dirty":
        return [[s, round(0.2 * topo.optical_links[s].length_km, 1), 6.0] for s in spans]
    if fault_type in ("ne_power_off", "fan_failure"):
        return [[n] for n in nes]
    if fault_type == "board_power_off":
        return [[n, f"{n}.{b}"] for n in nes for b in ("OTU", "FIU")]
    if fault_type == "rx_power_drift":
        return [[l.to_ne, f"{l.to_ne}.FIU", f"{l.to_ne}.FIU.line_from_{l.from_ne}"] for l in topo.optical_links.values()]
    if fault_type == "config_mismatch":
        return [[l.to_ne, f"{l.to_ne}.OTU", f"{l.to_ne}.OTU.line_from_{l.from_ne}"] for l in topo.otn_links.values()]
    return []


def record(conn: sqlite3.Connection, topo, fault_type: str, args: list, created_at: datetime, seed: int) -> tuple[int, int]:
    """One trigger -> (raw incident id, suppressed incident id)."""
    fn, target_kind, kw = faults.CATALOG[fault_type]
    rng = random.Random(seed)
    seeds = fn(topo, *args)
    raw = engine.expand_cascade(seeds, kw, group_id="R1")
    raw = engine.apply_polling_delay(raw, rng)
    suppressed = engine.apply_suppression(raw)
    ppe_rng = random.Random(seed + 7919)
    traces = engine.expand_ppe(topo, fault_type, *args, rng=ppe_rng, group_id="R1")
    traces = ppe.apply_polling_delay(traces, ppe_rng)
    target = " ".join(str(a) for a in args) or "(none)"
    base = created_at.isoformat(timespec="seconds")
    rid = db.record(conn, fault_type=fault_type, fault_target=target, target_kind=target_kind, suppressed=False,
                    topology_id=topo.topology_id(), alarms=raw, ppe_traces=traces, note="fixture (raw)",
                    created_at=base)
    sid = db.record(conn, fault_type=fault_type, fault_target=target, target_kind=target_kind, suppressed=True,
                    topology_id=topo.topology_id(), alarms=suppressed, ppe_traces=traces,
                    note="fixture (suppressed)", created_at=base)
    return rid, sid


def _fresh(path: Path, first_id: int | None = None) -> sqlite3.Connection:
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.open_db(str(path))
    if first_id:
        conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('incidents', ?)", (first_id - 1,))
        conn.commit()
    return conn


def make_fibre_cut(seed: int = 7) -> Path:
    path = FIXTURES / "fiber_cut_oms3.db"
    conn = _fresh(path, first_id=42)
    rid, sid = record(conn, _topology(), "fiber_cut", ["span_OMS3_BC"], BASE, seed)
    conn.close()
    print(f"{path}: raw incident {rid}, suppressed incident {sid}")
    return path


def make_variants(seed: int = 11) -> Path:
    path = FIXTURES / "variants.db"
    conn = _fresh(path)
    topo = _topology()
    for i, (ft, args) in enumerate(VARIANTS):
        rid, sid = record(conn, topo, ft, args, BASE + timedelta(hours=i), seed + i)
        print(f"  {rid:3d}/{sid:3d}  {ft} {' '.join(map(str, args))}")
    conn.close()
    print(path)
    return path


def make_history(seed: int = 101) -> Path:
    path = FIXTURES / "history.db"
    conn = _fresh(path, first_id=101)
    topo = _topology()
    for i, (ft, args) in enumerate(HISTORY):
        rid, sid = record(conn, topo, ft, args, BASE - timedelta(days=30 - 2 * i), seed + i)
        print(f"  {rid:3d}/{sid:3d}  {ft} {' '.join(map(str, args))}")
    conn.close()
    print(path)
    return path


def make_dataset(out: Path, per_fault: int, seed: int) -> Path:
    conn = _fresh(out)
    topo = _topology()
    rng = random.Random(seed)
    k = 0
    for ft in faults.CATALOG:
        targets = _targets(topo, ft)
        rng.shuffle(targets)
        for args in targets[:per_fault]:
            rid, _ = record(conn, topo, ft, args, BASE + timedelta(hours=k), seed + 13 * k)
            k += 1
            print(f"  {rid:4d}  {ft} {' '.join(map(str, args))}")
    conn.close()
    print(f"{out}: {k} triggers")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fibre-cut")
    p.add_argument("--seed", type=int, default=7)
    p = sub.add_parser("variants")
    p.add_argument("--seed", type=int, default=11)
    p = sub.add_parser("history")
    p.add_argument("--seed", type=int, default=101)
    p = sub.add_parser("dataset")
    p.add_argument("--out", default=str(ROOT / "data" / "diag_agent" / "dataset.db"))
    p.add_argument("--per-fault", type=int, default=4)
    p.add_argument("--seed", type=int, default=2026)
    sub.add_parser("all")
    a = ap.parse_args()
    if a.cmd == "fibre-cut":
        make_fibre_cut(a.seed)
    elif a.cmd == "variants":
        make_variants(a.seed)
    elif a.cmd == "history":
        make_history(a.seed)
    elif a.cmd == "dataset":
        make_dataset(Path(a.out), a.per_fault, a.seed)
    else:
        make_fibre_cut()
        make_variants()
        make_history()
    return 0


if __name__ == "__main__":
    sys.exit(main())
