"""Evaluate the rule-based reasoner against `is_root` ground truth.

NOTE (prompt 12): incidents.db no longer stores root-cause ground truth — the
rule reasoner is now what PRODUCES the RootCause/Secondary labels at KG-build
time, so there is no answer key in incidents.db to grade it against. This
evaluator therefore exits early on a current (label-less) incidents.db; it still
runs against a legacy db that carries the old `is_root`/`group_id` columns.

For each incident in `incidents.db` the reasoner picks one root-cause alarm
and (optionally) names a fault class.  This module compares those picks
against:

  • the alarm row(s) with `is_root=1` (ground-truth root)
  • `incidents.fault_type` (ground-truth fault class)

and emits:

  • per-fault-class accuracy table (raw + suppressed views, separately)
  • aggregate confusion matrix on fault_type
  • a sample of failed incidents with the predicted vs actual root

Compound incidents (`fault_type` containing '+') have multiple roots; the
reasoner picks one, so we score them more leniently — a compound is correct
iff the predicted root matches ANY of the compound's roots.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from typing import Optional

from simulator.simulator import db as simdb
from simulator.simulator import yang_loader

from .reasoner import infer_root


def _load_incidents(conn: sqlite3.Connection,
                    suppressed: Optional[bool] = None,
                    limit: Optional[int] = None) -> list[dict]:
    q = "SELECT id, fault_type, fault_target, suppressed FROM incidents"
    args: tuple = ()
    if suppressed is not None:
        q += " WHERE suppressed=?"
        args = (1 if suppressed else 0,)
    q += " ORDER BY id"
    if limit:
        q += f" LIMIT {limit}"
    cur = conn.execute(q, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _load_alarms(conn: sqlite3.Connection, sid: int) -> list[dict]:
    cur = conn.execute(
        """SELECT id, alarm_name, ne, board, port, severity, layer, container_id,
                  t_offset_ms, t_observed_ms, is_root, group_id
           FROM alarms WHERE incident_id=? ORDER BY id""",
        (sid,),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _ground_truth_roots(alarms: list[dict]) -> dict[str, dict]:
    """group_id -> root alarm row. Compound incidents have multiple groups."""
    out: dict[str, dict] = {}
    for a in alarms:
        if a.get("is_root"):
            gid = a.get("group_id") or "R1"
            out[gid] = a
    return out


def _incident_ground_truth_fault_types(incident_ft: str) -> list[str]:
    """fiber_cut -> ['fiber_cut']; A+B -> ['A', 'B']."""
    return [s.strip() for s in incident_ft.split("+") if s.strip()]


def evaluate(db_path: Optional[str] = None,
             max_incidents: Optional[int] = None,
             show_failures: int = 10,
             suppressed_filter: Optional[bool] = None) -> dict:
    topo = yang_loader.load()
    conn = simdb.open_db(db_path)

    # Ground truth (is_root) was removed from incidents.db (prompt 12): the rule
    # reasoner now PRODUCES the root labels, so there is no longer an answer key
    # in incidents.db to grade it against.  Bail gracefully on a label-less db;
    # a legacy db that still carries the columns can still be evaluated.
    alarm_cols = {r[1] for r in conn.execute("PRAGMA table_info(alarms)")}
    if "is_root" not in alarm_cols:
        print("incidents.db carries no root-cause ground truth (prompt 12) — the "
              "rule reasoner now produces these labels, so there is no answer key "
              "in incidents.db to evaluate it against. Nothing to do.")
        conn.close()
        return {}

    incidents = _load_incidents(conn, suppressed_filter, max_incidents)
    if not incidents:
        print("no incidents to evaluate")
        return {}

    # Counters per fault class and view.
    per_class_total: dict[tuple[str, bool], int] = defaultdict(int)
    per_class_root_ok: dict[tuple[str, bool], int] = defaultdict(int)
    per_class_fault_ok: dict[tuple[str, bool], int] = defaultdict(int)
    confusion: Counter = Counter()
    failures: list[dict] = []
    n_compound = 0

    for s in incidents:
        alarms = _load_alarms(conn, s["id"])
        if not alarms:
            continue
        truth_roots = _ground_truth_roots(alarms)
        truth_fts = _incident_ground_truth_fault_types(s["fault_type"])
        is_compound = "+" in s["fault_type"]
        if is_compound:
            n_compound += 1

        inf = infer_root(s["id"], alarms, topo)

        # Root match: predicted alarm_name matches the ground-truth root's name
        # AND ground-truth root's NE.  For compound: match against any root.
        root_ok = any(
            inf.root_alarm_name == g["alarm_name"]
            and inf.root_alarm_id == g["id"]
            for g in truth_roots.values()
        )
        # Looser fall-back: same name + NE (in case the picked id is wrong but
        # the inferred root is at the same place).
        if not root_ok:
            picked = next((a for a in alarms if a["id"] == inf.root_alarm_id), None)
            if picked:
                root_ok = any(
                    picked["alarm_name"] == g["alarm_name"]
                    and picked.get("ne") == g.get("ne")
                    for g in truth_roots.values()
                )

        fault_ok = inf.fault_type in truth_fts

        view = bool(s["suppressed"])
        # For compound incidents we aggregate against the compound id so the
        # per-class table doesn't double-count.
        key_class = s["fault_type"] if not is_compound else "[compound]"
        per_class_total[(key_class, view)]   += 1
        per_class_root_ok[(key_class, view)] += int(root_ok)
        per_class_fault_ok[(key_class, view)] += int(fault_ok)

        # Confusion only for single-fault incidents.
        if not is_compound:
            confusion[(s["fault_type"], inf.fault_type or "<none>")] += 1

        if not root_ok and len(failures) < show_failures:
            picked = next((a for a in alarms if a["id"] == inf.root_alarm_id), None)
            failures.append({
                "incident_id": s["id"],
                "fault_type":  s["fault_type"],
                "fault_target": s["fault_target"],
                "suppressed":  view,
                "predicted_root_name":  inf.root_alarm_name,
                "predicted_root_ne":    (picked or {}).get("ne", ""),
                "predicted_fault_type": inf.fault_type or "<none>",
                "predicted_source":     inf.source,
                "truth_root_names":     [g["alarm_name"] for g in truth_roots.values()],
            })

    conn.close()

    # Print tables.
    print(f"=== rule-engine evaluation ===")
    print(f"incidents: {len(incidents)} (compound: {n_compound})")
    print()
    print(f"{'fault_type':<30} {'view':<5} {'n':>6} {'root_ok':>10} {'class_ok':>10}")
    classes = sorted({k for k, _ in per_class_total.keys()})
    overall = [0, 0, 0]
    for cls in classes:
        for view in (False, True):
            n = per_class_total.get((cls, view), 0)
            if not n:
                continue
            ro = per_class_root_ok[(cls, view)]
            fo = per_class_fault_ok[(cls, view)]
            print(f"  {cls:<28} {'sup' if view else 'raw':<5} {n:>6} "
                  f"{ro/n:>9.1%} {fo/n:>9.1%}")
            overall[0] += n
            overall[1] += ro
            overall[2] += fo
    print(f"  {'OVERALL':<28} {'':<5} {overall[0]:>6} "
          f"{overall[1]/max(1,overall[0]):>9.1%} {overall[2]/max(1,overall[0]):>9.1%}")
    print()

    # Confusion matrix (single-fault incidents only).
    if confusion:
        actuals    = sorted({a for a, _ in confusion})
        predicteds = sorted({p for _, p in confusion})
        print("=== confusion matrix (single-fault incidents) ===")
        header = f"  {'actual / pred':<26} " + " ".join(f"{p[:10]:>10}" for p in predicteds)
        print(header)
        for a in actuals:
            row_n = sum(confusion[(a, p)] for p in predicteds)
            cells = []
            for p in predicteds:
                v = confusion[(a, p)]
                pct = f"{v/row_n:.0%}" if row_n else "-"
                cells.append(f"{pct:>10}")
            print(f"  {a:<26} " + " ".join(cells) + f"   (n={row_n})")
        print()

    if failures:
        print(f"=== sample failures (showing {len(failures)}) ===")
        for f in failures:
            print(f"  #{f['incident_id']} {f['fault_type']} target={f['fault_target']} "
                  f"{'sup' if f['suppressed'] else 'raw'}")
            print(f"     predicted: {f['predicted_root_name']} @ {f['predicted_root_ne']}  "
                  f"=> {f['predicted_fault_type']}  ({f['predicted_source']})")
            print(f"     truth:    {f['truth_root_names']}")

    return {
        "incidents":  len(incidents),
        "compound":   n_compound,
        "overall":    overall,
        "confusion":  dict(confusion),
        "failures":   failures,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None)
    ap.add_argument("--max-incidents", type=int, default=None)
    ap.add_argument("--show-failures", type=int, default=10)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--raw-only", action="store_true", help="evaluate only raw incidents")
    g.add_argument("--sup-only", action="store_true", help="evaluate only suppressed incidents")
    args = ap.parse_args(argv)

    suppressed = None
    if args.raw_only:
        suppressed = False
    elif args.sup_only:
        suppressed = True

    evaluate(args.db, args.max_incidents, args.show_failures, suppressed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
