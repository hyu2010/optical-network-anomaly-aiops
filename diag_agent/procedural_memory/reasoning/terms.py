"""Winnowing terms (doc 04 §6.3).

These are the likelihood terms winnowing supplies to the ledger. Observation
windows (windows.py) only decide which predicted effects are eligible.

Every synthesis recomputes all terms from the evidence held so far, so no
observation is counted twice across cycles.

  T_doc     (tau 0.7)  -2 per missing Required, -0.6 per missing Expected, -2 per
                       unexplained relevant item; floor -8.  NOTA: -2.
  T_gc      (tau 0.3)  -4 per contradicted rule R1-R6, at most two count.  NOTA: neutral.
  T_ppe     (tau 0.7)  0 if every read PPE prediction matches, -8 otherwise.  NOTA: -2.
  T_gc_ppe  (tau 0.3)  R7 (a located step/slope where the hypothesis puts none): -4.  NOTA: neutral.
  T_check:* (tau of the check)  -8 if the predicted answer class differs.  NOTA: neutral.
"""

from __future__ import annotations

from rdflib import Graph, Literal
from rdflib.namespace import RDF

from ...semantic_memory.namespaces import CASE, DX, KB, OPTX
from .windows import classify_trace

DOC_TAU, GC_TAU, PPE_TAU = 0.7, 0.3, 0.7
DOC_FLOOR = -8.0
RULE_NATS, RULE_MAX = 4.0, 2
PPE_RULES = {"R7"}


def doc_term(frame: dict, states: dict[str, dict], explained: dict[str, bool]) -> tuple[float, list[dict]]:
    debits = []
    for s in states.values():
        if s["condition"] in ("ServiceDown", "ServiceDegraded") or s["layer"] == "PPE":
            continue
        if s["state"] in ("MISSING", "CONTRADICTED"):
            if s["strength"] == "Required":
                debits.append({"what": f"{s['condition']} @ {s['resource']} {s['state'].lower()}", "nats": -2.0,
                               "pid": s["pid"]})
            elif s["strength"] == "Expected":
                debits.append({"what": f"{s['condition']} @ {s['resource']} {s['state'].lower()}", "nats": -0.6,
                               "pid": s["pid"]})
    for it in frame["items"]:
        if it["relevant"] and not explained.get(it["id"], False):
            debits.append({"what": f"{it['id']} {it['condition']} @ {it['resource']} unexplained", "nats": -2.0,
                           "item": it["id"]})
    return max(DOC_FLOOR, sum(d["nats"] for d in debits)), debits


def ppe_term(states: dict[str, dict]) -> tuple[float | None, list[dict]]:
    read = [s for s in states.values() if s["layer"] == "PPE" and s["state"] in ("MATCHED", "CONTRADICTED")]
    bad = [s for s in read if s["state"] == "CONTRADICTED"]
    if not bad:
        return 0.0, []
    return -8.0, [{"what": f"PPE {s['resource']}: {s['reason']}", "nats": -8.0, "pid": s["pid"]} for s in bad]


def dominant_ppe_items(frame: dict, ppe_read: dict) -> list[str]:
    out = []
    for och, tr in ppe_read.items():
        if tr is None:
            continue
        cls = classify_trace(tr)
        if cls["class"] not in ("step", "slope"):
            continue
        for x in frame.get("ppe", []):
            if x["och"] == och and x["kind"] == cls["class"] and x.get("span") == cls.get("span") \
                    and x.get("magnitude_db") == cls.get("magnitude_db"):
                out.append(x["id"])
                break
    return out


def case_graph(sem, frame: dict, hyps: dict[str, dict], states: dict[str, dict[str, dict]],
               explained: dict[str, dict[str, bool]], ppe_read: dict) -> Graph:
    g = Graph()
    T = Literal(True)
    for hid, h in hyps.items():
        hn = CASE[hid]
        g.add((hn, DX.mode, KB[h["mode"]]))
        g.add((hn, DX.location, sem.iri(h["location"])))
        for pid, s in states[hid].items():
            pn = CASE[pid.replace(".", "_")]
            g.add((pn, DX.ofHypothesis, hn))
            g.add((pn, DX.predStrength, DX[s["strength"]]))
            g.add((pn, DX.predCondition, OPTX[s["condition"]]))
            g.add((pn, DX.predAt, sem.iri(s["resource"])))
            closed = s["state"] in ("MATCHED", "MISSING", "CONTRADICTED") and s["layer"] != "PPE"
            g.add((pn, DX.windowClosed, Literal(closed)))
            g.add((pn, DX.instrumentAlive, Literal(s["state"] != "UNOBSERVABLE")))
            if s["state"] == "MATCHED":
                for ref in s["items"]:
                    g.add((pn, DX.matchedBy, CASE[ref]))
    for it in frame["items"]:
        sn = CASE[it["id"]]
        g.add((sn, RDF.type, DX.FrameSymptom))
        g.add((sn, DX.symptomKind, Literal("alarm")))
        g.add((sn, DX.relevant, Literal(bool(it["relevant"]))))
        g.add((sn, DX.reportedBy, sem.iri(it["resource"])))
        g.add((sn, DX.aboutResource, sem.iri(it["resource"])))
        for hid in hyps:
            if explained[hid].get(it["id"]):
                g.add((sn, DX.explainedBy, CASE[hid]))
    for v in frame["services"]:
        if v["state"] == "down":
            vn = CASE[v["id"]]
            g.add((vn, DX.symptomKind, Literal("service-down")))
            g.add((vn, DX.aboutResource, sem.iri(v["service"])))
    for pid in dominant_ppe_items(frame, ppe_read):
        x = next(p for p in frame["ppe"] if p["id"] == pid)
        pn = CASE[pid]
        g.add((pn, RDF.type, DX.FrameSymptom))
        g.add((pn, DX.symptomKind, Literal("ppe")))
        g.add((pn, DX.ppeKind, Literal(x["kind"])))
        g.add((pn, DX.relevant, Literal(False)))
    _ = T
    return g


def rule_terms(sem, frame, hyps, states, explained, ppe_read) -> dict[str, dict]:
    if not hyps:
        return {}
    g = case_graph(sem, frame, hyps, states, explained, ppe_read)
    contradicted = sem.ask_rules(g, {hid: CASE[hid] for hid in hyps})
    out = {}
    for hid, rules in contradicted.items():
        alarm_rules = [r for r in rules if r not in PPE_RULES]
        ppe_rules = [r for r in rules if r in PPE_RULES]
        out[hid] = {"gc": -RULE_NATS * min(RULE_MAX, len(alarm_rules)), "gc_rules": alarm_rules,
                    "gc_ppe": -RULE_NATS * min(RULE_MAX, len(ppe_rules)), "gc_ppe_rules": ppe_rules}
    return out


# ====================================================================== checks
def predicted_field_answer(sem, check_id: str, target: str, hyp: dict) -> str | None:
    """Answer class a hypothesis predicts for a field check (deterministic)."""
    mode, loc = hyp["mode"], hyp["location"]
    if check_id == "CHK_OTDR":
        if mode == "FIBRE_CUT" and loc == target:
            return "break"
        if mode == "SECTION_CUT" and sem.value(target, "partOfSection") == loc:
            return "break"
        if mode == "SPAN_LOSS_INCREASE" and loc == target:
            return "loss"
        if mode == "CONNECTOR_DIRTY" and loc == target:
            return "reflective"
        return "none"
    if check_id == "CHK_CONNECTOR_INSPECT":
        return "dirty" if (mode == "CONNECTOR_DIRTY" and loc == target) else "clean"
    if check_id == "CHK_BOARD_RESEAT":
        return "recovers" if (mode == "BOARD_POWER_LOSS" and loc == target) else "no-change"
    if check_id == "CHK_LOOPBACK":
        return "inside" if (mode in ("RX_POWER_DRIFT", "CONFIG_MISMATCH") and loc == target) else "outside"
    if check_id == "CHK_FAN_INSPECT":
        return "failed" if (mode == "FAN_FAILURE" and loc == target) else "ok"
    return None


def field_answer_class(check_id: str, answer: dict) -> str:
    for k in ("event", "connector", "board", "fault", "fan"):
        if k in answer:
            return str(answer[k])
    return "unknown"


def check_terms(sem, hyps: dict[str, dict], field_obs: list[dict]) -> list[dict]:
    terms = []
    for ob in field_obs:
        cid, tgt = ob["check_id"], ob["target"]
        observed = field_answer_class(cid, ob["answer"])
        vals = {}
        for hid, h in hyps.items():
            pred = predicted_field_answer(sem, cid, tgt, h)
            vals[hid] = 0.0 if (pred is None or pred == observed) else -8.0
        terms.append({"name": f"T_check:{cid}({tgt})", "tau": sem.checks()[cid]["tau"], "values": vals,
                      "nota": "neutral", "observed": observed})
    return terms
