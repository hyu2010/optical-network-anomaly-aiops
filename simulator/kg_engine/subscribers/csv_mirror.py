"""Mirror engine state into data/kg/*.csv in real time.

Companion to the batch builder (`src/kg/builder.py`). Where the batch writes
the whole KG once and exits, this subscriber keeps four CSVs current as the
engine processes events:

  - ``topology_triples.csv``  rewritten on every topology delta
  - ``alarm_triples.csv``     appended at each ``incident.boundary``
  - ``ppe_triples.csv``       appended at each ``incident.boundary`` (PPE triples
                               carried under the same incident_id are written here
                               instead of ``alarm_triples.csv``)
  - ``incident_manifest.csv`` appended at each ``incident.boundary``

Column shapes match the batch builder (``src/kg/io.py``) so the same
downstream consumers work against both files.

The subscriber takes a reference to ``engine.state`` so it can re-read the
authoritative triple set instead of reconstructing it from deltas. Writes
are atomic (write-to-temp + ``os.replace``) for the topology rewrite, and
single-shot ``open(..., "a")`` blocks for the alarm/ppe/manifest appends.
"""
from __future__ import annotations

import csv
import logging
import os
import tempfile

from simulator.kg.schema import (
    HAS_ANOMALY, LOCATED_AT, MEASURED_BY, MEASURED_ON, MEASURES_SPAN,
    ON_SPAN, PPE_INDICATES, PPE_ROOT_CAUSE, ROOT_CAUSE, Triple,
)

from ..events import Delta, Event, EventKind
from ..state import StateStore

log = logging.getLogger(__name__)

_TOPO_COLS = ("subject", "predicate", "object", "source", "weight", "incident_id")
_ALARM_COLS = ("subject", "subject_name", "predicate",
               "object", "object_name", "source", "weight", "incident_id")
_PPE_COLS = _ALARM_COLS  # same shape as alarm_triples.csv per Design/12 §8
_MANIFEST_COLS = ("incident_id", "fault_type", "fault_target", "target_kind",
                  "suppressed", "topology_id",
                  "n_alarms", "n_root", "n_triples", "n_ppe_triples")

# The eight PPE predicates from Design/12 §8.2. A triple is routed to
# ppe_triples.csv iff its predicate is in this set — regardless of `source`
# tag, because OnSpan carries source="topology" but still belongs in the
# PPE file (it lives only because PPE created a FibrePosition node).
_PPE_PREDICATES = frozenset({
    MEASURED_ON, MEASURES_SPAN, MEASURED_BY, HAS_ANOMALY,
    LOCATED_AT, ON_SPAN, PPE_INDICATES, PPE_ROOT_CAUSE,
})


class CsvMirrorSubscriber:
    """Keep ``<kg_dir>/{topology,alarm}_triples.csv`` + ``incident_manifest.csv``
    in sync with engine state.

    Parameters
    ----------
    state : StateStore
        The engine's live state. The subscriber reads from it; it does not
        mutate it.
    kg_dir : str
        Output directory (default ``data/kg``). Created if missing.
    """

    def __init__(self, state: StateStore, kg_dir: str = "data/kg") -> None:
        os.makedirs(kg_dir, exist_ok=True)
        self.state = state
        self.kg_dir = kg_dir
        self.topo_path = os.path.join(kg_dir, "topology_triples.csv")
        self.alarm_path = os.path.join(kg_dir, "alarm_triples.csv")
        self.ppe_path = os.path.join(kg_dir, "ppe_triples.csv")
        self.manifest_path = os.path.join(kg_dir, "incident_manifest.csv")
        # Track incidents we've already emitted so a replay/duplicate boundary
        # doesn't double-write. Seeded from the manifest so restarts pick up
        # where the previous process left off.
        self._written_incidents: set[int] = _read_manifest_ids(self.manifest_path)
        # Lazily ensure alarm + ppe + manifest CSVs have headers when first appended.
        self._alarm_header_ready = os.path.exists(self.alarm_path)
        self._ppe_header_ready = os.path.exists(self.ppe_path)
        self._manifest_header_ready = os.path.exists(self.manifest_path)

    # -- subscriber protocol ------------------------------------------------

    async def on_delta(self, delta: Delta, event: Event) -> None:
        kind = event.kind
        if kind == EventKind.TOPOLOGY_SNAPSHOT or _touches_topology(delta):
            self._rewrite_topology()
        if kind == EventKind.INCIDENT_BOUNDARY:
            incident_id = event.payload.get("incident_id")
            if incident_id is not None:
                self._append_incident(int(incident_id))

    # -- topology -----------------------------------------------------------

    def _rewrite_topology(self) -> None:
        """Atomically replace topology_triples.csv with the current set."""
        triples = [t for t in self.state.triples.all_triples
                   if t.source == "topology"]
        triples.sort(key=lambda t: (t.subject, t.predicate, t.object))

        fd, tmp_path = tempfile.mkstemp(prefix=".topology_triples.",
                                        suffix=".csv.tmp",
                                        dir=self.kg_dir)
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(_TOPO_COLS)
                for t in triples:
                    w.writerow([t.subject, t.predicate, t.object,
                                t.source, t.weight, t.incident_id])
            os.replace(tmp_path, self.topo_path)
        except Exception:
            log.exception("topology csv rewrite failed")
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # -- per-incident -------------------------------------------------------

    def _append_incident(self, incident_id: int) -> None:
        if incident_id in self._written_incidents:
            return
        inc = self.state.incidents.get(incident_id)
        if inc is None:
            return
        triples = list(self.state.triples.by_incident.get(incident_id, ()))
        if not triples:
            return

        alarm_names = {
            f"alarm:{incident_id}:{r['id']}": r["alarm_name"]
            for r in inc.alarm_rows
        }

        # Split by predicate so PPE triples go to ppe_triples.csv and
        # alarm/locality/rule triples go to alarm_triples.csv.
        alarm_triples = [t for t in triples if t.predicate not in _PPE_PREDICATES]
        ppe_triples = [t for t in triples if t.predicate in _PPE_PREDICATES]

        if alarm_triples:
            self._append_alarm_triples(alarm_triples, alarm_names)
        if ppe_triples:
            self._append_ppe_triples(ppe_triples, alarm_names)
        # n_root = rule-derived RootCause triples (no is_root column any more).
        n_root = sum(1 for t in alarm_triples if t.predicate == ROOT_CAUSE)
        self._append_manifest_row(inc, len(alarm_triples), len(ppe_triples), n_root)
        self._written_incidents.add(incident_id)

    def _append_alarm_triples(self, triples: list[Triple],
                              alarm_names: dict[str, str]) -> None:
        triples = sorted(triples,
                         key=lambda t: (t.incident_id, t.subject,
                                        t.predicate, t.object))
        with open(self.alarm_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if not self._alarm_header_ready:
                w.writerow(_ALARM_COLS)
                self._alarm_header_ready = True
            for t in triples:
                w.writerow([t.subject, alarm_names.get(t.subject, ""),
                            t.predicate,
                            t.object, alarm_names.get(t.object, ""),
                            t.source, t.weight, t.incident_id])

    def _append_ppe_triples(self, triples: list[Triple],
                            alarm_names: dict[str, str]) -> None:
        triples = sorted(triples,
                         key=lambda t: (t.incident_id, t.subject,
                                        t.predicate, t.object))
        with open(self.ppe_path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if not self._ppe_header_ready:
                w.writerow(_PPE_COLS)
                self._ppe_header_ready = True
            for t in triples:
                # subject_name stays blank for PPE URIs (self-describing);
                # object_name is populated only when the object is an Alarm
                # URI — i.e. on PpeIndicates rows.
                w.writerow([t.subject, "",
                            t.predicate,
                            t.object, alarm_names.get(t.object, ""),
                            t.source, t.weight, t.incident_id])

    def _append_manifest_row(self, inc, n_triples: int,
                             n_ppe_triples: int = 0, n_root: int = 0) -> None:
        info = inc.info or {}
        row = {
            "incident_id":    inc.incident_id,
            "fault_type":     info.get("fault_type", ""),
            "fault_target":   info.get("fault_target", ""),
            "target_kind":    info.get("target_kind", ""),
            "suppressed":     info.get("suppressed", False),
            "topology_id":    info.get("topology_id", ""),
            "n_alarms":       len(inc.alarm_rows),
            "n_root":         n_root,
            "n_triples":      n_triples,
            "n_ppe_triples":  n_ppe_triples,
        }
        with open(self.manifest_path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=_MANIFEST_COLS)
            if not self._manifest_header_ready:
                w.writeheader()
                self._manifest_header_ready = True
            w.writerow(row)


def _touches_topology(delta: Delta) -> bool:
    for t in delta.adds:
        if t.source == "topology":
            return True
    for t in delta.removes:
        if t.source == "topology":
            return True
    return False


def _read_manifest_ids(path: str) -> set[int]:
    """Return the set of incident_ids already present in the manifest, so a
    restart doesn't re-append the same incidents."""
    if not os.path.exists(path):
        return set()
    ids: set[int] = set()
    try:
        with open(path, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                sid = row.get("incident_id")
                if sid:
                    try:
                        ids.add(int(sid))
                    except ValueError:
                        continue
    except OSError:
        log.exception("could not seed _written_incidents from %s", path)
    return ids
