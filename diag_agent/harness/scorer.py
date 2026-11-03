"""Score a finished diagnosis against the fixture labels (doc 07 §5).

Only the harness reads labels; the agent never imports this module.
"""

from __future__ import annotations

from typing import Any

from .labels import LabelReader


def expected_locations(sem, mode_id: str, args: list[str]) -> set[str]:
    """Resources that count as the true location of a simulator fault target."""
    res = [a for a in args if sem.exists(a)]
    if not res:
        return set()
    target = res[-1]
    out = {target}
    want = sem.mode(mode_id).get("applies_to") if mode_id else None
    if want == "OtuLink" and sem.kind_of(target) == "span":
        out |= set(sem.subjects_with("supportedBy", target))
    return out


def true_mode(sem, fault_type: str) -> str | None:
    return next((m for m, d in sorted(sem.modes().items()) if d.get("simulator_fault") == fault_type), None)


def score(sem, state: dict, db_path: str, raw_incident_id: int) -> dict[str, Any]:
    labels = LabelReader(db_path, raw_incident_id, component="scorer")
    truth = labels.truth()
    mode = true_mode(sem, truth["fault_type"])
    locs = expected_locations(sem, mode, truth["args"])
    led = state.get("ledger") or {}
    info = state.get("decision_info") or {}
    hyps = state.get("hypotheses") or {}
    committed = info.get("committed")
    leader = led.get("leader")
    lh = hyps.get(leader, {}) if leader else {}
    out: dict[str, Any] = {
        "raw_incident_id": raw_incident_id, "truth": {"fault_type": truth["fault_type"], "args": truth["args"],
                                                      "mode": mode, "locations": sorted(locs)},
        "decision": state.get("decision"), "leader": {"mode": lh.get("mode"), "location": lh.get("location"),
                                                      "p": round(led.get("post", {}).get(leader, 0.0), 6)},
        "checks": [c for c in (state.get("budget") or {}).get("checks_done", [])],
        "effort_h": (state.get("budget") or {}).get("effort_h", 0.0),
        "llm_calls": (state.get("budget") or {}).get("llm_calls", 0),
    }
    out["committed"] = bool(committed)
    out["correct"] = bool(committed and committed["mode"] == mode and committed["location"] in locs)
    out["leader_correct"] = lh.get("mode") == mode and lh.get("location") in locs
    post = led.get("post", {})
    ranked = led.get("ranked", [])
    acc, in_set = 0.0, False
    for h in ranked:
        if hyps[h]["mode"] == mode and hyps[h]["location"] in locs:
            in_set = True
            break
        acc += post.get(h, 0.0)
        if acc >= 0.9:
            break
    out["truth_in_90_set"] = in_set
    out["p_truth"] = round(sum(post.get(h, 0.0) for h, x in hyps.items()
                               if x["mode"] == mode and x["location"] in locs), 6)
    spans = [a for a in truth["args"] if sem.exists(a) and sem.kind_of(a) == "span"]
    if committed and (committed.get("fit") or {}).get("position_km") is not None and spans:
        pos = labels.root_ppe_position(spans[-1])
        if pos is not None:
            out["position_error_km"] = round(abs(committed["fit"]["position_km"] - pos), 3)
    out["label_reads"] = labels.access_log()
    return out
