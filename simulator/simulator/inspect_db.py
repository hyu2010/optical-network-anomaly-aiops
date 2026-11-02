"""Inspector for incidents.db — quick read-only queries from the CLI.

Usage:

  python -m simulator.simulator.inspect_db summary
  python -m simulator.simulator.inspect_db classes              [--db path]
  python -m simulator.simulator.inspect_db incident 42          # show incident detail
  python -m simulator.simulator.inspect_db sample fiber_cut     # one full sample of this fault
  python -m simulator.simulator.inspect_db find OTS_LOS         # incidents containing this alarm
  python -m simulator.simulator.inspect_db cooccur fiber_cut    # alarm names + frequencies for this fault
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_DB = os.path.join(ROOT, "data", "incidents.db")


def open_db(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        print(f"DB not found: {path}", file=sys.stderr)
        sys.exit(1)
    return sqlite3.connect(path)


# ---------------------------------------------------------------------------

def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def cmd_summary(conn: sqlite3.Connection, _args) -> None:
    n_sce = conn.execute("SELECT count(*) FROM incidents").fetchone()[0]
    n_alm = conn.execute("SELECT count(*) FROM alarms").fetchone()[0]
    n_dist = conn.execute("SELECT count(distinct alarm_name) FROM alarms").fetchone()[0]
    n_lay  = conn.execute("SELECT count(distinct layer) FROM alarms").fetchone()[0]
    n_ctn  = conn.execute("SELECT count(distinct container_id) FROM alarms").fetchone()[0]
    print(f"incidents:             {n_sce}")
    print(f"alarm rows:            {n_alm}")
    print(f"  (root labels are not stored — derived by rule at KG-build time)")
    print(f"distinct alarm names:  {n_dist}")
    print(f"distinct layers:       {n_lay}")
    print(f"distinct container ids:{n_ctn}")

    if _has_table(conn, "ppe_traces"):
        n_tr = conn.execute("SELECT count(*) FROM ppe_traces").fetchone()[0]
        if n_tr:
            n_deg = conn.execute(
                "SELECT count(*) FROM ppe_traces WHERE degraded=1"
            ).fetchone()[0]
            n_anom = conn.execute(
                "SELECT count(*) FROM ppe_anomalies"
            ).fetchone()[0]
            n_root_anom = conn.execute(
                "SELECT count(*) FROM ppe_anomalies WHERE is_root=1"
            ).fetchone()[0]
            n_distinct_kinds = conn.execute(
                "SELECT count(distinct kind) FROM ppe_anomalies"
            ).fetchone()[0]
            print(f"ppe traces:            {n_tr}  ({n_deg} degraded)")
            print(f"ppe anomalies:         {n_anom}  ({n_root_anom} root; "
                  f"{n_distinct_kinds} distinct kinds)")

    print()
    print("most recent 5 incidents:")
    for r in conn.execute(
        """SELECT id, created_at, fault_type, fault_target, suppressed,
                  (SELECT count(*) FROM alarms WHERE incident_id=incidents.id)
           FROM incidents ORDER BY id DESC LIMIT 5"""):
        view = "sup" if r[4] else "raw"
        print(f"  #{r[0]:<5} {r[1]}  {r[2]:<26} target={r[3]:<14} {view} alarms={r[5]}")


def cmd_classes(conn: sqlite3.Connection, _args) -> None:
    print(f"{'fault_type':<26} {'view':<5} {'n':>6} {'avg_alarms':>10} {'min':>4} {'max':>4} {'#unique_targets':>15}")
    for r in conn.execute("""
        SELECT fault_type, suppressed,
               count(*) AS n,
               AVG((SELECT count(*) FROM alarms WHERE incident_id=s.id)) AS avg_a,
               MIN((SELECT count(*) FROM alarms WHERE incident_id=s.id)) AS min_a,
               MAX((SELECT count(*) FROM alarms WHERE incident_id=s.id)) AS max_a,
               count(distinct fault_target)
        FROM incidents s
        GROUP BY fault_type, suppressed
        ORDER BY fault_type, suppressed
    """):
        ft, sup, n, avg_a, min_a, max_a, n_t = r
        view = "sup" if sup else "raw"
        print(f"{ft:<26} {view:<5} {n:>6} {avg_a:>10.1f} {min_a:>4} {max_a:>4} {n_t:>15}")


def cmd_incident(conn: sqlite3.Connection, args) -> None:
    sid = args.id
    s = conn.execute(
        "SELECT id, created_at, fault_type, fault_target, target_kind, "
        "suppressed, topology_id, note FROM incidents WHERE id=?", (sid,)
    ).fetchone()
    if not s:
        print(f"incident #{sid} not found")
        return
    print(f"=== incident #{s[0]} ===")
    print(f"  created_at:    {s[1]}")
    print(f"  fault_type:    {s[2]}    <-- root cause event")
    print(f"  fault_target:  {s[3]}  ({s[4]})")
    print(f"  view:          {'suppressed' if s[5] else 'raw'}")
    print(f"  topology_id:   {s[6]}")
    if s[7]: print(f"  note:          {s[7]}")
    print()
    rows = conn.execute(
        "SELECT alarm_name, ne, board, port, layer, container_id, severity, "
        "t_offset_ms FROM alarms WHERE incident_id=? ORDER BY t_offset_ms",
        (sid,)
    ).fetchall()
    # No 'root' column: incidents.db carries no root-cause ground truth.
    print(f"{'#':>3} {'name':<14} {'sev':<9} {'layer':<6} "
          f"{'location':<32} {'container':<14} {'+ms':>5}")
    for i, r in enumerate(rows, 1):
        loc = r[3] or r[2] or r[1] or "-"
        print(f"{i:>3} {r[0]:<14} {r[6]:<9} {r[4]:<6} {loc:<32} "
              f"{r[5] or '':<14} {r[7]:>5}")


def cmd_sample(conn: sqlite3.Connection, args) -> None:
    sid = conn.execute(
        "SELECT id FROM incidents WHERE fault_type=? "
        "AND suppressed=? ORDER BY id LIMIT 1",
        (args.fault_type, 1 if args.suppressed else 0),
    ).fetchone()
    if not sid:
        print(f"no incident with fault_type={args.fault_type} suppressed={args.suppressed}")
        return
    args2 = argparse.Namespace(id=sid[0])
    cmd_incident(conn, args2)


def cmd_find(conn: sqlite3.Connection, args) -> None:
    rows = conn.execute("""
        SELECT s.id, s.fault_type, s.fault_target, s.suppressed,
               count(*) AS hits
        FROM alarms a JOIN incidents s ON s.id = a.incident_id
        WHERE a.alarm_name = ?
        GROUP BY s.id ORDER BY s.id DESC LIMIT 30
    """, (args.alarm_name,)).fetchall()
    if not rows:
        print(f"no incident contains alarm {args.alarm_name!r}")
        return
    print(f"30 most recent incidents containing {args.alarm_name!r}:")
    for r in rows:
        view = "sup" if r[3] else "raw"
        print(f"  #{r[0]:<5} {r[1]:<26} target={r[2]:<14} {view}  ({r[4]} occurrences)")


def cmd_cooccur(conn: sqlite3.Connection, args) -> None:
    rows = conn.execute("""
        SELECT a.alarm_name, count(*) AS n,
               0 AS as_root
        FROM alarms a JOIN incidents s ON s.id = a.incident_id
        WHERE s.fault_type = ? AND s.suppressed = ?
        GROUP BY a.alarm_name
        ORDER BY n DESC
    """, (args.fault_type, 1 if args.suppressed else 0)).fetchall()
    if not rows:
        print(f"no incidents with fault_type={args.fault_type} suppressed={args.suppressed}")
        return
    n_sce = conn.execute(
        "SELECT count(*) FROM incidents WHERE fault_type=? AND suppressed=?",
        (args.fault_type, 1 if args.suppressed else 0)
    ).fetchone()[0]
    print(f"alarm-name frequencies in {n_sce} incidents of "
          f"{args.fault_type} ({'suppressed' if args.suppressed else 'raw'}):")
    print(f"  {'alarm':<22} {'n':>6}  {'%incidents':>10}  {'asRoot':>8}")
    for nm, n, n_root in rows:
        pct = 100 * n / n_sce
        print(f"  {nm:<22} {n:>6}  {pct:>9.1f}%  {n_root:>8}")


# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("summary", help="counts and most recent incidents")
    sub.add_parser("classes", help="per-fault class balance + cascade sizes")

    p = sub.add_parser("incident", help="full detail of one incident by id")
    p.add_argument("id", type=int)

    p = sub.add_parser("sample", help="one full sample for a given fault type")
    p.add_argument("fault_type")
    p.add_argument("--suppressed", action="store_true")

    p = sub.add_parser("find", help="recent incidents containing this alarm name")
    p.add_argument("alarm_name")

    p = sub.add_parser("cooccur", help="alarm-name frequencies inside one fault class")
    p.add_argument("fault_type")
    p.add_argument("--suppressed", action="store_true")

    args = ap.parse_args(argv)
    conn = open_db(args.db)
    try:
        {
            "summary":  cmd_summary,
            "classes":  cmd_classes,
            "incident": cmd_incident,
            "sample":   cmd_sample,
            "find":     cmd_find,
            "cooccur":  cmd_cooccur,
        }[args.cmd](conn, args)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
