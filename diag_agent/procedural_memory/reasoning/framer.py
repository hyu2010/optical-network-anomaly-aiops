"""Case framing (doc 04 §2).

The frame is a pure function of the observations seen so far, so re-framing
after new evidence keeps the S#/V#/E# numbering stable (observations only
arrive in time order).

  items     S#  (condition, resource) groups; same-location suppression
                descendants fold into their root's item
  ppe       P#  located PPE detector anomalies (when a trace has been read)
  services  V#  client services down / degraded / at risk
  shared    E#  resources on the path of every hurt service
  quiet     instruments in scope that reported nothing
  onset     intersection of [t_obs - cadence, t_obs] over the alarms
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

TRANSPORT_EXCLUDE = {"OtherCondition"}
SERVICE_DOWN_CONDS = {"ServerSignalFail", "LossOfSignal", "TraceMismatch"}
ONSET_SLACK_S = 0.2


def resource_of(alarm: dict) -> str:
    return alarm.get("port") or alarm.get("board") or alarm.get("ne") or ""


def cadence_s(sem, layer_key: str) -> float:
    return sem.layer(layer_key)["cadence_ms"] / 1000.0


def onset_interval(sem, alarms: list[dict]) -> tuple[float, float]:
    if not alarms:
        return (0.0, 0.0)
    lo = max(a["t_obs"] - cadence_s(sem, a["layer"]) for a in alarms) - ONSET_SLACK_S
    hi = min(a["t_obs"] for a in alarms)
    if lo > hi:
        first = min(alarms, key=lambda a: a["t_obs"])
        lo = first["t_obs"] - cadence_s(sem, first["layer"])
    return (round(lo, 3), round(hi, 3))


def port_role(sem, res: str) -> str:
    kind = sem.kind_of(res)
    if kind == "port":
        if sem.subjects_with("rxPort", res):
            for s in sem.subjects_with("rxPort", res):
                k = sem.kind_of(s)
                if k == "span":
                    return "rx_of_span"
                if k == "otu":
                    return "rx_of_otu"
        if sem.subjects_with("txPort", res):
            for s in sem.subjects_with("txPort", res):
                k = sem.kind_of(s)
                if k == "span":
                    return "tx_of_span"
                if k == "otu":
                    return "tx_of_otu"
        if sem.subjects_with("clientPort", res):
            return "client_drop"
        if sem.subjects_with("sourceClientPort", res):
            return "client_add"
        return "port"
    if kind == "board":
        cls = sorted(c for c in sem.classes_of(res) if c.endswith("Board") and c != "Board")
        return f"board:{cls[0] if cls else 'Board'}"
    return kind


def _short(cond: str) -> str:
    return {"LossOfSignal": "LOS", "ServerSignalFail": "SSF", "BackwardDefect": "BDI", "LowInputPower": "LowPwr",
            "PreFecDegraded": "PreFEC", "SignalDegraded": "DEG", "EquipmentDown": "EqDown",
            "TraceMismatch": "TIM", "CoolingFailure": "Fan", "OverTemperature": "Temp",
            "InputPowerAbnormal": "PwrAbn"}.get(cond, cond)


def build_frame(sem, alarms: list[dict], ppe_traces: list[dict], *, t_frame: float, scope_hops: int = 2,
                hold_s: float = 32.0) -> dict[str, Any]:
    alarms = sorted(alarms, key=lambda a: (a["t_obs"], a["row"]))
    onset = onset_interval(sem, alarms)

    # ---------------------------------------------------------------- folding
    by_res: dict[str, list[dict]] = defaultdict(list)
    for a in alarms:
        by_res[resource_of(a)].append(a)
    groups: dict[tuple[str, str], dict] = {}
    for res, lst in by_res.items():
        names = {a["name"] for a in lst}
        roots = [n for n in names if not any(n in sem.descendants(m) for m in names if m != n)]
        if not roots:                       # suppression cycle: earliest name is the root
            roots = [lst[0]["name"]]
        roots_sorted = sorted(roots, key=lambda n: min(a["t_obs"] for a in lst if a["name"] == n))
        root_of: dict[str, str] = {}
        for n in names:
            if n in roots:
                root_of[n] = n
            else:
                root_of[n] = next((r for r in roots_sorted if n in sem.descendants(r)), roots_sorted[0])
        for a in lst:
            root = root_of[a["name"]]
            at = sem.alarm_type(root)
            key = (at["condition"], res)
            g = groups.setdefault(key, {"condition": at["condition"], "resource": res, "alarms": [], "root_names": [],
                                        "names": [], "layers": [], "containers": [], "folded": []})
            g["alarms"].append(a)
            if a["name"] == root:
                if root not in g["root_names"]:
                    g["root_names"].append(root)
            else:
                g["folded"].append(a["event_id"])
            if a["name"] not in g["names"]:
                g["names"].append(a["name"])
            if a["container"] and a["container"] not in g["containers"]:
                g["containers"].append(a["container"])

    items = []
    for g in sorted(groups.values(), key=lambda g: (min(a["t_obs"] for a in g["alarms"]),
                                                    min(a["row"] for a in g["alarms"]))):
        root_alarms = [a for a in g["alarms"] if a["name"] in g["root_names"]]
        lay_ids = sorted({sem.layer(a["layer"])["id"] for a in root_alarms},
                         key=lambda l: sem.layer(l)["rank"])
        rk = sem.kind_of(g["resource"])
        items.append({
            "id": f"S{len(items) + 1}", "condition": g["condition"], "resource": g["resource"], "kind": rk,
            "role": port_role(sem, g["resource"]), "layer": lay_ids[0] if lay_ids else "OTHER",
            "layers": lay_ids, "t_first": round(min(a["t_obs"] for a in g["alarms"]), 3),
            "root_names": g["root_names"], "names": g["names"],
            "events": [a["event_id"] for a in g["alarms"]], "folded": g["folded"],
            "containers": g["containers"], "ne": g["alarms"][0]["ne"],
            "relevant": g["condition"] not in TRANSPORT_EXCLUDE,
            "possible_causes": sorted({m for n in g["root_names"] for m in sem.alarm_type(n)["possible_causes"]}),
            "keywords": sorted({k for n in g["root_names"] for k in sem.alarm_type(n)["keywords"]}),
        })

    # ---------------------------------------------------------------- PPE symptoms
    ppe = []
    latest: dict[str, dict] = {}
    for tr in sorted(ppe_traces, key=lambda t: t["t_obs"]):
        latest[tr["och"]] = tr
    for och, tr in sorted(latest.items()):
        anoms = tr.get("anomalies") or []
        if not anoms:
            ppe.append({"id": f"P{len(ppe) + 1}", "och": och, "kind": "null" if tr.get("degraded") else "clean",
                        "event_id": tr["event_id"], "t_obs": tr["t_obs"], "span": "", "offset_km": None,
                        "magnitude_db": None, "z_km": None, "confidence": None, "trace_id": tr["trace_id"]})
            continue
        for an in anoms:
            ppe.append({"id": f"P{len(ppe) + 1}", "och": och, "kind": an["kind"], "event_id": tr["event_id"],
                        "t_obs": tr["t_obs"], "span": an.get("span_id") or "", "offset_km": an.get("offset_in_span_km"),
                        "magnitude_db": an.get("magnitude_db"), "z_km": an.get("z_km"),
                        "confidence": an.get("confidence"), "trace_id": tr["trace_id"]})

    # ---------------------------------------------------------------- services
    services: list[dict] = []
    seen_svc: dict[str, dict] = {}

    def add_service(svc: str, odu: str, state: str, via: str) -> None:
        rank = {"down": 0, "degraded": 1, "at-risk": 2}
        if svc in seen_svc:
            cur = seen_svc[svc]
            if rank[state] < rank[cur["state"]]:
                cur.update(state=state, via=via)
            return
        v = {"id": "", "service": svc, "odu": odu, "state": state, "via": via}
        seen_svc[svc] = v
        services.append(v)

    hard_resources: set[str] = set()
    for it in items:
        if it["kind"] == "port" and it["condition"] in ("ServerSignalFail", "TraceMismatch") and \
                ("ODU" in it["layers"] or it["role"] == "client_drop"):
            for odu in sem.subjects_with("clientPort", it["resource"]):
                for svc in sem.values(odu, "carries"):
                    add_service(svc, odu, "down", it["id"])
        if it["condition"] in ("SignalDegraded", "PreFecDegraded") and it["role"] == "rx_of_otu":
            for otu in sem.subjects_with("rxPort", it["resource"]):
                for odu in sem.subjects_with("traverses", otu):
                    if sem.kind_of(odu) == "odu":
                        for svc in sem.values(odu, "carries"):
                            add_service(svc, odu, "degraded", it["id"])
        if it["condition"] in ("LossOfSignal", "EquipmentDown", "ServerSignalFail"):
            hard_resources.add(it["resource"])
            for c in it["containers"]:
                hard_resources.add(c)
            for up in sem.subjects_with("rxPort", it["resource"]):
                hard_resources.add(up)
    for svc in sem.resources_of_kind("service"):
        deps = sem.depends_on(svc)
        hit = sorted(r for r in hard_resources if r in deps and sem.kind_of(r) in ("span", "otu"))
        if hit:
            odus = [o for o in sem.subjects_with("carries", svc)]
            add_service(svc, odus[0] if odus else "", "at-risk", hit[0])
    services.sort(key=lambda v: ({"down": 0, "degraded": 1, "at-risk": 2}[v["state"]], v["service"]))
    for i, v in enumerate(services):
        v["id"] = f"V{i + 1}"

    # ---------------------------------------------------------------- shared elements
    hurt = [v for v in services if v["state"] in ("down", "at-risk", "degraded")]
    shared: list[dict] = []
    if len(hurt) >= 2:
        common = set.intersection(*(sem.depends_on(v["service"]) for v in hurt))
        order = {"span": 0, "section": 1, "otu": 2, "board": 3, "ne": 4}
        cand = sorted((r for r in common if sem.kind_of(r) in order),
                      key=lambda r: (order[sem.kind_of(r)], r))
        for r in cand[:8]:
            shared.append({"id": f"E{len(shared) + 1}", "resource": r, "kind": sem.kind_of(r)})

    # ---------------------------------------------------------------- scope and quiet instruments
    scope_nes = {it["ne"] for it in items if it["ne"]}
    frontier = set(scope_nes)
    for _ in range(max(0, scope_hops - 1)):
        nxt = set()
        for span in sem.resources_of_kind("span"):
            s, d = sem.value(span, "sourceNE"), sem.value(span, "destNE")
            if s in frontier or d in frontier:
                nxt.update({s, d})
        frontier |= nxt
    scope_nes = {n for n in frontier if n}
    reporting = {it["resource"] for it in items}
    quiet = []
    for ne in sorted(scope_nes):
        for b in sem.values(ne, "hostsBoard"):
            for p in sem.values(b, "hasPort"):
                role = port_role(sem, p)
                if role in ("rx_of_span", "rx_of_otu", "client_drop") and p not in reporting:
                    lay = {"rx_of_span": "OTS", "rx_of_otu": "OCH", "client_drop": "ODU"}[role]
                    quiet.append({"resource": p, "role": role, "layer": lay,
                                  "window_closes": round(onset[1] + cadence_s(sem, lay) + 2, 1)})
        if ne not in {it["ne"] for it in items if it["kind"] in ("board", "ne")}:
            quiet.append({"resource": ne, "role": "equipment", "layer": "EQUIPMENT",
                          "window_closes": round(onset[1] + cadence_s(sem, "EQUIPMENT") + 2, 1)})

    # ---------------------------------------------------------------- anchor and name
    rel = [it for it in items if it["relevant"]]
    capable = [it for it in rel if it["possible_causes"]] or rel or items
    anchor = min(capable, key=lambda it: (sem.layer(it["layer"])["rank"], it["t_first"])) if capable else None
    name = ""
    if anchor:
        where = anchor["containers"][0] if anchor["containers"] else anchor["resource"]
        name = f"{_cond_text(anchor['condition'])} on {where}"

    role_form = {
        "items": sorted({f"{_short(it['condition'])}[{it['layer']}]@{it['role']}" for it in rel}),
        "services": {s: sum(1 for v in services if v["state"] == s) for s in ("down", "degraded", "at-risk")},
        "shared_kinds": sorted({e["kind"] for e in shared}),
        "quiet_roles": sorted({q["role"] for q in quiet}),
        "ppe_kinds": sorted({p["kind"] for p in ppe}),
        "earliest_layer": anchor["layer"] if anchor else "",
        "anchor_condition": anchor["condition"] if anchor else "",
    }
    return {
        "t_frame": round(float(t_frame), 3), "onset": list(onset),
        "t_first": round(alarms[0]["t_obs"], 3) if alarms else None,
        "hold_s": hold_s, "n_alarms": len(alarms), "items": items, "ppe": ppe, "services": services,
        "shared": shared, "quiet": quiet, "scope_nes": sorted(scope_nes),
        "anchor": anchor["id"] if anchor else None, "name": name, "role_form": role_form,
    }


def _cond_text(cond: str) -> str:
    return {"LossOfSignal": "Loss of signal", "LowInputPower": "Low input power", "PreFecDegraded": "Pre-FEC degradation",
            "SignalDegraded": "Signal degraded", "ServerSignalFail": "Server signal fail",
            "EquipmentDown": "Equipment down", "TraceMismatch": "Trace mismatch", "CoolingFailure": "Fan failure",
            "OverTemperature": "Over-temperature", "InputPowerAbnormal": "Input power abnormal",
            "RegeneratorSectionErrors": "Regenerator-section errors", "MultiplexSectionErrors": "Multiplex-section errors",
            "BackwardDefect": "Backward defect"}.get(cond, cond)


def item_by_id(frame: dict, ref: str) -> dict | None:
    for coll in ("items", "services", "shared", "ppe"):
        for x in frame.get(coll, []):
            if x["id"] == ref:
                return x
    return None


def anchor_resource(sem, frame: dict, ref: str) -> str | None:
    """Resource a location term is anchored at: S# -> reporting resource; V# -> drop client port; E# -> element."""
    x = item_by_id(frame, ref)
    if x is None:
        return ref if sem.exists(ref) else None
    if ref.startswith("S"):
        return x["resource"]
    if ref.startswith("V"):
        ports = sem.values(x["odu"], "clientPort") if x.get("odu") else []
        return ports[0] if ports else x["service"]
    if ref.startswith("E"):
        return x["resource"]
    if ref.startswith("P"):
        return x["och"]
    return None
