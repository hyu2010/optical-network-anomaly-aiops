"""CLI for the knowledge-graph builder.

  python -m simulator.kg build                              # full build → data/kg/
  python -m simulator.kg build --max-incidents 100
  python -m simulator.kg build --no-derived                  # skip locality+temporal
  python -m simulator.kg build --no-correlation              # skip R1–R4 edges
  python -m simulator.kg build --materialise-temporal        # pairwise Close/Earlier
  python -m simulator.kg inspect topology
  python -m simulator.kg inspect incident 42
  python -m simulator.kg stats
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

from . import builder, io as kg_io
from .schema import Triple

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT_DIR = os.path.join(ROOT, "data", "kg")


# ---------------------------------------------------------------------------

def _print_pred_dist(triples: list[Triple], title: str) -> None:
    print(f"=== {title} ===")
    c = Counter(t.predicate for t in triples)
    for k, v in c.most_common():
        print(f"  {k:<22} {v:>10}")


def cmd_build(args) -> int:
    print(f"Building KG from {args.db or 'data/incidents.db'} ...")
    kg = builder.build_kg(
        db_path=args.db,
        max_incidents=args.max_incidents,
        include_derived=not args.no_derived,
        include_correlation=not args.no_correlation,
        include_ppe=not args.no_ppe,
        compact_temporal=not args.materialise_temporal,
    )

    topo_path = os.path.join(OUT_DIR, "topology_triples.csv")
    alm_path  = os.path.join(OUT_DIR, "alarm_triples.csv")
    ppe_path  = os.path.join(OUT_DIR, "ppe_triples.csv")
    man_path  = os.path.join(OUT_DIR, "incident_manifest.csv")

    # Topology has no alarms — write without name columns.
    n_topo = kg_io.write_csv(kg["topology_triples"], topo_path)
    # Alarm triples — enrich with alarm names so `alarm:42:3` becomes
    # human-readable in the CSV.
    n_alm  = kg_io.write_csv(kg["alarm_triples"], alm_path,
                             alarm_names=kg["alarm_names"])
    # PPE triples — alarm URIs appear as objects of PpeIndicates, so reuse
    # the alarm_names map to keep them human-readable here too.
    n_ppe  = kg_io.write_csv(kg["ppe_triples"], ppe_path,
                             alarm_names=kg["alarm_names"])
    n_man  = kg_io.write_manifest_csv(kg["manifest"], man_path)

    print()
    print(f"  topology triples: {n_topo:>8}  -> {topo_path}")
    print(f"  alarm triples:    {n_alm:>8}  -> {alm_path}")
    print(f"  ppe triples:      {n_ppe:>8}  -> {ppe_path}")
    print(f"  incidents:        {n_man:>8}  -> {man_path}")
    print()
    _print_pred_dist(kg["topology_triples"], "topology predicate distribution")
    print()
    _print_pred_dist(kg["alarm_triples"],    "alarm predicate distribution")
    print()
    print("=== alarm-triple source distribution ===")
    c = Counter(t.source for t in kg["alarm_triples"])
    for k, v in c.most_common():
        print(f"  {k:<14} {v:>10}")
    if kg["ppe_triples"]:
        print()
        _print_pred_dist(kg["ppe_triples"], "ppe predicate distribution")
        print()
        print("=== ppe-triple source distribution ===")
        c = Counter(t.source for t in kg["ppe_triples"])
        for k, v in c.most_common():
            print(f"  {k:<14} {v:>10}")
    return 0


def cmd_inspect_topology(args) -> int:
    # Don't touch incidents.db — just walk the YANG and run Stage 1.
    from simulator.simulator import yang_loader
    from . import topology_graph
    topo = yang_loader.load()
    tt = topology_graph.build_topology_triples(topo)

    print(f"topology triples: {len(tt)}")
    _print_pred_dist(tt, "predicate distribution")
    nodes = set()
    for t in tt:
        nodes.add(t.subject)
        nodes.add(t.object)
    etypes = Counter(n.split(":", 1)[0] for n in nodes)
    print()
    print(f"entities: {len(nodes)}  by type:")
    for k, v in etypes.most_common():
        print(f"  {k:<10} {v:>4}")
    return 0


def cmd_inspect_incident(args) -> int:
    kg = builder.build_kg(db_path=args.db)
    sce = [t for t in kg["alarm_triples"] if t.incident_id == args.incident_id]
    if not sce:
        print(f"no triples for incident {args.incident_id}")
        return 1
    head = next((m for m in kg["manifest"] if m["incident_id"] == args.incident_id), None)
    if head:
        print(f"=== incident {head['incident_id']} — {head['fault_type']} "
              f"on {head['fault_target']} ({'sup' if head['suppressed'] else 'raw'}) ===")
        print(f"  alarms: {head['n_alarms']}   roots: {head['n_root']}   "
              f"triples: {head['n_triples']}")
    print()
    _print_pred_dist(sce, "predicate distribution")
    print()
    print("first 25 triples:")
    for t in sce[:25]:
        print(f"  {t.subject:<42} {t.predicate:<18} {t.object}")
    if len(sce) > 25:
        print(f"  ... ({len(sce) - 25} more)")
    # R4 fire log — useful for confirming the cross-service demo
    r4 = [t for t in sce if t.source == "rule:R4"]
    if r4:
        print()
        print(f"R4 (cross-service) edges: {len(r4)}")
        for t in r4[:5]:
            print(f"  {t.subject}  <->  {t.object}")
    return 0


def cmd_stats(args) -> int:
    kg = builder.build_kg(db_path=args.db)
    n_a = sum(m["n_alarms"] for m in kg["manifest"])
    n_r = sum(m["n_root"]   for m in kg["manifest"])
    print(f"topology triples:    {len(kg['topology_triples'])}")
    print(f"alarm triples:       {len(kg['alarm_triples'])}")
    print(f"incidents:           {len(kg['manifest'])}")
    print(f"alarm rows total:    {n_a}  (roots: {n_r})")
    return 0


# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--db", default=None,
                    help="path to incidents.db (default: data/incidents.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="build the KG and write to data/kg/")
    b.add_argument("--max-incidents", type=int, default=None)
    b.add_argument("--no-derived", action="store_true",
                   help="skip locality + temporal predicates")
    b.add_argument("--no-correlation", action="store_true",
                   help="skip R1–R4 Correlated edges")
    b.add_argument("--no-ppe", action="store_true",
                   help="skip PPE-trace / anomaly triples (Design/12 §8)")
    b.add_argument("--materialise-temporal", action="store_true",
                   help="emit pairwise Close/WithinSix/Earlier instead of the "
                        "compact AllClose flag")
    b.set_defaults(func=cmd_build)

    i = sub.add_parser("inspect", help="print KG stats / one incident subgraph")
    isub = i.add_subparsers(dest="what", required=True)
    it = isub.add_parser("topology")
    it.set_defaults(func=cmd_inspect_topology)
    isc = isub.add_parser("incident")
    isc.add_argument("incident_id", type=int)
    isc.set_defaults(func=cmd_inspect_incident)

    s = sub.add_parser("stats", help="print triple-count summary")
    s.set_defaults(func=cmd_stats)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
