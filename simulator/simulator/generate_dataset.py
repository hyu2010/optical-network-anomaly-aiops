"""Batch dataset generator — fills incidents.db with ML-training-scale data.

Iterates over every (fault_type, target) combination that's valid in the
loaded YANG topology and samples each one N times.  Each sample applies
timestamp jitter and a small drop probability to non-root alarms, so
within-class samples vary.  Each fault rule produces a *root-cause event*
(incidents.fault_type), a *root-cause alarm* (alarm row with is_root=1)
and a set of *derived alarms* (is_root=0).

Examples:

  python -m simulator.simulator.generate_dataset --clear
  python -m simulator.simulator.generate_dataset --samples-per-target 30
  python -m simulator.simulator.generate_dataset --db data/training.db
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time
from collections import Counter

from . import db, engine, faults, ppe, yang_loader
from .faults import Alarm

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


# ---------------------------------------------------------------------------
# Target enumeration per fault target_kind
# ---------------------------------------------------------------------------

def all_targets_for(t, fault_type: str) -> list[tuple]:
    """Return the list of arg-tuples to call the fault rule with.  These are
    every target valid in the loaded topology."""
    kind = faults.CATALOG[fault_type][1]
    if kind == "link":
        return [(span_id,) for span_id in sorted(t.optical_links)]
    if kind == "link_z":
        # connector_dirty — target is (span_id, z_km, magnitude_db).
        # One z_km per span (mid-span) × two magnitudes:
        #   0.8 dB  → sub-threshold (zero alarms, PPE-only demo)
        #   2.0 dB  → above threshold (one Minor BEFFEC_EXC + PPE step)
        # The 10 samples/target loop then varies PPE noise across draws.
        # Skip spans that carry no OCh channels — in v3 each OMS is modelled
        # as two unidirectional optical-topo links and only one direction
        # carries traffic; the unused direction would produce zero alarms
        # AND zero PPE traces, contributing nothing to the demo.
        channel_carrying_spans = {span_id for tn in t.wdm_tunnels.values()
                                  for span_id in tn.ero}
        out: list[tuple] = []
        for span_id in sorted(t.optical_links):
            if span_id not in channel_carrying_spans:
                continue
            L = t.optical_links[span_id].length_km
            if L <= 0:
                continue
            z_km = round(0.5 * L, 1)
            for mag in (0.8, 2.0):
                out.append((span_id, z_km, mag))
        return out
    if kind == "ne":
        return [(ne_id,) for ne_id in sorted(t.nes)]
    if kind == "board":
        out: list[tuple] = []
        for ne_id, ne in sorted(t.nes.items()):
            for b in ne.boards:
                # FAN boards are the target of fan_failure; SCC has its own
                # semantics but is included so the dataset has variety.
                if b.type in ("OTU", "OA", "FIU", "SCC"):
                    out.append((ne_id, b.uuid))
        return out
    if kind == "port":
        # Port-targeted rules auto-resolve a sensible port from the NE.
        # One sample per NE keeps things balanced; we'll vary further via the
        # jitter+drop sampling rather than enumerating every port.
        return [(ne_id,) for ne_id in sorted(t.nes)]
    return []


# ---------------------------------------------------------------------------
# Stochastic perturbation of a cascade
# ---------------------------------------------------------------------------

def jitter_alarms(alarms: list[Alarm], jitter_ms: int,
                  drop_prob: float, rng: random.Random) -> list[Alarm]:
    """Add +/- jitter_ms ms to each alarm's t_offset_ms and drop ~drop_prob of
    non-root alarms.  Root-cause alarms are always preserved."""
    out: list[Alarm] = []
    for a in alarms:
        if not a.is_root and drop_prob > 0 and rng.random() < drop_prob:
            continue
        new_t = a.t_offset_ms + rng.randint(-jitter_ms, jitter_ms)
        out.append(Alarm(
            name=a.name, layer=a.layer,
            ne=a.ne, board=a.board, port=a.port,
            container_id=a.container_id,
            severity=a.severity,
            t_offset_ms=max(0, new_t),
            is_root=a.is_root,
            group_id=a.group_id,
        ))
    out.sort(key=lambda a: a.t_offset_ms)
    return out


# ---------------------------------------------------------------------------
# One incident (writes a raw row + a suppressed row, sharing the same jitter)
# ---------------------------------------------------------------------------

def generate_one(t, conn, fault_type: str, args: tuple, *,
                 jitter_ms: int, drop_prob: float, seed: int,
                 with_ppe: bool = False) -> tuple[int, int]:
    fn, kind, kw = faults.CATALOG[fault_type]
    seeds = fn(t, *args)
    raw = engine.expand_cascade(seeds, kw)
    rng = random.Random(seed)
    raw_jit = jitter_alarms(raw, jitter_ms, drop_prob, rng)
    # Per-layer NMS poll jitter — derives t_observed_ms.  Reuses the same RNG
    # so the run is deterministic for a given seed.
    raw_jit = engine.apply_polling_delay(raw_jit, rng)
    suppressed = engine.apply_suppression(raw_jit)

    # PPE side — paired with the same fault.  Separate RNG (derived from the
    # same seed) so alarm-jitter determinism is unaffected, while the PPE
    # noise stream is itself reproducible.  The same trace list is recorded
    # on both raw + suppressed incidents because PPE is a separate physical
    # observation and is unaffected by alarm-side suppression.
    ppe_traces: list = []
    if with_ppe:
        ppe_rng = random.Random(seed ^ 0xA17ECEE)   # decorrelated stream
        ppe_traces = engine.expand_ppe(t, fault_type, *args,
                                       rng=ppe_rng, group_id="R1")
        ppe_traces = ppe.apply_polling_delay(ppe_traces, ppe_rng)

    target_str = " ".join(str(a) for a in args) or "(none)"
    note = f"batch seed={seed}"
    sid_raw = db.record(
        conn, fault_type=fault_type, fault_target=target_str, target_kind=kind,
        suppressed=False, topology_id=t.topology_id(),
        alarms=raw_jit, ppe_traces=ppe_traces, note=note + " raw",
    )
    sid_sup = db.record(
        conn, fault_type=fault_type, fault_target=target_str, target_kind=kind,
        suppressed=True, topology_id=t.topology_id(),
        alarms=suppressed, ppe_traces=ppe_traces, note=note + " sup",
    )
    return sid_raw, sid_sup


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def _print_stats(conn) -> None:
    print()
    n_ppe_traces = conn.execute("SELECT count(*) FROM ppe_traces").fetchone()[0]
    n_ppe_anom = conn.execute("SELECT count(*) FROM ppe_anomalies").fetchone()[0]
    if n_ppe_traces:
        n_root_anom = conn.execute(
            "SELECT count(*) FROM ppe_anomalies WHERE is_root=1"
        ).fetchone()[0]
        n_degraded = conn.execute(
            "SELECT count(*) FROM ppe_traces WHERE degraded=1"
        ).fetchone()[0]
        print(f"=== PPE traces: {n_ppe_traces}  "
              f"(anomalies: {n_ppe_anom} total, {n_root_anom} root; "
              f"degraded: {n_degraded}) ===")
        print()
    print("=== Class balance (number of incidents per fault, raw + sup) ===")
    rows = conn.execute(
        """SELECT fault_type, suppressed, count(*) AS n,
                  AVG((SELECT count(*) FROM alarms
                       WHERE incident_id = incidents.id)) AS avg_alarms
           FROM incidents GROUP BY fault_type, suppressed
           ORDER BY fault_type, suppressed""").fetchall()
    print(f"  {'fault_type':<26} {'view':<5} {'n':>6}   {'avg alarms':>11}")
    for ft, sup, n, avg in rows:
        view = "sup" if sup else "raw"
        print(f"  {ft:<26} {view:<5} {n:>6}   {avg:>11.1f}")

    print()
    print("=== Alarm rows ===")
    n_alarm_rows = conn.execute("SELECT count(*) FROM alarms").fetchone()[0]
    # No root/derived split here any more: incidents.db carries no root-cause
    # ground truth (prompt 12).  Root vs. secondary is a rule-derived label
    # computed downstream by the KG builder, not a stored column.
    print(f"  total alarm rows {n_alarm_rows:>10}")

    print()
    print("=== Diversity ===")
    n_total = conn.execute("SELECT count(*) FROM incidents").fetchone()[0]
    n_distinct_alarms = conn.execute("SELECT count(distinct alarm_name) FROM alarms").fetchone()[0]
    n_distinct_layers = conn.execute("SELECT count(distinct layer) FROM alarms").fetchone()[0]
    n_distinct_containers = conn.execute("SELECT count(distinct container_id) FROM alarms").fetchone()[0]
    print(f"  incidents:               {n_total}")
    print(f"  distinct alarm names:    {n_distinct_alarms}")
    print(f"  distinct layers:         {n_distinct_layers}")
    print(f"  distinct container ids:  {n_distinct_containers}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=db.DEFAULT_DB,
                    help=f"path to SQLite DB (default: {db.DEFAULT_DB})")
    ap.add_argument("--samples-per-target", type=int, default=10,
                    help="how many samples per (fault, target) combo (default: 10)")
    ap.add_argument("--jitter-ms", type=int, default=15,
                    help="max +/- ms timestamp jitter per alarm (default: 15)")
    ap.add_argument("--drop-prob", type=float, default=0.05,
                    help="non-root alarm drop probability (default: 0.05)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--clear", action="store_true",
                    help="wipe the DB before generating")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress per-fault progress lines")
    ap.add_argument("--with-ppe", dest="with_ppe", action="store_true",
                    default=True,
                    help="also record paired PPE traces (default: on)")
    ap.add_argument("--no-ppe", dest="with_ppe", action="store_false",
                    help="skip PPE generation — alarms only, faster")
    args = ap.parse_args(argv)

    t = yang_loader.load()
    conn = db.open_db(args.db)
    if args.clear:
        conn.execute("DELETE FROM ppe_anomalies")
        conn.execute("DELETE FROM ppe_traces")
        conn.execute("DELETE FROM alarms")
        conn.execute("DELETE FROM incidents")
        conn.execute("DELETE FROM sqlite_sequence")
        conn.commit()
        print(f"DB cleared: {args.db}")

    rng = random.Random(args.seed)
    written = 0
    skipped = 0
    started = time.time()
    fault_types = list(faults.CATALOG.keys())

    for fault_type in fault_types:
        targets = all_targets_for(t, fault_type)
        n_before = written
        for target_args in targets:
            for _ in range(args.samples_per_target):
                seed = rng.randint(0, 1_000_000)
                try:
                    generate_one(t, conn, fault_type, target_args,
                                 jitter_ms=args.jitter_ms,
                                 drop_prob=args.drop_prob, seed=seed,
                                 with_ppe=args.with_ppe)
                    written += 2  # raw + suppressed
                except (ValueError, TypeError) as exc:
                    skipped += 1
                    if skipped <= 5:
                        print(f"  skip {fault_type} {target_args}: {exc}")
        if not args.quiet:
            print(f"  {fault_type:<26} +{written - n_before:>5} incidents")

    elapsed = time.time() - started
    print()
    print(f"Wrote {written} incidents in {elapsed:.1f}s -> {args.db}")
    if skipped:
        print(f"  skipped {skipped} combos due to errors")

    _print_stats(conn)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
