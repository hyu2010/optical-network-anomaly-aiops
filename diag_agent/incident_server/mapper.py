"""ietf-incident payload mapper (doc 06 §5).

Input is an *incident view* assembled by the decision procedure (plain dict):
    incident_no, name, type, qualifier, raw_incident_id, created_at, status, ack_status,
    ticket_no, times{raise, occur, ack, clear, last_updated} (ISO), frame, decision,
    leader{mode, location, p, category, cause_name, cause_name_ext, fit{...}, events[]},
    set[...], runner_up{...}, nota, candidates[...], alarms[...], ppe_events[...],
    detail, resolve_advice, tasks[...], kb_release, llm_profile, tree_uri, onset_iso[2]
Output is RFC 7951 JSON (uint64 and decimal64 as strings, identities qualified).
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

INC = "ietf-incident"
EXT = "diag-agent-incident-ext"
DAAT = "diag-agent-alarm-types"
OTN_KINDS = {"otu", "odu", "service"}


def dec(x: float | None, digits: int) -> str | None:
    if x is None:
        return None
    q = Decimal(1).scaleb(-digits)
    return str(Decimal(repr(float(x))).quantize(q, rounding=ROUND_HALF_EVEN))


def network_ref(kind: str, resource: str) -> str:
    if kind in OTN_KINDS:
        return "otn-topo"
    if kind == "port" and (".client_" in resource):
        return "otn-topo"
    return "optical-topo"


def priority(view: dict) -> str:
    frame = view.get("frame") or {}
    svcs = frame.get("services", [])
    items = frame.get("items", [])
    if any(v["state"] == "down" for v in svcs):
        p = "critical"
    elif any(it["condition"] == "LossOfSignal" for it in items) and any(v["state"] == "at-risk" for v in svcs):
        p = "critical"
    elif any(v["state"] == "degraded" for v in svcs) or any(it["condition"] == "EquipmentDown" for it in items):
        p = "high"
    elif any(it["condition"] in ("CoolingFailure", "OverTemperature") for it in items) and len(items) <= 3:
        p = "low"
    else:
        p = "medium"
    order = ["low", "medium", "high", "critical"]
    floor = view.get("priority_floor")
    if floor and order.index(floor) > order.index(p) and any(v["state"] == "down" for v in svcs):
        p = floor
    return p


def category(view: dict) -> str:
    lead = view.get("leader")
    if lead and view.get("decision") in ("committed",):
        return lead.get("category") or f"{INC}:network"
    frame = view.get("frame") or {}
    anchor = next((it for it in frame.get("items", []) if it["id"] == frame.get("anchor")), None)
    lay = anchor["layer"] if anchor else "OTS"
    if lay == "EQUIPMENT":
        return f"{INC}:device-hardware"
    if lay == "ENVIRONMENT":
        return f"{INC}:power-environment"
    return f"{INC}:network"


def service_instance(view: dict) -> list[str]:
    frame = view.get("frame") or {}
    return sorted({v["service"] for v in frame.get("services", [])})


def _events(view: dict, mode: str) -> list[dict]:
    out = []
    for a in view.get("alarms", []):
        out.append({"type": f"{INC}:alarm", "event-id": a["event_id"],
                    "alarm": {"resource": a["resource"], "alarm-type-id": f"{DAAT}:{a['name']}"}})
    ptype = f"{INC}:metric" if mode == "strict" else f"{EXT}:ppe-anomaly"
    for p in view.get("ppe_events", []):
        out.append({"type": ptype, "event-id": p["event_id"],
                    "metric": {k: v for k, v in (("resource", p["och"]), ("metric-name", p["metric_name"]),
                                                 ("threshold-value", dec(p.get("threshold"), 2)),
                                                 ("observed-value", dec(p.get("observed"), 2))) if v is not None}})
    return out


def _cause_entries(view: dict, mode: str) -> list[dict]:
    members = []
    if view.get("decision") == "committed" and view.get("leader"):
        members = [view["leader"]]
    elif view.get("set"):
        members = view["set"]
    out: dict[tuple, dict] = {}
    for m in members:
        cname = m["cause_name"] if mode == "strict" else (m.get("cause_name_ext") or m["cause_name"])
        node = m["node_ref"]
        key = (node, cname)
        detail = m.get("detail") or f"{m['mode']} at {m['location']}; P = {dec(m['p'], 5)}."
        res = {"name": m["location"], "cause-name": cname}
        if m.get("resource_detail"):
            res["detail"] = m["resource_detail"]
        if key in out:
            if all(r["name"] != m["location"] for r in out[key]["resource"]):
                out[key]["resource"].append(res)
            continue
        out[key] = {"node-ref": node, "network-ref": m.get("network_ref", "optical-topo"), "cause-name": cname,
                    "detail": detail, "resource": [res]}
    return list(out.values())


def _sources(view: dict) -> list[dict]:
    out: dict[str, dict] = {}
    for s in view.get("sources", []):
        e = out.setdefault(s["node_ref"], {"node-ref": s["node_ref"], "network-ref": s["network_ref"], "resource": []})
        if all(r["name"] != s["resource"] for r in e["resource"]):
            e["resource"].append({"name": s["resource"]})
    return list(out.values())


def incident_info(view: dict, mode: str = "extended") -> dict[str, Any]:
    """The incident-info grouping (shared by the data tree and the notification)."""
    events = _events(view, mode)
    ev_keys = {(e["type"], e["event-id"]) for e in events}
    info: dict[str, Any] = {
        "incident-no": str(view["incident_no"]),
        "name": view["name"], "type": view.get("type", f"{INC}:problem"),
        "incident-qualifier": view.get("qualifier", ""),
        "service-instance": service_instance(view),
        "domain": f"{INC}:transport",
        "priority": priority(view),
        "status": view.get("status", "raised"),
        "ack-status": view.get("ack_status", "unacknowledged"),
        "category": category(view),
    }
    if view.get("detail"):
        info["detail"] = view["detail"]
    if view.get("resolve_advice") and view.get("decision") == "committed":
        info["resolve-advice"] = view["resolve_advice"]
    srcs = _sources(view) if view.get("decision") in ("committed", "set-reported") else []
    if srcs:
        info["sources"] = {"source": srcs}
    causes = _cause_entries(view, mode)
    if causes:
        info["probable-causes"] = {"probable-cause": causes}
        pe = []
        for t, eid in view.get("probable_events", []):
            typ = f"{INC}:alarm" if t == "alarm" else (f"{INC}:metric" if mode == "strict" else f"{EXT}:ppe-anomaly")
            if (typ, eid) in ev_keys and {"type": typ, "event-id": eid} not in pe:
                pe.append({"type": typ, "event-id": eid})
        if pe:
            info["probable-events"] = {"probable-event": pe}
    if events:
        info["events"] = {"event": events}
    return info


def _summary(view: dict) -> dict:
    s = {"decision": view.get("decision", "in-progress")}
    if view.get("leader") and view.get("decision") != "in-progress":
        s["leader-probability"] = dec(view["leader"]["p"], 6)
    if view.get("nota") is not None and view.get("decision") != "in-progress":
        s["nota-probability"] = dec(view["nota"], 6)
    if view.get("tree_uri"):
        s["tree-uri"] = view["tree_uri"]
    return s


def notification(view: dict, mode: str = "extended") -> dict:
    body = incident_info(view, mode)
    body["time"] = view["times"]["occur"]
    if mode == "extended":
        if view.get("ticket_no"):
            body[f"{EXT}:ticket-no"] = view["ticket_no"]
        body[f"{EXT}:diagnosis-summary"] = _summary(view)
    return {f"{INC}:incident-notification": body}


def task_record(task: dict, mode: str = "extended") -> dict:
    rec = {"task-id": task["task_id"], "incident-no": [str(task["incident_no"])]}
    if task.get("ticket_no"):
        rec["ticket-no"] = task["ticket_no"]
    rec["start-time"] = task["start_time"]
    if task.get("end_time"):
        rec["end-time"] = task["end_time"]
    rec["task-state"] = task["state"]
    if task.get("result"):
        rec["diagnosis-result"] = task["result"]
    if task.get("description"):
        rec["diagnosis-result-description"] = task["description"]
    if task.get("view"):
        causes = _cause_entries(task["view"], mode)
        if causes:
            rec["probable-causes"] = {"probable-cause": causes}
        ev = incident_info(task["view"], mode).get("probable-events")
        if ev:
            rec["probable-events"] = ev
    if task.get("repair_advices"):
        rec["repair-advices"] = task["repair_advices"]
    return rec


def task_notification(task: dict) -> dict:
    return {f"{EXT}:diagnosis-task-notification": task_record(task, "extended")}


def data_entry(view: dict, mode: str = "extended") -> dict:
    e = incident_info(view, mode)
    t = view["times"]
    for k, leaf in (("raise", "raise-time"), ("occur", "occur-time"), ("clear", "clear-time"),
                    ("ack", "ack-time"), ("last_updated", "last-updated")):
        if t.get(k):
            e[leaf] = t[k]
    if mode == "extended":
        if view.get("ticket_no"):
            e[f"{EXT}:ticket-no"] = view["ticket_no"]
        di: dict[str, Any] = {"decision": view.get("decision", "in-progress")}
        if view.get("leader") and view.get("decision") != "in-progress":
            di["leader-probability"] = dec(view["leader"]["p"], 6)
        if view.get("nota") is not None and view.get("decision") != "in-progress":
            di["nota-probability"] = dec(view["nota"], 6)
        if view.get("tree_uri"):
            di["tree-uri"] = view["tree_uri"]
        if view.get("onset_iso"):
            di["onset-estimate-start"], di["onset-estimate-end"] = view["onset_iso"]
        di["kb-release"] = view.get("kb_release", "")
        di["llm-profile"] = view.get("llm_profile", "none")
        if view.get("candidates"):
            di["candidate"] = [{"rank": c["rank"], "fault-mode": c["mode"], "location": c["location"],
                                "probability": dec(c["p"], 6)} for c in view["candidates"][:5]]
        e[f"{EXT}:diagnosis-info"] = di
        if view.get("tasks"):
            e[f"{EXT}:incident-diagnosis-tasks"] = {"incident-diagnosis-task": [task_record(t, mode)
                                                                                for t in view["tasks"]]}
    return e


def incidents_tree(views: list[dict], mode: str = "extended") -> dict:
    return {f"{INC}:incidents": {"incident": [data_entry(v, mode) for v in views]}}


def rpc_error(rpc: str, incident_no: str, reason: str, message: str, description: str, path: bool = True) -> dict:
    err: dict[str, Any] = {"error-type": "application", "error-tag": "operation-failed"}
    if path:
        err["error-path"] = f"/{INC}:{rpc}/input/incident-no"
    err["error-message"] = message
    err["error-info"] = {f"{INC}:{rpc}-error-info": {"incident-no": str(incident_no), "reason": f"{INC}:{reason}",
                                                     "description": description}}
    return {"ietf-restconf:errors": {"error": [err]}}


def restconf_wrap(payload: dict, event_time: str) -> dict:
    return {"ietf-restconf:notification": {"eventTime": event_time, **payload}}
