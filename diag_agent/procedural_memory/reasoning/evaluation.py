"""Evaluate one hypothesis against the frame: predictions, window states,
explained items and the preview score used by the generation lines."""

from __future__ import annotations

from typing import Any

from .propagator import propagate
from .windows import Explainer, evaluate, index_alarms


class Evaluator:
    def __init__(self, sem, *, tol_km: float = 0.4, clean_slope_max: float = 0.08):
        self.sem = sem
        self.explainer = Explainer(sem)
        self.tol_km = tol_km
        self.clean_slope_max = clean_slope_max
        self._prop: dict[tuple[str, str], dict] = {}

    def propagation(self, hid: str, mode: str, location: str) -> dict:
        key = (mode, location)
        if key not in self._prop:
            self._prop[key] = propagate(self.sem, "H?", mode, location)
        base = self._prop[key]
        preds = []
        for p in base["predictions"]:
            q = dict(p)
            q["id"] = hid + "." + p["id"].split(".", 1)[1]
            q["hyp"] = hid
            preds.append(q)
        return {**base, "predictions": preds}

    def context(self, frame: dict, alarms: list[dict], ppe_read: dict, clock: float, audits: list[dict]) -> dict:
        return {"frame": frame, "by_res": index_alarms(alarms), "ppe_read": ppe_read, "clock": clock,
                "audits": audits}

    def evaluate(self, hid: str, mode: str, location: str, ctx: dict) -> dict[str, Any]:
        prop = self.propagation(hid, mode, location)
        hyp = {"id": hid, "mode": mode, "location": location}
        states = evaluate(self.sem, hyp, prop, ctx["frame"], ctx["by_res"], ctx["ppe_read"], ctx["clock"],
                          ctx["audits"], tol_km=self.tol_km, clean_slope_max=self.clean_slope_max)
        explained = self.explainer.explained(ctx["frame"], prop, states)
        return {"preds": {p["id"]: p for p in prop["predictions"]}, "states": states, "explained": explained,
                "keyword_resources": prop["keyword_resources"], "dead": prop["dead"]}

    def preview(self, mode: str, location: str, ctx: dict) -> dict[str, Any]:
        ev = self.evaluate("Hx", mode, location, ctx)
        frame = ctx["frame"]
        rel = [it["id"] for it in frame["items"] if it["relevant"]]
        explained = [i for i in rel if ev["explained"].get(i)]
        svc = [v["id"] for v in frame["services"] if v["state"] == "down" and ev["explained"].get(v["id"])]
        ppe_ok = [x["id"] for x in frame.get("ppe", []) if ev["explained"].get(x["id"])]
        st = list(ev["states"].values())
        miss_req = [s for s in st if s["state"] in ("MISSING", "CONTRADICTED") and s["strength"] == "Required"
                    and s["layer"] != "PPE"]
        miss_exp = [s for s in st if s["state"] in ("MISSING", "CONTRADICTED") and s["strength"] == "Expected"
                    and s["layer"] != "PPE" and s["condition"] not in ("ServiceDown", "ServiceDegraded")]
        ppe_bad = [s for s in st if s["layer"] == "PPE" and s["state"] == "CONTRADICTED"]
        pending = [s for s in st if s["state"] == "PENDING" and s["strength"] != "Possible"]
        score = len(explained) + len(svc) + len(ppe_ok) - 2 * len(miss_req) - 0.6 * len(miss_exp) - 2 * len(ppe_bad)
        reason = ""
        if score <= 0:
            if miss_req:
                s = miss_req[0]
                reason = f"{s['condition']} @ {s['resource']} {s['state'].lower()}"
            elif ppe_bad:
                reason = f"PPE {ppe_bad[0]['reason']}"
            elif not explained:
                reason = "explains no symptom"
            else:
                reason = "net score <= 0"
        return {"mode": mode, "location": location, "score": round(score, 3), "explained": explained + svc + ppe_ok,
                "unexplained": [i for i in rel if i not in explained],
                "missing": [f"{s['condition']}@{s['resource']}" for s in miss_req + miss_exp],
                "pending": [f"{s['condition']}@{s['resource']}" for s in pending],
                "contradicted_ppe": [s["reason"] for s in ppe_bad], "reason": reason,
                "n_relevant": len(rel)}
