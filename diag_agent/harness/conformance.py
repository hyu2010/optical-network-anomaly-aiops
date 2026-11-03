"""Documentation conformance (doc 03 §8): does the KB describe the test bed?

For each labelled raw incident, the documented mode for the simulator fault
is propagated at the true location and compared, with every window closed,
against what the substrate actually emitted.  Findings are reported for a
knowledge engineer; the KB is never written.
"""

from __future__ import annotations

from typing import Any

from ..environment.substrate.simulator_db import SimulatorDBSubstrate, list_incidents
from ..procedural_memory.reasoning.evaluation import Evaluator
from ..procedural_memory.reasoning.framer import build_frame
from .labels import LabelReader
from .scorer import expected_locations, true_mode


def check_incident(sem, db_path: str, incident_id: int, evaluator: Evaluator | None = None) -> dict[str, Any]:
    ev = evaluator or Evaluator(sem)
    truth = LabelReader(db_path, incident_id, component="scorer").truth()
    mode = true_mode(sem, truth["fault_type"])
    row: dict[str, Any] = {"incident": incident_id, "fault_type": truth["fault_type"], "args": truth["args"],
                           "mode": mode, "findings": []}
    if mode is None:
        row["findings"].append({"kind": "undocumented-fault", "detail": f"no mode documents {truth['fault_type']}"})
        return row
    locs = sorted(expected_locations(sem, mode, truth["args"]))
    if not locs:
        row["findings"].append({"kind": "unknown-target", "detail": f"no KB resource among {truth['args']}"})
        return row
    want = sem.mode(mode).get("applies_to")
    loc = next((l for l in locs if want != "OtuLink" or sem.kind_of(l) == "otu"), locs[0])
    row["location"] = loc
    sub = SimulatorDBSubstrate(db_path, incident_id, component="substrate")
    try:
        t_end = (sub.last_alarm_time() or 0.0) + 120.0
        alarms = sub.alarms_until(t_end)
        traces = sub.ppe_traces_until(t_end)
    finally:
        sub.close()
    if not alarms:
        row["findings"].append({"kind": "no-alarms", "detail": "the fault emitted no alarm"})
        return row
    frame = build_frame(sem, alarms, traces, t_frame=t_end)
    ppe_read = {}
    for tr in traces:
        ppe_read[tr["och"]] = tr
    ctx = ev.context(frame, alarms, ppe_read, t_end + 3600.0, [])
    res = ev.evaluate("H1", mode, loc, ctx)
    for s in sorted(res["states"].values(), key=lambda s: s["pid"]):
        if s["strength"] in ("Required", "Expected") and s["state"] in ("MISSING", "CONTRADICTED"):
            row["findings"].append({"kind": f"predicted-{s['state'].lower()}", "strength": s["strength"],
                                    "condition": s["condition"], "resource": s["resource"], "layer": s["layer"],
                                    "detail": s.get("reason", "")})
    for it in frame["items"]:
        if it["relevant"] and not res["explained"].get(it["id"]):
            row["findings"].append({"kind": "observed-unexplained", "condition": it["condition"],
                                    "resource": it["resource"], "layer": it["layer"],
                                    "detail": ", ".join(it["root_names"][:4])})
    row["n_items"] = len(frame["items"])
    row["ok"] = not row["findings"]
    return row


def run(sem, db_paths: list[str]) -> list[dict[str, Any]]:
    ev = Evaluator(sem)
    out = []
    for db in db_paths:
        for inc in list_incidents(db, suppressed=False):
            r = check_incident(sem, db, int(inc["id"]), ev)
            r["db"] = db
            out.append(r)
    return out
