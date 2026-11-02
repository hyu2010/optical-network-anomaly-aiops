"""IO helpers — CSV writer, manifest writer, optional NetworkX export."""
from __future__ import annotations

import csv
import os
from typing import Iterable

from .schema import Triple

_COLS = ("subject", "predicate", "object", "source", "weight", "incident_id")
_COLS_WITH_NAMES = ("subject", "subject_name", "predicate",
                    "object", "object_name", "source", "weight", "incident_id")


def write_csv(triples: Iterable[Triple], path: str,
              alarm_names: dict[str, str] | None = None) -> int:
    """Write triples to CSV. Returns row count.

    If `alarm_names` is provided, two extra columns `subject_name` and
    `object_name` are emitted next to `subject` / `object`, populated only
    when the URI is an Alarm URI (and so present in the map). Non-alarm
    URIs are already self-describing (e.g. `port:A.FIU.line_to_B`).
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if alarm_names is None:
            w.writerow(_COLS)
            for t in triples:
                w.writerow([t.subject, t.predicate, t.object,
                            t.source, t.weight, t.incident_id])
                n += 1
        else:
            w.writerow(_COLS_WITH_NAMES)
            for t in triples:
                s_name = alarm_names.get(t.subject, "")
                o_name = alarm_names.get(t.object, "")
                w.writerow([t.subject, s_name, t.predicate,
                            t.object, o_name,
                            t.source, t.weight, t.incident_id])
                n += 1
    return n


def write_manifest_csv(manifest: list[dict], path: str) -> int:
    if not manifest:
        return 0
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    cols = list(manifest[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(manifest)
    return len(manifest)


def to_networkx(triples: Iterable[Triple]):
    """Build a `nx.MultiDiGraph` for interactive exploration.

    Imported lazily so the rest of the package works without NetworkX.
    """
    import networkx as nx
    g = nx.MultiDiGraph()
    for t in triples:
        for node in (t.subject, t.object):
            if node not in g:
                etype = node.split(":", 1)[0] if ":" in node else "literal"
                g.add_node(node, entity_type=etype)
        g.add_edge(t.subject, t.object,
                   predicate=t.predicate, source=t.source,
                   weight=t.weight, incident_id=t.incident_id)
    return g
