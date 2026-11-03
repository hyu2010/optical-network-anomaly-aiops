"""Windowed evaluation of predictions (doc 04 §6.2) and PPE trace classes.

A prediction p with physical delay d has the observation window
    W = [t0_lo + d,  t0_hi + d + cadence(layer) + 2 s]
where [t0_lo, t0_hi] is the onset interval.  States:
    MATCHED       an alarm at the resource with condition below p's was observed
    PENDING       not seen yet and the window is still open
    MISSING       window closed (or an audit resynchronised the NE), not seen, instrument alive
    CONTRADICTED  as MISSING, and the instrument reported a different condition there
    UNOBSERVABLE  instrument predicted dead, or PPE not read / no trace available
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .framer import cadence_s, resource_of
from .propagator import instrument_alive

PPE_KIND = {"ProfileStep": "step", "ProfileSlope": "slope", "RxOffset": "rx_offset", "ProfileNull": "null",
            "ProfileClean": "clean"}
HARD_STEP_DB = 24.0
MIN_STEP_DB = 0.5
GRACE_S = 2.0


# ====================================================================== PPE
def classify_trace(trace: dict | None, *, clean_slope_max: float = 0.08) -> dict:
    """Reduce a detector trace to its dominant answer (doc 04 §8.3, CHK_PPE_PROFILE)."""
    if trace is None:
        return {"class": "unavailable"}
    anoms = trace.get("anomalies") or []
    steps = [a for a in anoms if a["kind"] == "step" and a.get("magnitude_db") is not None
             and abs(a["magnitude_db"]) >= MIN_STEP_DB]
    if steps:
        a = max(steps, key=lambda a: (abs(a["magnitude_db"]), a.get("confidence") or 0))
        return {"class": "step", "band": "hard" if abs(a["magnitude_db"]) >= HARD_STEP_DB else "soft",
                "span": a.get("span_id") or "", "offset_km": a.get("offset_in_span_km"),
                "magnitude_db": a["magnitude_db"], "z_km": a.get("z_km"), "confidence": a.get("confidence")}
    rx = [a for a in anoms if a["kind"] == "rx_offset"]
    if rx:
        a = max(rx, key=lambda a: a.get("confidence") or 0)
        return {"class": "rx_offset", "magnitude_db": a.get("magnitude_db"), "span": a.get("span_id") or "",
                "confidence": a.get("confidence")}
    slopes = [a for a in anoms if a["kind"] == "slope" and (a.get("magnitude_db") or 0) > clean_slope_max]
    if slopes:
        a = max(slopes, key=lambda a: a["magnitude_db"])
        return {"class": "slope", "span": a.get("span_id") or "", "offset_km": a.get("offset_in_span_km"),
                "magnitude_db": a["magnitude_db"], "confidence": a.get("confidence")}
    if trace.get("degraded") or any(a["kind"] in ("null", "no_signal") for a in anoms):
        return {"class": "null"}
    return {"class": "clean"}


def answer_key(cls: dict) -> str:
    c = cls["class"]
    if c == "step":
        return f"step-{cls['band']}@{cls['span']}"
    if c == "slope":
        return f"slope@{cls['span']}"
    return c


def predicted_ppe_key(pred: dict) -> str:
    kind = PPE_KIND.get(pred["condition"], "clean")
    sig = pred.get("signature") or {}
    spans = pred.get("spans") or []
    if kind == "step":
        mx = sig.get("max_db")
        band = "hard" if (mx is not None and mx <= -HARD_STEP_DB) else "soft"
        return f"step-{band}@{spans[0] if spans else '?'}"
    if kind == "slope":
        return f"slope@{spans[0] if spans else '?'}"
    return kind


def match_ppe(pred: dict, cls: dict, *, tol_km: float = 0.4) -> tuple[str, str]:
    if cls["class"] == "unavailable":
        return "UNOBSERVABLE", "no PPE trace available"
    want = PPE_KIND.get(pred["condition"], "clean")
    sig = pred.get("signature") or {}
    spans = pred.get("spans") or []
    if cls["class"] != want:
        return "CONTRADICTED", f"predicted {want}, observed {cls['class']}"
    if want in ("step", "slope"):
        if spans and cls.get("span") not in spans:
            return "CONTRADICTED", f"{want} in {cls.get('span')}, predicted in {', '.join(spans)}"
        mag = cls.get("magnitude_db")
        slack = 1.0 if want == "step" else 0.02
        lo, hi = sig.get("min_db"), sig.get("max_db")
        if mag is not None and ((lo is not None and mag < lo - slack) or (hi is not None and mag > hi + slack)):
            return "CONTRADICTED", f"magnitude {mag} outside [{lo}, {hi}]"
        if want == "step" and sig.get("position") == "span-start" and (cls.get("offset_km") or 0) > max(2 * tol_km, 1.0):
            return "CONTRADICTED", f"step {cls.get('offset_km')} km into the span, predicted at the span start"
    return "MATCHED", ""


# ====================================================================== windows
def index_alarms(alarms: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for a in alarms:
        out[resource_of(a)].append(a)
    return out


def evaluate(sem, hyp: dict, prop: dict, frame: dict, alarms_by_res: dict[str, list[dict]],
             ppe_read: dict[str, dict | None], clock: float, audits: list[dict],
             *, tol_km: float = 0.4, clean_slope_max: float = 0.08) -> dict[str, dict]:
    onset_lo, onset_hi = frame["onset"]
    dead = set(prop["dead"])
    item_of_event = {e: it["id"] for it in frame["items"] for e in it["events"]}
    svc_state = {v["service"]: v for v in frame["services"]}
    ppe_by_och = defaultdict(list)
    for p in frame.get("ppe", []):
        ppe_by_och[p["och"]].append(p)
    out: dict[str, dict] = {}
    for p in prop["predictions"]:
        st: dict[str, Any] = {"pid": p["id"], "hyp": hyp["id"], "condition": p["condition"], "resource": p["resource"],
                              "layer": p["layer"], "strength": p["strength"], "matched_by": [], "items": [],
                              "window": None, "closes_at": None, "reason": ""}
        if p["ppe"]:
            if p["resource"] not in ppe_read:
                st.update(state="UNOBSERVABLE", reason="PPE not read", checkable=True)
            else:
                cls = classify_trace(ppe_read[p["resource"]], clean_slope_max=clean_slope_max)
                state, why = match_ppe(p, cls, tol_km=tol_km)
                st.update(state=state, reason=why, observed=cls, checkable=False)
                if state == "MATCHED":
                    tr = ppe_read[p["resource"]]
                    st["matched_by"] = [tr["event_id"]] if tr else []
                    st["items"] = [x["id"] for x in ppe_by_och.get(p["resource"], [])
                                   if cls["class"] not in ("step", "slope") or
                                   (x["kind"] == cls["class"] and x.get("span") == cls.get("span"))]
            out[p["id"]] = st
            continue

        cad = cadence_s(sem, p["layer"]) if not p["service"] else cadence_s(sem, "ODU")
        d = p["delay_ms"] / 1000.0
        lo, hi = onset_lo + d, onset_hi + d + cad + GRACE_S
        closes = hi
        ne = sem.hosting_ne(p["resource"]) if not p["service"] else None
        for au in audits:
            if au["t"] >= lo and ((au.get("ne") and au["ne"] == ne) or (au.get("port") and au["port"] == p["resource"])):
                closes = min(closes, au["t"])
        st.update(window=[round(lo, 3), round(hi, 3)], closes_at=round(closes, 3))

        if p["service"]:
            v = svc_state.get(p["resource"])
            ok = v is not None and (v["state"] == "down" if p["condition"] == "ServiceDown"
                                    else v["state"] in ("down", "degraded"))
            if ok:
                st.update(state="MATCHED", matched_by=[v["id"]], items=[v["id"]])
            elif clock >= closes:
                st.update(state="MISSING", reason="service not reported hurt")
            else:
                st.update(state="PENDING")
            out[p["id"]] = st
            continue

        hits = [a for a in alarms_by_res.get(p["resource"], [])
                if a["t_obs"] <= clock + 1e-9 and sem.cond_subsumes(p["condition"], sem.alarm_type(a["name"])["condition"])]
        if hits:
            st.update(state="MATCHED", matched_by=[a["event_id"] for a in hits],
                      items=sorted({item_of_event[a["event_id"]] for a in hits if a["event_id"] in item_of_event}))
        elif not instrument_alive(sem, p["resource"], dead):
            st.update(state="UNOBSERVABLE", reason="instrument predicted dead")
        elif clock >= closes:
            other = [it for it in frame["items"] if it["resource"] == p["resource"] and it["relevant"]]
            if other:
                st.update(state="CONTRADICTED", reason=f"{p['resource']} reported {other[0]['condition']} instead")
            else:
                st.update(state="MISSING", reason=f"{p['resource']} quiet")
        else:
            st.update(state="PENDING")
        out[p["id"]] = st
    return out


# ====================================================================== explanation
class Explainer:
    """Decides which frame items a hypothesis explains (doc 04 §6.3)."""

    def __init__(self, sem):
        self.sem = sem
        self._desc_of_cond: dict[str, set[str]] = {}

    def _descendants_of_condition(self, cond: str) -> set[str]:
        if cond not in self._desc_of_cond:
            out: set[str] = set()
            for n in self.sem.alarm_types_for(cond):
                out |= self.sem.descendants(n)
            self._desc_of_cond[cond] = out
        return self._desc_of_cond[cond]

    def explained(self, frame: dict, prop: dict, states: dict[str, dict]) -> dict[str, bool]:
        sem = self.sem
        pred_at: dict[str, set[str]] = defaultdict(set)
        for p in prop["predictions"]:
            if not p["ppe"] and not p["service"]:
                pred_at[p["resource"]].add(p["condition"])
        kw_res = set(prop["keyword_resources"])
        kws = set(prop["keywords"])
        out: dict[str, bool] = {}
        for it in frame["items"]:
            conds = pred_at.get(it["resource"], set())
            ok = True
            for name in it["root_names"]:
                at = sem.alarm_type(name)
                if any(sem.cond_subsumes(c, at["condition"]) for c in conds):
                    continue
                if any(name in self._descendants_of_condition(c) for c in conds):
                    continue
                if it["resource"] in kw_res and kws & set(at["keywords"]):
                    continue
                ok = False
                break
            out[it["id"]] = ok
        for v in frame["services"]:
            out[v["id"]] = any(s["state"] == "MATCHED" and v["id"] in s["items"] for s in states.values())
        for x in frame.get("ppe", []):
            out[x["id"]] = any(s["state"] == "MATCHED" and x["id"] in s["items"] for s in states.values())
        return out
