"""CLI — Design/10 §11.

  python -m simulator.kg_engine run --source simulator [--db PATH] [--once]
                              [--log-dir PATH] [--max-incidents N]
  python -m simulator.kg_engine status [--log-dir PATH]

`replay` and `export` (Design/10 §11) are R-2+ work and raise
NotImplementedError so the CLI surface lines up with the design.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from .adapters import SimulatorAdapter, YangWatchAdapter
from .engine import Engine
from .subscribers import (CsvMirrorSubscriber, EventLogSubscriber,
                          RuleEngineSubscriber)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_LOG_DIR = os.path.join(ROOT, "data", "kg_engine")
DEFAULT_KG_DIR = os.path.join(ROOT, "data", "kg")


async def _run(args) -> int:
    snapshots_dir = args.snapshots_dir if args.snapshot else None
    engine = Engine(snapshots_dir=snapshots_dir,
                    snapshot_interval_s=args.snapshot_interval,
                    sliding_window=args.sliding_window)
    engine.register_subscriber(EventLogSubscriber(args.log_dir))

    if args.mirror_csv:
        engine.register_subscriber(CsvMirrorSubscriber(engine.state,
                                                       kg_dir=args.kg_dir))

    if args.rule_engine:
        engine.register_subscriber(RuleEngineSubscriber(engine))

    if args.yang_watch:
        engine.register_adapter(YangWatchAdapter()(engine))

    if args.source == "simulator":
        adapter = SimulatorAdapter(db_path=args.db, once=args.once,
                                   max_incidents=args.max_incidents,
                                   sliding_window=args.sliding_window)
    else:
        print(f"unknown source: {args.source!r}", file=sys.stderr)
        return 2

    engine.register_adapter(adapter(engine))
    await engine.run()
    return 0


def cmd_run(args) -> int:
    return asyncio.run(_run(args))


def cmd_status(args) -> int:
    """Print a one-shot count of events / triples in the newest log file."""
    if not os.path.isdir(args.log_dir):
        print(f"no log dir at {args.log_dir}")
        return 1
    files = sorted(f for f in os.listdir(args.log_dir)
                   if f.startswith("events.ndjson."))
    if not files:
        print(f"no event-log files in {args.log_dir}")
        return 1
    newest = os.path.join(args.log_dir, files[-1])
    import json
    n_events = 0
    n_adds = 0
    n_removes = 0
    by_kind: dict[str, int] = {}
    with open(newest, encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            n_events += 1
            kind = rec["event"]["kind"]
            by_kind[kind] = by_kind.get(kind, 0) + 1
            n_adds    += len(rec["delta"]["adds"])
            n_removes += len(rec["delta"]["removes"])
    print(f"log file:        {newest}")
    print(f"events:          {n_events}")
    print(f"triple adds:     {n_adds}")
    print(f"triple removes:  {n_removes}")
    print("by kind:")
    for k, v in sorted(by_kind.items(), key=lambda x: -x[1]):
        print(f"  {k:<22} {v:>8}")
    return 0


def cmd_replay(args) -> int:
    raise NotImplementedError("`replay` is R-2+ work — see Design/10 §11")


def cmd_export(args) -> int:
    raise NotImplementedError("`export` is R-2+ work — see Design/10 §11")


def cmd_web(args) -> int:
    """R-4 — launch the demo Flask SSE server."""
    from . import web
    web.main(host=args.host, port=args.port, db_path=args.db,
             mirror_csv=not args.no_mirror_csv, kg_dir=args.kg_dir)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run the engine")
    r.add_argument("--source", default="simulator",
                   choices=["simulator"])
    r.add_argument("--db", default=None, help="path to incidents.db")
    r.add_argument("--log-dir", default=DEFAULT_LOG_DIR,
                   help=f"event log dir (default: {DEFAULT_LOG_DIR})")
    r.add_argument("--once", action="store_true",
                   help="drain existing incidents then exit")
    r.add_argument("--max-incidents", type=int, default=None)
    r.add_argument("--yang-watch", action="store_true",
                   help="(R-2) watch data/yang/*.json for topology changes "
                        "and emit topology.snapshot events")
    r.add_argument("--snapshot", action="store_true",
                   help="(R-2) enable periodic state snapshots for restart "
                        "recovery")
    r.add_argument("--snapshots-dir", default=os.path.join(DEFAULT_LOG_DIR,
                                                            "snapshots"))
    r.add_argument("--snapshot-interval", type=float, default=60.0,
                   help="seconds between snapshot writes (default 60)")
    r.add_argument("--sliding-window", action="store_true",
                   help="(R-3) group alarms by R1-R4 over a 6-min window "
                        "instead of one-incident-per-incident")
    r.add_argument("--rule-engine", action="store_true",
                   help="(R-3) enable rule-engine subscriber — emits "
                        "RootCause triples tagged source='rule:fingerprint'")
    r.add_argument("--mirror-csv", action="store_true",
                   help="mirror engine state into <kg-dir>/*.csv in real "
                        "time: rewrite topology_triples.csv on topology "
                        "changes, append to alarm_triples.csv + "
                        "incident_manifest.csv at each incident.boundary")
    r.add_argument("--kg-dir", default=DEFAULT_KG_DIR,
                   help=f"CSV mirror output dir (default: {DEFAULT_KG_DIR})")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("status", help="summarise newest event-log file")
    s.add_argument("--log-dir", default=DEFAULT_LOG_DIR)
    s.set_defaults(func=cmd_status)

    rp = sub.add_parser("replay", help="(R-2+) replay events from a log")
    rp.set_defaults(func=cmd_replay)

    ex = sub.add_parser("export", help="(R-2+) export batch CSVs from log")
    ex.set_defaults(func=cmd_export)

    w = sub.add_parser("web", help="(R-4) launch the demo SSE web UI")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=5001)
    w.add_argument("--db",   default=None,
                   help="path to incidents.db (default: data/incidents.db)")
    w.add_argument("--no-mirror-csv", action="store_true",
                   help="disable the CsvMirrorSubscriber (mirror is on by "
                        "default in the web demo so fault clicks update "
                        "data/kg/*.csv in real time)")
    w.add_argument("--kg-dir", default=None,
                   help=f"CSV mirror output dir (default: {DEFAULT_KG_DIR})")
    w.set_defaults(func=cmd_web)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
