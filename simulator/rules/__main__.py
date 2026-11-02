"""CLI: python -m simulator.rules evaluate / explain.

  python -m simulator.rules evaluate                       # full DB, raw + sup
  python -m simulator.rules evaluate --raw-only            # raw cascades only
  python -m simulator.rules evaluate --max-incidents 100
  python -m simulator.rules explain 42                     # rule trace for incident 42
"""
from __future__ import annotations
import argparse
import sqlite3
import sys

from simulator.simulator import db as simdb
from simulator.simulator import yang_loader

from . import evaluate as ev
from .reasoner import infer_root


def cmd_evaluate(args) -> int:
    suppressed = None
    if args.raw_only:
        suppressed = False
    elif args.sup_only:
        suppressed = True
    ev.evaluate(args.db, args.max_incidents, args.show_failures, suppressed)
    return 0


def cmd_explain(args) -> int:
    topo = yang_loader.load()
    conn = simdb.open_db(args.db)
    head = conn.execute(
        "SELECT fault_type, fault_target, suppressed FROM incidents WHERE id=?",
        (args.incident_id,)
    ).fetchone()
    if not head:
        print(f"incident {args.incident_id} not found")
        return 1
    alarms = ev._load_alarms(conn, args.incident_id)
    inf = infer_root(args.incident_id, alarms, topo)
    conn.close()

    print(f"=== incident {args.incident_id} — {head[0]} on {head[1]} "
          f"({'sup' if head[2] else 'raw'}) ===")
    print(f"alarms: {len(alarms)}")
    truth = [a for a in alarms if a.get("is_root")]
    print(f"ground truth root(s): "
          f"{[(a['alarm_name'], a.get('ne'), a.get('container_id')) for a in truth]}")
    print()
    print(f"PREDICTED:")
    print(f"  alarm  = {inf.root_alarm_name} (id {inf.root_alarm_id})")
    print(f"  fault  = {inf.fault_type or '<none>'}")
    print(f"  source = {inf.source}")
    print(f"  candidates: {inf.n_candidates}   secondaries: {inf.n_secondaries}")
    print()
    print("inferred records (first 25):")
    for x in inf.inferred[:25]:
        print(f"  {x.kind:<13} alarm={x.alarm_id:<5} parent={x.parent_id:<5} "
              f"src={x.source:<28} ft={x.fault_type or '-'}")
    if len(inf.inferred) > 25:
        print(f"  ... ({len(inf.inferred) - 25} more)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("evaluate", help="score the reasoner across all incidents")
    e.add_argument("--max-incidents", type=int, default=None)
    e.add_argument("--show-failures", type=int, default=10)
    g = e.add_mutually_exclusive_group()
    g.add_argument("--raw-only", action="store_true")
    g.add_argument("--sup-only", action="store_true")
    e.set_defaults(func=cmd_evaluate)

    x = sub.add_parser("explain", help="show the rule trace for one incident")
    x.add_argument("incident_id", type=int)
    x.set_defaults(func=cmd_explain)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
