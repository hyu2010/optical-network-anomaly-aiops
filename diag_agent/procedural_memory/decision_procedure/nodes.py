"""Graph nodes of the decision procedure (doc 05 §2).

Every node is a thin adapter: it reads working memory, calls a reasoning
procedure, and returns the slots it owns.  Long-term memories and the
environment are reached through `Deps` (built by `diag_agent.app`).
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable

from ...environment.substrate.clock import iso_at, parse_iso, seconds_between
from ...working_memory.tree import TreeBuilder, root_id
from ..reasoning import ledger as L
from ..reasoning import terms as T
from ..reasoning.advisor import Advisor, mutual_information
from ..reasoning.evaluation import Evaluator
from ..reasoning.framer import build_frame, resource_of
from ..reasoning.lines import analogy, correlation, llm_proposer, pooling, walk
from ..reasoning.lines.analogy import conclusion_relations
from ..reasoning.narrator import narrate as run_narrator
from ..reasoning.narrator import orchestrate, position_text, raise_detail
from ..reasoning.terms import field_answer_class
from ..reasoning.windows import answer_key, classify_trace

log = logging.getLogger("diag_agent.graph")


@dataclass
class Deps:
    sem: Any
    substrate: Any
    episodic: Any
    llm: Any
    dispatcher: Any
    incidents: Any
    cfg: dict
    kb_release: str = ""
    llm_profile: str = "none"
    scenario: dict = field(default_factory=dict)
    ppe_access: str = "on_demand"
    live_field: bool = False
    publish_incident_graph: bool = True
    writer: Callable[[dict], None] | None = None
    evaluator: Evaluator | None = None

    def __post_init__(self):
        if self.evaluator is None:
            chk = self.sem.checks().get("CHK_PPE_PROFILE", {})
            self.evaluator = Evaluator(self.sem, tol_km=chk.get("position_tol_km", 0.4),
                                       clean_slope_max=chk.get("clean_slope_max", 0.08))


# ====================================================================== helpers
def gather(state: dict) -> dict[str, Any]:
    alarms: dict[str, dict] = {}
    ppe_read: dict[str, dict | None] = {}
    audits, field_obs, done = [], [], []
    for o in state.get("obs_log", []):
        k = o["kind"]
        if k == "alarm":
            cur = alarms.get(o["event_id"])
            if cur is None or o["t_obs"] < cur["t_obs"]:
                alarms[o["event_id"]] = o
        elif k == "ppe":
            ppe_read[o["och"]] = o.get("trace")
        elif k == "audit":
            audits.append(o)
        elif k == "check":
            done.append(f"{o['check_id']}({o['target']})")
            if o.get("field"):
                field_obs.append(o)
    al = sorted(alarms.values(), key=lambda a: (a["t_obs"], a["row"]))
    return {"alarms": al, "ppe_read": ppe_read, "audits": audits, "field_obs": field_obs, "done": done}


def _emit(deps: Deps, state: dict, nodes: list[dict]) -> None:
    if nodes and deps.incidents is not None and state.get("incident_no"):
        deps.incidents.tree_delta(state["incident_no"], nodes)
    if deps.writer and nodes:
        for n in nodes:
            deps.writer({"type": "tree.node", "node": n})


def _tip(tree: list[dict]) -> str | None:
    tip = root_id(tree)
    for n in tree:
        if n["kind"] == "answer" and n.get("taken"):
            tip = n["id"]
    return tip


def _fmt_p(p: float) -> str:
    return f"{p:.5f}" if p >= 1e-5 else f"{p:.1e}"


def _hyp_label(h: dict) -> str:
    return f"{h['id']} {h['mode']}@{h['location']}"


def _ctx(deps: Deps, state: dict, frame: dict | None = None) -> dict:
    g = gather(state)
    return deps.evaluator.context(frame or state["frame"], g["alarms"], g["ppe_read"], state["clock"], g["audits"])


def _iso(state: dict, t: float) -> str:
    return iso_at(state["created_at"], t)


def _script_time(state: dict, at: str) -> float:
    at = str(at).strip()
    if at.startswith("+"):
        unit = {"s": 1.0, "m": 60.0, "h": 3600.0}.get(at[-1], 1.0)
        return float(at[1:].rstrip("smh")) * unit
    base = parse_iso(state["created_at"])
    if "T" not in at:
        at = base.strftime("%Y-%m-%dT") + at
    return seconds_between(state["created_at"], at)


# ====================================================================== client script (replay OSS)
def run_client_script(deps: Deps, state: dict, up_to: float) -> tuple[dict, list[dict]]:
    closure = dict(state.get("closure") or {})
    done = set(closure.get("script_done", []))
    script = (deps.scenario or {}).get("client_script") or []
    log_entries = []
    no = state.get("incident_no")
    if not no or deps.incidents is None:
        return closure, log_entries
    for i, ev in enumerate(script):
        if i in done:
            continue
        t = _script_time(state, ev["at"])
        if t > up_to + 1e-9:
            continue
        rpc, inp = ev["rpc"], dict(ev.get("input") or {})
        t_iso = _iso(state, t)
        entry = {"t": round(t, 3), "rpc": rpc, "input": inp}
        try:
            nos = [str(x) for x in inp.get("incident-no", [no])]
            ticket = inp.get("ticket-no")
            if rpc == "incident-diagnose":
                entry["output"] = {"task-id": deps.incidents.diagnose(nos, ticket, t_iso)}
            elif rpc == "incident-acknowledge":
                deps.incidents.acknowledge(nos, ticket, t_iso)
                entry["output"] = "ok"
            elif rpc == "incident-resolve":
                deps.incidents.resolve(nos, ticket, t_iso)
                entry["output"] = "accepted"
                closure["resolve_requested"] = t
        except Exception as e:  # noqa: BLE001 - RPC errors are part of the replay
            entry["error"] = getattr(e, "payload", str(e))
        done.add(i)
        log_entries.append(entry)
    closure["script_done"] = sorted(done)
    closure["rpc_log"] = list(closure.get("rpc_log", [])) + log_entries
    return closure, log_entries


# ====================================================================== incident view
def _node_ref(sem, loc: str) -> tuple[str, str]:
    k = sem.kind_of(loc)
    if k in ("span", "otu", "och", "odu"):
        return sem.value(loc, "sourceNE") or "", ("otn-topo" if k in ("otu", "odu") else "optical-topo")
    if k == "section":
        spans = sem.subjects_with("partOfSection", loc)
        return (sem.value(spans[0], "sourceNE") if spans else "") or "", "optical-topo"
    ne = sem.hosting_ne(loc) or loc
    return ne, ("otn-topo" if (k == "port" and ".client_" in loc) else "optical-topo")


def _sources(sem, loc: str) -> list[dict]:
    k = sem.kind_of(loc)
    out = []
    spans = [loc] if k in ("span", "otu") else (sem.subjects_with("partOfSection", loc) if k == "section" else [])
    for s in spans:
        net = "otn-topo" if k == "otu" else "optical-topo"
        tx, rx = sem.value(s, "txPort"), sem.value(s, "rxPort")
        if tx:
            out.append({"node_ref": sem.value(s, "sourceNE"), "network_ref": net, "resource": tx})
        if rx:
            out.append({"node_ref": sem.value(s, "destNE"), "network_ref": net, "resource": rx})
    if not spans:
        ne, net = _node_ref(sem, loc)
        out.append({"node_ref": ne, "network_ref": net, "resource": loc})
    return out


def leader_fit(sem, deps: Deps, state: dict, hid: str) -> dict | None:
    win = (state.get("windows") or {}).get(hid, {})
    for s in win.get("states", {}).values():
        if s["layer"] == "PPE" and s["state"] == "MATCHED" and s.get("observed", {}).get("class") in ("step", "slope"):
            ob = s["observed"]
            span = ob.get("span")
            if ob.get("offset_km") is None:
                continue
            return {"position_km": float(ob["offset_km"]), "tol_km": deps.sem.checks()["CHK_PPE_PROFILE"]["position_tol_km"],
                    "from_ne": sem.value(span, "sourceNE") or "", "span": span,
                    "span_length_km": float(sem.value(span, "lengthKm") or 0), "och": s["resource"],
                    "z_km": ob.get("z_km"), "magnitude_db": ob.get("magnitude_db"), "kind": ob["class"]}
    return None


def _root_symptom(state: dict, hid: str) -> tuple[str, str]:
    win = (state.get("windows") or {}).get(hid, {})
    g = gather(state)
    by_id = {a["event_id"]: a for a in g["alarms"]}
    best = None
    for s in win.get("states", {}).values():
        if s["state"] == "MATCHED" and s["strength"] == "Required" and s["layer"] != "PPE" and s["matched_by"]:
            if s["condition"] in ("ServiceDown", "ServiceDegraded"):
                continue
            a = min((by_id[e] for e in s["matched_by"] if e in by_id), key=lambda a: a["t_obs"], default=None)
            if a and (best is None or a["t_obs"] < best["t_obs"]):
                best = a
    if best is None:
        return "", ""
    frame = state.get("frame") or {}
    item = next((it for it in frame.get("items", []) if best["event_id"] in it["events"]), None)
    name = item["root_names"][0] if item else best["name"]
    return name, resource_of(best)


def make_view(deps: Deps, state: dict, *, decision: str | None = None) -> dict:
    sem = deps.sem
    frame = state.get("frame") or {}
    g = gather(state)
    led = state.get("ledger") or {}
    hyps = state.get("hypotheses") or {}
    post = led.get("post") or {}
    nota = led.get("nota")
    dec = decision or (state.get("incident") or {}).get("decision") or "in-progress"
    view = dict(state.get("incident") or {})
    t_first = frame.get("t_first") or 0.0
    view.update({
        "incident_no": state.get("incident_no", ""), "name": frame.get("name", ""), "type": "ietf-incident:problem",
        "raw_incident_id": state.get("raw_incident_id"), "created_at": state.get("created_at"),
        "frame": {"items": frame.get("items", []), "services": frame.get("services", []), "anchor": frame.get("anchor")},
        "decision": dec, "nota": nota, "kb_release": deps.kb_release, "llm_profile": deps.llm_profile,
        "onset_iso": [_iso(state, frame["onset"][0]), _iso(state, frame["onset"][1])] if frame else None,
    })
    view.setdefault("times", {})
    view["times"] = dict(view["times"])
    view["times"].setdefault("occur", _iso(state, t_first))
    view["alarms"] = [{"event_id": a["event_id"], "name": a["name"], "resource": resource_of(a)} for a in g["alarms"]]
    ppe_events = []
    for och, tr in sorted(g["ppe_read"].items()):
        if tr is None:
            continue
        cls = classify_trace(tr)
        metric = {"step": ("ppe-step-db", -3.0), "slope": ("ppe-slope-db-per-km", 0.08),
                  "rx_offset": ("ppe-rx-offset-db", 1.0), "null": ("ppe-null", 0.0)}.get(cls["class"])
        if metric is None:
            continue
        ppe_events.append({"event_id": tr["event_id"], "och": och, "metric_name": metric[0], "threshold": metric[1],
                           "observed": cls.get("magnitude_db") if cls.get("magnitude_db") is not None else 0.0})
    view["ppe_events"] = ppe_events
    ranked = led.get("ranked") or []
    view["candidates"] = [{"rank": i + 1, "mode": hyps[h]["mode"], "location": hyps[h]["location"], "p": post[h]}
                          for i, h in enumerate(ranked[:5]) if h in hyps]

    def member(hid: str) -> dict:
        h = hyps[hid]
        m = sem.mode(h["mode"])
        node, net = _node_ref(sem, h["location"])
        return {"hid": hid, "mode": h["mode"], "location": h["location"], "p": post.get(hid, 0.0),
                "category": m["category"], "cause_name": m["cause_name"], "cause_name_ext": m["cause_name_ext"],
                "node_ref": node, "network_ref": net}

    view.pop("leader", None)
    view.pop("set", None)
    view["sources"], view["probable_events"] = [], []
    if ranked and dec == "committed":
        lead = member(ranked[0])
        ru = ranked[1] if len(ranked) > 1 else None
        lead["detail"] = (f"{lead['mode']} at {lead['location']}; P = {_fmt_p(lead['p'])}"
                          + (f"; runner-up {hyps[ru]['mode']} at {hyps[ru]['location']}, P = {_fmt_p(post[ru])}" if ru else "")
                          + ".")
        fit = leader_fit(sem, deps, state, ranked[0])
        if fit:
            lead["resource_detail"] = (f"{'Break' if fit['kind'] == 'step' else 'Added loss'} {fit['position_km']:.1f} +/- "
                                       f"{fit['tol_km']:.1f} km from {fit['from_ne']} on a {fit['span_length_km']:.0f} km "
                                       f"span; PPE {fit['kind']} {fit['magnitude_db']} on {fit['och']} at z = {fit['z_km']} km.")
        view["leader"] = lead
        view["sources"] = _sources(sem, lead["location"])
        win = (state.get("windows") or {}).get(ranked[0], {})
        pe = []
        for s in sorted(win.get("states", {}).values(), key=lambda s: s["pid"]):
            if s["state"] != "MATCHED" or s["strength"] != "Required":
                continue
            if s["layer"] == "PPE":
                pe += [("ppe", e) for e in s["matched_by"]]
            elif s["condition"] not in ("ServiceDown", "ServiceDegraded"):
                first = sorted(s["matched_by"])[:3]
                pe += [("alarm", e) for e in first]
        view["probable_events"] = pe[:16]
    elif ranked and dec in ("set-reported", "probably-not-considered", "limit-reached"):
        acc, members = 0.0, []
        for h in ranked:
            members.append(member(h))
            acc += post[h]
            if acc >= 0.9 or len(members) >= 4:
                break
        view["set"] = members
        view["leader"] = member(ranked[0])
        for m in members:
            view["sources"] += _sources(sem, m["location"])
    return view


# ====================================================================== nodes
class Nodes:
    def __init__(self, deps: Deps):
        self.d = deps
        self.advisor = Advisor(deps.sem, deps.cfg)

    # ---------------------------------------------------------------- ingest
    def ingest(self, state: dict) -> dict:
        d = self.d
        clock = float(state.get("clock") or 0.0)
        seen = {o["event_id"] for o in state.get("obs_log", []) if o["kind"] == "alarm"}
        seen_tr = {o["trace"]["trace_id"] for o in state.get("obs_log", []) if o["kind"] == "ppe" and o.get("trace")}
        if state.get("frame") is None and not seen:
            t_first = d.substrate.first_alarm_time()
            if t_first is None:
                return {"clock": clock, "obs_log": []}
            clock = max(clock, t_first + float(d.cfg["frame"]["hold_s"]))
        new = [dict(a, kind="alarm") for a in d.substrate.alarms_until(clock) if a["event_id"] not in seen]
        if d.ppe_access == "streamed":
            for tr in d.substrate.ppe_traces_until(clock):
                if tr["trace_id"] not in seen_tr:
                    new.append({"kind": "ppe", "och": tr["och"], "trace": tr, "t": tr["t_obs"], "via": "stream"})
        out: dict[str, Any] = {"clock": clock, "obs_log": new}
        if state.get("incident_no"):
            closure, _ = run_client_script(d, state, clock)
            out["closure"] = closure
        return out

    def after_ingest(self, state: dict) -> str:
        if state.get("frame") is None:
            return "frame" if any(o["kind"] == "alarm" for o in state.get("obs_log", [])) else "__end__"
        return "evaluate"

    # ---------------------------------------------------------------- frame / raise
    def frame(self, state: dict) -> dict:
        d = self.d
        g = gather(state)
        fr = build_frame(d.sem, g["alarms"], [t for t in g["ppe_read"].values() if t], t_frame=state["clock"],
                         scope_hops=d.cfg["frame"].get("scope_hops", 2), hold_s=d.cfg["frame"]["hold_s"])
        tb = TreeBuilder(state.get("tree", []), 0, state["clock"])
        root = tb.add("root", f"Incident — what is the root cause of {fr['name']}?", parent=None,
                      detail={"question": state.get("question", {}).get("text", "")})
        tb.add("frame", f"frame: onset [{fr['onset'][0]:+.1f}, {fr['onset'][1]:+.1f}] s · {len(fr['items'])} items · "
                        f"{len(fr['services'])} services · {len(fr['shared'])} shared · {len(fr['quiet'])} quiet",
               parent=root, detail={"onset": fr["onset"], "items": [_item_txt(it) for it in fr["items"]],
                                    "services": fr["services"], "shared": fr["shared"],
                                    "quiet": fr["quiet"][:30], "role_form": fr["role_form"], "name": fr["name"]})
        _emit(d, state, tb.new)
        return {"frame": fr, "tree": tb.new, "cycle": 0, "mode": "diagnose", "pass_no": 0}

    def raise_incident(self, state: dict) -> dict:
        d = self.d
        fr = state["frame"]
        view = make_view(d, state, decision="in-progress")
        view["times"]["raise"] = _iso(state, state["clock"])
        view["detail"] = raise_detail(fr, "dx-?-1")
        view = d.incidents.on_raise(view)
        no = view["incident_no"]
        task = view["tasks"][0]
        view["detail"] = raise_detail(fr, task["task_id"])
        d.incidents.store.save_view(dict(d.incidents.store.view(no), detail=view["detail"]))
        tb = TreeBuilder(state.get("tree", []), 0, state["clock"])
        root = next(n for n in state["tree"] if n["kind"] == "root")
        tb.update(root, label=f"Incident {no} — what is the root cause of {fr['name']}?")
        st = dict(state, incident_no=no)
        _emit(d, st, tb.new)
        closure, _ = run_client_script(d, st, state["clock"])
        return {"incident_no": no, "incident": {"decision": "in-progress", "times": view["times"],
                                                "detail": view["detail"]},
                "task": task, "tree": tb.new, "closure": closure}

    def retrieve_episodes(self, state: dict) -> dict:
        return {"line_results": {}}

    def fan_out(self, state: dict):
        from langgraph.types import Send
        return [Send(f"line_{l}", state) for l in self.d.cfg["lines"]]

    # ---------------------------------------------------------------- lines
    def line_walk(self, state: dict) -> dict:
        r = walk.run(self.d.sem, self.d.evaluator, state["frame"], _ctx(self.d, state), pass_no=state.get("pass_no", 0))
        return {"line_results": {"walk": r}}

    def line_correlation(self, state: dict) -> dict:
        r = correlation.run(self.d.sem, self.d.evaluator, state["frame"], _ctx(self.d, state),
                            set_mass=self.d.cfg["ledger"].get("set_mass", 0.9))
        return {"line_results": {"correlation": r}}

    def line_analogy(self, state: dict) -> dict:
        r = analogy.run(self.d.sem, self.d.episodic, state["frame"])
        return {"line_results": {"analogy": r}}

    def line_llm(self, state: dict) -> dict:
        pc = self.d.cfg.get("proposer", {})
        r = llm_proposer.run(self.d.sem, self.d.evaluator, self.d.llm, state["frame"], _ctx(self.d, state),
                             max_tool_calls=pc.get("max_tool_calls", 8), max_neighbors=pc.get("max_neighbors", 30),
                             pass_no=state.get("pass_no", 0))
        return {"line_results": {"llm": r}}

    # ---------------------------------------------------------------- pool
    def pool(self, state: dict) -> dict:
        d = self.d
        results = {l: state["line_results"].get(l, {"status": "abstain", "dist": {}, "residual": 1.0, "hyps": {}})
                   for l in d.cfg["lines"]}
        weights = d.cfg.get("line_weights", {})
        old = state.get("hypotheses") or {}
        by_key = {h["key"]: h for h in old.values()}
        pl = pooling.pool(results, weights, existing={k: h for k, h in by_key.items()})
        next_n = max([int(h[1:]) for h in old] + [0]) + 1
        hyps: dict[str, dict] = {}
        for key in sorted(pl["hyps"], key=lambda k: (-pl["prior"][k], k)):
            info = pl["hyps"][key]
            if key in by_key:
                hid = by_key[key]["id"]
            else:
                hid = f"H{next_n}"
                next_n += 1
            hyps[hid] = {"id": hid, "key": key, "mode": info["mode"], "location": info["location"],
                         "lines": {l: round(p, 6) for l, p in info["lines"].items()},
                         "provenance": info["provenance"], "pass_no": state.get("pass_no", 0)}
        prior = {hid: pl["prior"][h["key"]] for hid, h in hyps.items()}
        prior["NOTA"] = pl["prior"]["NOTA"]
        cycle = state.get("cycle", 0)
        tb = TreeBuilder(state.get("tree", []), cycle, state["clock"])
        root = root_id(state["tree"])
        pass_no = state.get("pass_no", 0)
        budget = dict(state.get("budget") or {})
        for l in d.cfg["lines"]:
            r = results[l]
            det = r.get("detail") or {}
            if l == "llm" and det.get("usage"):
                budget["llm_calls"] = budget.get("llm_calls", 0) + det.get("llm_calls", 0)
                budget["tokens"] = budget.get("tokens", 0) + det["usage"]["input"] + det["usage"]["output"]
            dist_txt = " · ".join(f"{k} {v:.3f}" for k, v in sorted(r["dist"].items(), key=lambda x: -x[1])[:6])
            label = f"generate[{l}] pass {pass_no + 1}: " + (dist_txt + f" · residual {r['residual']:.3f}"
                                                              if r["status"] == "ok" else f"abstain ({det.get('reason', '')})")
            gid = tb.add("generate", label, parent=root, detail={"line": l, "status": r["status"], "dist": r["dist"],
                                                                 "residual": r["residual"],
                                                                 "detail": _trim(det)})
            for pr in r.get("pruned", [])[:8]:
                tb.add("pruned", f"pruned {pr['key']}: {pr['reason']}", parent=gid, taken=False, detail=pr)
            if l == "analogy" and r["status"] == "ok":
                top = det.get("used", [{}])[0]
                tb.add("analogy", f"analogy: episode {top.get('episode')} (incident {top.get('incident_no')}) "
                                  f"similarity {top.get('similarity')} weight {top.get('weight')} → "
                                  f"{top.get('mode')}@{', '.join(top.get('resolved', []))}", parent=gid, detail=det)
        for hid, h in hyps.items():
            if hid in old:
                continue
            tb.add("hypothesis", f"{_hyp_label(h)} prior {prior[hid]:.4f}", parent=root, mass=prior[hid],
                   detail={"lines": h["lines"], "provenance": h["provenance"]})
        visited = sorted(set(state.get("walk_visited") or []) | set(results.get("walk", {}).get("visited", []))
                         | set(results.get("llm", {}).get("visited", [])))
        led = dict(state.get("ledger") or {})
        led["prior"] = prior
        led["pool"] = {"weights": pl.get("weights", {}), "abstained": pl.get("abstained", []), "folded": pl["folded"]}
        _emit(d, state, tb.new)
        return {"hypotheses": hyps, "ledger": led, "tree": tb.new, "budget": budget, "walk_visited": visited}

    # ---------------------------------------------------------------- evaluate
    def evaluate(self, state: dict) -> dict:
        d = self.d
        g = gather(state)
        fr = build_frame(d.sem, g["alarms"], [t for t in g["ppe_read"].values() if t], t_frame=state["clock"],
                         scope_hops=d.cfg["frame"].get("scope_hops", 2), hold_s=d.cfg["frame"]["hold_s"])
        old_fr = state["frame"]
        fr["t_frame"] = old_fr["t_frame"]
        fr["name"], fr["anchor"] = old_fr["name"], old_fr["anchor"] if any(
            it["id"] == old_fr["anchor"] for it in fr["items"]) else fr["anchor"]
        ctx = d.evaluator.context(fr, g["alarms"], g["ppe_read"], state["clock"], g["audits"])
        windows, preds = {}, {}
        counts = {}
        for hid, h in (state.get("hypotheses") or {}).items():
            ev = d.evaluator.evaluate(hid, h["mode"], h["location"], ctx)
            windows[hid] = {"states": ev["states"], "explained": ev["explained"],
                            "keyword_resources": ev["keyword_resources"], "dead": ev["dead"]}
            preds[hid] = ev["preds"]
            c = {"MATCHED": 0, "PENDING": 0, "MISSING": 0, "CONTRADICTED": 0, "UNOBSERVABLE": 0}
            for s in ev["states"].values():
                if s["strength"] != "Possible":
                    c[s["state"]] += 1
            counts[hid] = c
        cycle = state.get("cycle", 0) + 1
        tb = TreeBuilder(state.get("tree", []), cycle, state["clock"])
        txt = " · ".join(f"{hid} {c['MATCHED']}✓ {c['MISSING'] + c['CONTRADICTED']}✗ {c['PENDING']}⧗"
                         for hid, c in list(counts.items())[:8])
        tb.add("evaluate", f"evaluate cycle {cycle} at {state['clock']:+.1f} s: {txt}", parent=root_id(state["tree"]),
               detail={"counts": counts, "new_items": [it["id"] for it in fr["items"]
                                                        if it["id"] not in {x["id"] for x in old_fr["items"]}]})
        _emit(d, state, tb.new)
        return {"frame": fr, "windows": windows, "predictions": preds, "tree": tb.new, "cycle": cycle}

    # ---------------------------------------------------------------- synthesize
    def synthesize(self, state: dict) -> dict:
        d = self.d
        hyps = state.get("hypotheses") or {}
        win = state.get("windows") or {}
        g = gather(state)
        fr = state["frame"]
        prior = state["ledger"]["prior"]
        rules = T.rule_terms(d.sem, fr, hyps, {h: win[h]["states"] for h in hyps},
                             {h: win[h]["explained"] for h in hyps}, g["ppe_read"])
        evidence: dict[str, dict] = {}
        doc, ppe, gc, gc_ppe = {}, {}, {}, {}
        for h in hyps:
            v, debits = T.doc_term(fr, win[h]["states"], win[h]["explained"])
            pv, pdeb = T.ppe_term(win[h]["states"])
            doc[h], ppe[h] = v, pv
            gc[h], gc_ppe[h] = rules[h]["gc"], rules[h]["gc_ppe"]
            evidence[h] = {"doc": v, "doc_debits": debits, "ppe": pv, "ppe_debits": pdeb,
                           "gc_rules": rules[h]["gc_rules"], "gc_ppe_rules": rules[h]["gc_ppe_rules"]}
        terms = [{"name": "T_doc", "tau": T.DOC_TAU, "values": doc, "nota": -2.0},
                 {"name": "T_gc", "tau": T.GC_TAU, "values": gc, "nota": "neutral",
                  "rules": {h: rules[h]["gc_rules"] for h in hyps}}]
        if any(tr is not None for tr in g["ppe_read"].values()):
            terms.append({"name": "T_ppe", "tau": T.PPE_TAU, "values": ppe, "nota": -2.0})
            terms.append({"name": "T_gc_ppe", "tau": T.GC_TAU, "values": gc_ppe, "nota": "neutral",
                          "rules": {h: rules[h]["gc_ppe_rules"] for h in hyps}})
        terms += T.check_terms(d.sem, hyps, g["field_obs"])
        syn = L.synthesize(prior, terms, clip_nats=d.cfg["ledger"]["clip_nats"])
        n_checks = sum(1 for o in state.get("obs_log", []) if o["kind"] == "check" and o["check_id"] != "CHK_WAIT")
        hist = list((state.get("ledger") or {}).get("history", []))
        prev_checks = hist[-1]["n_checks"] if hist else 0
        hist.append({"cycle": state.get("cycle", 0), "t": round(state["clock"], 3), "entropy": round(syn["entropy"], 4),
                     "leader": syn["leader"], "p": round(syn["post"].get(syn["leader"], 0), 6) if syn["leader"] else 0,
                     "nota": round(syn["nota"], 6), "n_checks": n_checks, "after_check": n_checks > prev_checks})
        led = dict(state["ledger"])
        led.update({"terms": syn["terms"], "post": syn["post"], "nota": syn["nota"], "ranked": syn["ranked"],
                    "leader": syn["leader"], "runner_up": syn["runner_up"], "entropy": syn["entropy"],
                    "entropy_prior": syn["entropy_prior"], "evidence": evidence, "history": hist})
        cycle = state.get("cycle", 0)
        tb = TreeBuilder(state.get("tree", []), cycle, state["clock"])
        tip = _tip(state["tree"])
        for q in self._questions(state, evidence, prior):
            tb.add("question", q["label"], parent=tip, detail=q, taken=True)
        top = " · ".join(f"{h} {_fmt_p(syn['post'][h])}" for h in syn["ranked"][:4])
        tb.add("synthesis", f"synthesis cycle {cycle}: {top} · NOTA {_fmt_p(syn['nota'])} · entropy "
                            f"{syn['entropy_prior']:.2f} → {syn['entropy']:.2f} bits",
               parent=root_id(state["tree"]), posterior={h: round(v, 6) for h, v in syn["post"].items()},
               detail={"terms": syn["terms"]})
        _emit(d, state, tb.new)
        if state.get("incident_no"):
            d.incidents.ledger_snapshot(state["incident_no"], {"cycle": cycle, "posterior": syn["post"], "nota": syn["nota"]})
        return {"ledger": led, "tree": tb.new}

    def _questions(self, state: dict, evidence: dict, prior: dict) -> list[dict]:
        """IG-ordered yes/no questions that winnowing has already answered (doc 04 §9)."""
        hyps = state.get("hypotheses") or {}
        win = state.get("windows") or {}
        asked = {n["detail"].get("key") for n in state.get("tree", []) if n["kind"] == "question"}
        qs: dict[str, dict] = {}
        live = [h for h in hyps if prior.get(h, 0) >= 0.002]
        for h in live:
            for s in win[h]["states"].values():
                if s["strength"] == "Required" and s["state"] in ("MISSING", "CONTRADICTED") and s["layer"] != "PPE":
                    key = f"seen:{s['condition']}@{s['resource']}"
                    q = qs.setdefault(key, {"key": key, "kind": "window", "answer": "no", "eliminates": [],
                                            "text": f"{s['condition']} at {s['resource']} (window closed "
                                                    f"{s['closes_at']:+.1f} s)?"})
                    q["eliminates"].append(h)
                if s["layer"] == "PPE" and s["state"] == "CONTRADICTED":
                    key = f"ppe:{s['resource']}"
                    q = qs.setdefault(key, {"key": key, "kind": "ppe", "answer": answer_key(s["observed"]),
                                            "eliminates": [], "text": f"PPE profile of {s['resource']} as predicted?"})
                    q["eliminates"].append(h)
            for r in evidence[h]["gc_rules"] + evidence[h]["gc_ppe_rules"]:
                if r == "R1":
                    continue
                rule = next((x for x in self.d.sem.rules() if x["id"] == r), {"label": r})
                key = f"rule:{r}"
                q = qs.setdefault(key, {"key": key, "kind": "rule", "answer": "violated", "eliminates": [],
                                        "text": f"{r}: {rule['label']}?"})
                q["eliminates"].append(h)
        out = []
        for key, q in qs.items():
            if key in asked:
                continue
            answers = {h: ("out" if h in q["eliminates"] else "in") for h in hyps}
            p = {h: prior.get(h, 0) for h in hyps}
            p["NOTA"] = prior.get("NOTA", 0)
            bits, _, _ = mutual_information(p, answers)
            q["bits"] = round(bits, 4)
            names = ", ".join(sorted(set(q["eliminates"]), key=lambda x: int(x[1:])))
            q["label"] = f"Q {q['text']} ─ {q['answer']} ─▶ {names} out (IG {bits:.2f} bit)"
            if bits >= 0.005:
                out.append(q)
        out.sort(key=lambda q: (-q["bits"], q["key"]))
        return out[:6]

    # ---------------------------------------------------------------- decide
    def decide(self, state: dict) -> dict:
        d = self.d
        led = state["ledger"]
        hyps = state.get("hypotheses") or {}
        win = state.get("windows") or {}
        g = gather(state)
        evidence = {h: dict(led["evidence"][h], states=win[h]["states"]) for h in hyps}
        actx = {"hyps": hyps, "evidence": {h: dict(evidence[h], preds=state["predictions"][h]) for h in hyps},
                "clock": state["clock"], "budget": state.get("budget") or {}, "done": g["done"],
                "leader": led.get("leader")}
        confirm = self.advisor.confirm_check(actx, led["leader"]) if led.get("leader") else None
        decision, info = L.decide(led, evidence, d.cfg, state.get("budget") or {}, led.get("history", []),
                                  pass_no=state.get("pass_no", 0), confirm_check=confirm)
        info["confirm"] = confirm
        mode = state.get("mode", "diagnose")
        prev = state.get("decision_info") or {}
        cycle = state.get("cycle", 0)
        tb = TreeBuilder(state.get("tree", []), cycle, state["clock"])
        tip = _tip(state["tree"])
        out: dict[str, Any] = {}
        if mode == "verify":
            committed = prev.get("committed")
            lead = led["leader"]
            if decision == "commit" and committed and hyps[lead]["key"] == committed["key"]:
                decision = "verified"
                info["committed"] = committed
                vnode = next((n for n in reversed(state["tree"]) if n["kind"] == "decision"
                              and n["detail"].get("decision") == "commit"), None)
                tb.add("verify", f"✓ verify {state['clock']:+.1f} s: P = {_fmt_p(info['p'])} NOTA = {_fmt_p(info['nota'])}",
                       parent=vnode["id"] if vnode else tip, posterior={lead: round(info["p"], 6)},
                       detail={"t": state["clock"], "p": info["p"], "nota": info["nota"]})
            else:
                mode = "diagnose"
                out["mode"] = mode
                if decision == "commit":
                    decision = "continue" if confirm else decision
        if decision == "commit":
            lead = led["leader"]
            h = hyps[lead]
            fit = leader_fit(d.sem, d, state, lead)
            info["committed"] = {"hid": lead, "key": h["key"], "mode": h["mode"], "location": h["location"],
                                 "p": info["p"], "nota": info["nota"], "t": state["clock"], "fit": fit}
            tb.add("decision", f"◆ COMMIT {h['mode']} @ {h['location']}"
                               + (f", {fit['position_km']:.1f} ± {fit['tol_km']:.1f} km" if fit and fit.get("position_km")
                                  is not None else "")
                               + f"   P = {_fmt_p(info['p'])}  NOTA = {_fmt_p(info['nota'])}",
                   parent=tip, posterior={lead: round(info["p"], 6)},
                   detail={"decision": "commit", "tests": info["tests"], "leader": lead, "fit": fit})
            out["mode"] = "verify"
        elif decision in ("idk", "limit"):
            tb.add("decision", f"◆ {decision.upper()}: leader {info['leader']} P = {_fmt_p(info['p'])}, NOTA = "
                               f"{_fmt_p(info['nota'])}", parent=tip,
                   detail={"decision": decision, "tests": info["tests"], "reasons": info.get("regen_reasons")})
            out["mode"] = "closed"
        elif decision == "regenerate":
            tb.add("decision", f"↺ return pass {state.get('pass_no', 0) + 1}/{d.cfg['advisor']['max_returns']}: "
                               f"{', '.join(info.get('regen_reasons') or [])}", parent=tip,
                   detail={"decision": "regenerate", "tests": info["tests"], "reasons": info.get("regen_reasons")})
        _emit(d, state, tb.new)
        out.update({"decision": decision, "decision_info": info, "tree": tb.new})
        return out

    def route_decision(self, state: dict) -> str:
        return {"commit": "narrate", "continue": "plan", "regenerate": "widen", "idk": "narrate", "limit": "narrate",
                "verified": "update_incident"}[state["decision"]]

    # ---------------------------------------------------------------- plan / execute
    def plan(self, state: dict) -> dict:
        d = self.d
        led = state["ledger"]
        hyps = state["hypotheses"]
        win = state["windows"]
        g = gather(state)
        evidence = {h: dict(led["evidence"][h], states=win[h]["states"], preds=state["predictions"][h]) for h in hyps}
        info = state.get("decision_info") or {}
        confirm = info.get("confirm") if info.get("tests", {}).get("p_leader", {}).get("ok") else None
        actx = {"hyps": hyps, "evidence": evidence, "clock": state["clock"], "budget": state.get("budget") or {},
                "done": g["done"], "leader": led["leader"]}
        pl = self.advisor.plan(actx, led["post"], confirm=confirm)
        cycle = state.get("cycle", 0)
        tb = TreeBuilder(state.get("tree", []), cycle, state["clock"])
        tip = _tip(state["tree"])
        if pl is None:
            tb.add("decision", f"◆ IDK: no admissible check (leader {led['leader']} P = "
                               f"{_fmt_p(led['post'].get(led['leader'], 0))})", parent=tip,
                   detail={"decision": "idk", "reason": "no admissible check"})
            _emit(d, state, tb.new)
            return {"plan": None, "decision": "idk", "tree": tb.new, "decision_info": dict(info, no_check=True)}
        chosen = pl["chosen"]
        orch = {"index": 0, "source": "confirmation rule"} if chosen.get("reason") == "confirm" else \
            orchestrate(d.llm, pl["ranked"], enabled=bool(d.cfg.get("orchestrator", True)))
        if orch.get("index") not in (None, 0) and chosen.get("reason") != "confirm":
            adm = [c for c in pl["ranked"] if c["admissible"]]
            chosen = adm[orch["index"]]
            chosen["branches"] = self.advisor.forward(actx, led["post"], chosen, depth=1)
        budget = dict(state.get("budget") or {})
        if orch.get("source") == "llm":
            budget["llm_calls"] = budget.get("llm_calls", 0) + 1
        why = orch.get("rationale") or ("confirm the leader's Required prediction"
                                        if chosen.get("reason") == "confirm" else "highest bits per hour")
        ranked_txt = "  >  ".join(f"{c['check_id']}({c['target']}) {c['rate']:.1f}" for c in pl["ranked"][:4])
        cid = tb.add("check", f"Q [check] {chosen['check_id']}({chosen['target']}): {chosen['bits']:.4f} bits, "
                              f"{_effort_txt(chosen['effort_h'])}, {chosen['rate']:.1f} bits/h   ⧗ running",
                     parent=tip, detail={"check_id": chosen["check_id"], "target": chosen["target"],
                                         "bits": chosen["bits"], "effort_h": chosen["effort_h"], "rate": chosen["rate"],
                                         "why": why, "orchestrator": orch.get("source"), "ranked": ranked_txt,
                                         "classes": chosen["classes"], "p_answer": chosen["p_answer"]})
        branch_ids = {}
        for i, b in enumerate(chosen.get("branches", [])):
            lab = f"[{chr(97 + i)}] {b['answer']} ({b['p']:.4f}: {', '.join(b['hyps']) or 'unspecified'}) ┄▶ {b['next']}"
            branch_ids[b["answer"]] = tb.add("answer", lab, parent=cid, taken=False, mass=b["p"], detail=b)
        plan = {"check_id": chosen["check_id"], "target": chosen["target"], "until": chosen.get("until"),
                "node": cid, "branches": branch_ids, "answers": chosen["answers"], "classes": chosen["classes"],
                "bits": chosen["bits"], "effort_h": chosen["effort_h"], "rate": chosen["rate"],
                "ranked": [{k: c[k] for k in ("check_id", "target", "bits", "effort_h", "rate", "admissible")}
                           for c in pl["ranked"]], "why": why}
        _emit(d, state, tb.new)
        return {"plan": plan, "tree": tb.new, "budget": budget, "decision": "continue"}

    def route_plan(self, state: dict) -> str:
        return "execute_check" if state.get("plan") else "narrate"

    def execute_check(self, state: dict) -> dict:
        d = self.d
        plan = state["plan"]
        cid, tgt = plan["check_id"], plan["target"]
        chk = d.sem.checks()[cid]
        if not chk["remote"] and d.live_field:
            from langgraph.types import interrupt
            answer = interrupt({"check_id": cid, "target": tgt, "question": f"Run {cid} on {tgt} and report"})
            obs = {"kind": "check", "check_id": cid, "target": tgt, "t_start": state["clock"],
                   "t_done": state["clock"] + chk["minutes"] * 60, "answer": answer, "field": True, "remote": False}
        else:
            obs = d.dispatcher.run(cid, tgt, state["clock"], until=plan.get("until"))
        t_done = obs["t_done"]
        new_obs = [{k: v for k, v in obs.items() if k not in ("trace", "alarms")}]
        if cid == "CHK_PPE_PROFILE":
            new_obs.append({"kind": "ppe", "och": tgt, "trace": obs.get("trace"), "t": t_done, "via": "check"})
        if obs.get("audit"):
            new_obs.append(dict(obs["audit"], kind="audit"))
            for a in obs.get("alarms", []):
                new_obs.append(dict(a, kind="alarm"))
        budget = dict(state.get("budget") or {})
        budget["checks"] = budget.get("checks", 0) + (0 if cid == "CHK_WAIT" else 1)
        eff = (t_done - state["clock"]) / 3600.0 if cid == "CHK_WAIT" else plan["effort_h"]
        budget["effort_h"] = round(budget.get("effort_h", 0.0) + eff, 6)
        budget["remote_checks" if chk["remote"] else "field_checks"] = \
            budget.get("remote_checks" if chk["remote"] else "field_checks", 0) + (0 if cid == "CHK_WAIT" else 1)
        budget["checks_done"] = list(budget.get("checks_done", [])) + [f"{cid}({tgt})"]
        if not chk["remote"]:
            from ..reasoning.advisor import _site
            site = _site(d.sem, cid, tgt)
            if site and site not in budget.get("sites", []):
                budget["sites"] = list(budget.get("sites", [])) + [site]
        observed, text = self._observed_class(state, plan, obs)
        tb = TreeBuilder(state.get("tree", []), state.get("cycle", 0), t_done)
        node = next(n for n in state["tree"] if n["id"] == plan["node"])
        tb.update(node, label=node["label"].replace("⧗ running", f"✔ done {t_done:+.1f} s"))
        if observed in plan["branches"]:
            bn = next(n for n in state["tree"] if n["id"] == plan["branches"][observed])
            tb.update(bn, taken=True, label=bn["label"].split(" ┄▶")[0].replace("(", f"{text} (", 1) + "  ✔",
                      evidence=[obs.get("trace", {}).get("event_id")] if obs.get("trace") else [])
        else:
            tb.add("answer", f"{observed}: {text}  ✔", parent=plan["node"], taken=True,
                   detail={"answer": observed, "obs": obs.get("answer")})
        _emit(d, state, tb.new)
        return {"obs_log": new_obs, "clock": t_done, "budget": budget, "tree": tb.new, "pending_check": None}

    def _observed_class(self, state: dict, plan: dict, obs: dict) -> tuple[str, str]:
        cid = plan["check_id"]
        if cid == "CHK_PPE_PROFILE":
            cls = classify_trace(obs.get("trace"))
            key = answer_key(cls)
            if cls["class"] == "step":
                return key, (f"{cls['magnitude_db']:+.1f} dB step {cls['offset_km']} km into {cls['span']} "
                             f"(z = {cls['z_km']} km)")
            if cls["class"] == "slope":
                return key, f"slope {cls['magnitude_db']} dB/km in {cls['span']}"
            return key, cls["class"]
        if cid in ("CHK_WAIT", "CHK_ALARM_AUDIT") or obs.get("audit"):
            keys = set()
            for a in plan["answers"].values():
                if a and a.startswith("seen:"):
                    keys |= set(a[5:].split(","))
            alarms = self.d.substrate.alarms_until(obs["t_done"]) + obs.get("alarms", [])
            seen = set()
            for k in keys:
                cond, res = k.split("@", 1)
                if any(resource_of(a) == res and self.d.sem.cond_subsumes(cond, self.d.sem.alarm_type(a["name"])["condition"])
                       for a in alarms):
                    seen.add(k)
            cls = "seen:" + ",".join(sorted(seen)) if seen else "nothing"
            return cls, ("seen " + ", ".join(sorted(seen))) if seen else "nothing seen"
        cls = field_answer_class(cid, obs.get("answer") or {})
        return cls, cls + (f" at {obs['answer']['position_km']} km" if obs.get("answer", {}).get("position_km") else "")

    # ---------------------------------------------------------------- widen
    def widen(self, state: dict) -> dict:
        budget = dict(state.get("budget") or {})
        budget["returns"] = budget.get("returns", 0) + 1
        return {"pass_no": state.get("pass_no", 0) + 1, "budget": budget, "line_results": {}}

    # ---------------------------------------------------------------- narrate / update
    def _slots(self, state: dict, decision: str) -> dict:
        d = self.d
        led = state["ledger"]
        hyps = state["hypotheses"]
        lead = led["leader"]
        h = hyps[lead]
        m = d.sem.mode(h["mode"])
        slots: dict[str, Any] = {"decision": decision, "mode": h["mode"], "label": m["label"], "location": h["location"],
                                 "p": f"{led['post'][lead]:.5f}", "nota": f"{led['nota']:.4f}"}
        k = d.sem.kind_of(h["location"])
        if k in ("span", "otu"):
            slots["ends"] = f"{d.sem.value(h['location'], 'sourceNE')}->{d.sem.value(h['location'], 'destNE')}"
        fit = leader_fit(d.sem, d, state, lead)
        if fit:
            slots["fit"] = {k2: fit[k2] for k2 in ("position_km", "tol_km", "from_ne", "och", "magnitude_db", "z_km")}
        ev = []
        if fit:
            ev.append(f"PPE {fit['kind']} of {fit['magnitude_db']} dB {fit['position_km']:.1f} km into {fit['span']} on {fit['och']}")
        for n in state.get("tree", []):
            if n["kind"] == "question" and n["detail"].get("eliminates"):
                elim = sorted(set(n["detail"]["eliminates"]), key=lambda x: int(x[1:]))[:3]
                names = ", ".join(f"{hyps[x]['mode']}@{hyps[x]['location']}" for x in elim if x in hyps)
                ev.append(f"{n['detail']['text'].rstrip('?')}: {n['detail']['answer']}, which rules out {names}")
            if len(ev) >= 4:
                break
        slots["evidence"] = ev
        ranked = led["ranked"]
        if decision != "committed":
            acc, members = 0.0, []
            for x in ranked:
                members.append({"mode": hyps[x]["mode"], "location": hyps[x]["location"], "p": f"{led['post'][x]:.3f}"})
                acc += led["post"][x]
                if acc >= 0.9 or len(members) >= 4:
                    break
            slots["set"] = members
            plan = state.get("plan") or {}
            if plan.get("ranked"):
                slots["next_check"] = f"{plan['ranked'][0]['check_id']}({plan['ranked'][0]['target']})"
        return slots

    def narrate(self, state: dict) -> dict:
        d = self.d
        decision = state["decision"]
        led = state["ledger"]
        dec = {"commit": "committed", "limit": "limit-reached"}.get(decision)
        if dec is None:
            dec = "set-reported" if led.get("nota", 1) < d.cfg["ledger"]["nota_max"] else "probably-not-considered"
        inc = dict(state.get("incident") or {})
        budget = dict(state.get("budget") or {})
        if not led.get("leader"):
            inc.update({"decision": "probably-not-considered", "detail": "No documented fault mode explains the symptoms."})
            return {"incident": inc}
        slots = self._slots(state, dec)
        nar = run_narrator(d.llm, slots, enabled=bool(d.cfg.get("narrator", True)))
        if nar["source"] == "llm" or nar.get("rejected"):
            budget["llm_calls"] = budget.get("llm_calls", 0) + 1
        inc.update({"decision": dec, "detail": nar["detail"], "narration": {k: v for k, v in nar.items() if k != "detail"}})
        if dec == "committed":
            h = state["hypotheses"][led["leader"]]
            m = d.sem.mode(h["mode"])
            root_alarm, root_port = _root_symptom(state, led["leader"])
            fit = leader_fit(d.sem, d, state, led["leader"])
            src = d.sem.value(h["location"], "sourceNE") or d.sem.hosting_ne(h["location"]) or h["location"]
            adv = m["resolve_advice"]
            for k, v in {"{location}": h["location"], "{source_ne}": src, "{position_text}": position_text(fit),
                         "{root_alarm}": root_alarm or "the root alarm", "{root_port}": root_port or h["location"]}.items():
                adv = adv.replace(k, v)
            inc["resolve_advice"] = adv
        tb = TreeBuilder(state.get("tree", []), state.get("cycle", 0), state["clock"])
        root = next(n for n in state["tree"] if n["kind"] == "root")
        tb.update(root, detail=dict(root["detail"], narration=nar["detail"], narration_source=nar["source"]))
        _emit(d, state, tb.new)
        return {"incident": inc, "budget": budget, "tree": tb.new}

    def update_incident(self, state: dict) -> dict:
        d = self.d
        inc = dict(state.get("incident") or {})
        view = make_view(d, state, decision=inc.get("decision"))
        view["times"]["last_updated"] = _iso(state, state["clock"])
        sent = d.incidents.on_update(view, event_time=_iso(state, state["clock"]))
        task = dict(state.get("task") or {})
        dec = inc.get("decision")
        if state["decision"] == "commit":
            task.update(state="diagnosed", result="succeeded", end_time=_iso(state, state["clock"]),
                        description=_task_description(state), view=make_view(d, state, decision="committed"),
                        repair_advices=inc.get("resolve_advice"))
            d.incidents.on_task(state["incident_no"], task)
        elif state["decision"] in ("idk", "limit"):
            budget = state.get("budget") or {}
            task.update(state="finished", result="failed", end_time=_iso(state, state["clock"]),
                        description=_task_description(state), view=view,
                        exhausted=budget.get("returns", 0) >= d.cfg["advisor"]["max_returns"])
            d.incidents.on_task(state["incident_no"], task)
        d.incidents.store.save_reasoning(state["incident_no"], tree=state.get("tree"), ledger=_ledger_public(state))
        inc["last_update_sent"] = sent
        inc["times"] = view["times"]
        return {"incident": inc, "task": task}

    # ---------------------------------------------------------------- verification
    def _verify_pending(self, state: dict) -> list[float]:
        led = state["ledger"]
        win = state.get("windows") or {}
        horizon = state["clock"] + 900.0
        times = []
        for h, w in win.items():
            p = led["post"].get(h, 0)
            if h != led.get("leader") and p < 1e-4:
                continue
            for s in w["states"].values():
                if s["state"] == "PENDING" and s["strength"] in ("Required", "Expected") and s["closes_at"] \
                        and state["clock"] < s["closes_at"] <= horizon:
                    times.append(s["closes_at"])
        return sorted(set(times))

    def verify_gate(self, state: dict) -> dict:
        return {}

    def route_verify(self, state: dict) -> str:
        if state.get("mode") == "verify" and self._verify_pending(state):
            return "wait"
        return "await_closure"

    def wait(self, state: dict) -> dict:
        times = self._verify_pending(state)
        return {"clock": times[0] + 0.001 if times else state["clock"]}

    # ---------------------------------------------------------------- closure / learning
    def await_closure(self, state: dict) -> dict:
        d = self.d
        no = state["incident_no"]
        task = dict(state.get("task") or {})
        out: dict[str, Any] = {}
        tb = TreeBuilder(state.get("tree", []), state.get("cycle", 0), state["clock"])
        if task.get("state") == "diagnosed":
            task.update(state="finished", end_time=_iso(state, state["clock"]))
            d.incidents.on_task(no, task)
            out["task"] = task
        sc = d.scenario or {}
        script_end = max([_script_time(state, e["at"]) for e in sc.get("client_script", [])] + [state["clock"]])
        repair = sc.get("repair")
        t_rep = _script_time(state, repair["at"]) if repair else None
        clock = state["clock"]
        closure, _ = run_client_script(d, state, max(script_end, t_rep or 0.0))
        cleared = False
        if repair and t_rep is not None and (state.get("decision_info") or {}).get("committed"):
            clock = max(clock, t_rep)
            committed = state["decision_info"]["committed"]
            note = repair.get("note", "")
            d.incidents.on_clear(no, _iso(state, clock))
            cleared = True
            closure.update({"cleared_at": round(clock, 3), "repair_note": note,
                            "verification": "repair_confirmed" if closure.get("resolve_requested") is not None or note
                            else "self"})
            vnode = next((n for n in reversed(state["tree"]) if n["kind"] == "decision"
                          and n["detail"].get("decision") == "commit"), None)
            tb.add("verify", f"✓ cleared {clock:+.0f} s: repair — {note or 'root symptoms cleared'}"
                             f" ({committed['mode']} @ {committed['location']})",
                   parent=vnode["id"] if vnode else root_id(state["tree"]), detail={"repair": repair})
        closure["cleared"] = cleared
        if not cleared:
            closure.setdefault("verification", "self" if (state.get("decision_info") or {}).get("committed") else "none")
        _emit(d, state, tb.new)
        d.incidents.store.save_reasoning(no, tree=list(state.get("tree", [])) + tb.new, ledger=_ledger_public(state))
        out.update({"closure": closure, "clock": clock, "tree": tb.new, "mode": "closed"})
        return out

    def learn(self, state: dict) -> dict:
        d = self.d
        closure = dict(state.get("closure") or {})
        committed = (state.get("decision_info") or {}).get("committed")
        if committed and d.episodic is not None and hasattr(d.episodic, "write_episode"):
            closure["episode_id"] = d.episodic.write_episode(
                episode_of(d.sem, state, closure.get("verification", "self")))
        if committed and d.publish_incident_graph:
            try:
                ttl = incident_graph_ttl(d.sem, state, committed)
                d.sem.publish(f"incident/{state['incident_no']}", ttl,
                              {"event": "Closure", "valid_from": _iso(state, state["frame"]["onset"][0]),
                               "used": f"diag_agent incident {state['incident_no']}"})
                closure["incident_graph"] = f"g:incident/{state['incident_no']}"
            except Exception as e:  # noqa: BLE001 - learning must not fail the diagnosis
                log.warning("incident graph publish failed: %s", e)
        return {"closure": closure}


# ====================================================================== misc
def episode_of(sem, state: dict, verification: str) -> dict:
    committed = state["decision_info"]["committed"]
    fr = state["frame"]
    eid = "ep-" + hashlib.sha1(f"{state['created_at']}|{state.get('raw_incident_id')}|{state['incident_no']}"
                               .encode()).hexdigest()[:12]
    return {"id": eid, "incident_no": state["incident_no"], "created_at": state["created_at"],
            "signature": fr["role_form"],
            "conclusion": {"mode": committed["mode"], "location": committed["location"],
                           "relations": conclusion_relations(sem, fr, committed["mode"], committed["location"])},
            "outcome": "committed", "verification": verification,
            "summary": f"{committed['mode']} at {committed['location']} (P {committed['p']:.4f})",
            "source": f"raw incident {state.get('raw_incident_id')}"}


def _item_txt(it: dict) -> str:
    return (f"{it['id']} {it['condition']} @ {it['resource']} [{it['layer']}] t {it['t_first']:+.2f} s · "
            f"{len(it['events'])} alarms ({', '.join(it['root_names'][:3])})")


def _effort_txt(h: float) -> str:
    return f"{h * 60:.0f} min" if h < 1 else f"{h:.2f} h"


def _trim(det: dict) -> dict:
    out = {}
    for k, v in det.items():
        if k in ("tool_trace",):
            out[k] = [{"tool": t["tool"], "args": t["args"], "result": t["result"][:200]} for t in v]
        elif k == "raw":
            out[k] = v
        else:
            out[k] = v
    return out


def _task_description(state: dict) -> str:
    b = state.get("budget") or {}
    checks = [c for c in b.get("checks_done", []) if not c.startswith("CHK_WAIT")]
    dec = state["decision"]
    head = {"commit": "Committed", "idk": "No commitment", "limit": "Budget limit reached"}.get(dec, dec)
    return (f"{head} after {state.get('cycle', 0)} decision cycles and {len(checks)} checks"
            + (f" ({', '.join(checks)})" if checks else "") + f", {b.get('effort_h', 0) * 60:.1f} min of check effort.")


def _ledger_public(state: dict) -> dict:
    led = dict(state.get("ledger") or {})
    led.pop("evidence", None)
    led.pop("log_post", None)
    return led


def incident_graph_ttl(sem, state: dict, committed: dict) -> str:
    """RFC 9940 incident graph (onm:) for a closed incident (doc 03 §6)."""
    no = state["incident_no"]
    frame = state["frame"]
    inc = f"<urn:x-diag-incident:{no}>"
    lines = ["@prefix onm: <https://optical-aiops.example/ns/rfc9940#> .",
             "@prefix dx: <https://optical-aiops.example/ns/diagnosis#> .",
             "@prefix kb: <https://optical-aiops.example/kb#> .",
             "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .", ""]
    lines.append(f"{inc} a onm:Incident ; dx:incidentNo \"{no}\" ; dx:mode kb:{committed['mode']} ;")
    lines.append(f"    dx:location <{sem.iri(committed['location'])}> ;")
    lines.append(f"    dx:probability \"{committed['p']:.6f}\"^^xsd:decimal ;")
    lines.append(f"    dx:onsetStart \"{_iso(state, frame['onset'][0])}\"^^xsd:dateTime .")
    for it in frame["items"]:
        sym = f"<urn:x-diag-incident:{no}:{it['id']}>"
        lines.append(f"{sym} a onm:Symptom ; dx:symptomOf {inc} ; dx:aboutResource <{sem.iri(it['resource'])}> ; "
                     f"dx:condition \"{it['condition']}\" .")
    return "\n".join(lines) + "\n"
