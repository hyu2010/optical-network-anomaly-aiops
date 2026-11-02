"""End-to-end demo: load YANG topology, trigger one fault, print cascade,
write to incidents.db, dump the row.

  python -m simulator.simulator.run_demo                        # default fiber_cut(span_23)
  python -m simulator.simulator.run_demo fiber_cut span_23
  python -m simulator.simulator.run_demo ne_power_off ne1
  python -m simulator.simulator.run_demo board_power_off ne2 ne2.OTU
  python -m simulator.simulator.run_demo fan_failure ne2
"""
from __future__ import annotations

import sys
import sqlite3

from . import db, engine, faults, yang_loader


def main(argv: list[str]) -> None:
    if len(argv) < 2:
        argv = [argv[0], "fiber_cut", "span_23"]
    fault_type = argv[1]
    args = argv[2:]

    t = yang_loader.load()
    if fault_type not in faults.CATALOG:
        print(f"unknown fault: {fault_type}; pick from {list(faults.CATALOG)}")
        sys.exit(1)
    fn, target_kind, _kw = faults.CATALOG[fault_type]

    seeds = fn(t, *args)
    raw = engine.expand_cascade(seeds, faults.keywords_for(fault_type))
    suppressed = engine.apply_suppression(raw)

    print(f"=== {fault_type} target={args}  topology={t.topology_id()} ===")
    print(f"\n--- raw cascade ({len(raw)} alarms) ---")
    _print(raw)
    print(f"\n--- suppressed view ({len(suppressed)} alarms) ---")
    _print(suppressed)

    conn = db.open_db()
    sid_raw = db.record(conn, fault_type=fault_type,
                        fault_target=" ".join(args),
                        target_kind=target_kind,
                        suppressed=False, topology_id=t.topology_id(),
                        alarms=raw, note="run_demo (raw)")
    sid_sup = db.record(conn, fault_type=fault_type,
                        fault_target=" ".join(args),
                        target_kind=target_kind,
                        suppressed=True, topology_id=t.topology_id(),
                        alarms=suppressed, note="run_demo (suppressed)")
    print(f"\nrecorded incidents: raw=#{sid_raw} suppressed=#{sid_sup}")
    rows = conn.execute(
        "SELECT id, fault_type, fault_target, suppressed, "
        "(SELECT count(*) FROM alarms WHERE incident_id=incidents.id) AS n "
        "FROM incidents ORDER BY id DESC LIMIT 6"
    ).fetchall()
    print("\nlast 6 incidents in DB:")
    for r in rows:
        print(f"  #{r[0]}  {r[1]:<16} target={r[2]:<14} sup={r[3]}  alarms={r[4]}")
    conn.close()


def _print(alarms):
    for a in alarms:
        root = " *ROOT*" if a.is_root else ""
        # inventory uuids already encode NE.board.port; pick the most specific.
        loc = a.port or a.board or a.ne or "-"
        print(f"  +{a.t_offset_ms:>5}ms  [{a.severity:<8}] {a.layer:<11} "
              f"{a.name:<14} @ {loc:<28} container={a.container_id}{root}")


if __name__ == "__main__":
    main(sys.argv)
