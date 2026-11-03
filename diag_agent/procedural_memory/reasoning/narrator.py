"""Narrator and orchestrator (doc 04 §8.2, §11).

Both LLM roles are optional and validated: the narrator's text may only use
numbers and resource ids present in its slots (otherwise the deterministic
template is used); the orchestrator may only pick one of the advisor's
admissible candidates (otherwise the advisor's first choice stands).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..llm.cache import CassetteMiss
from ..llm.provider import LLMUnavailable, parse_json

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
_NUM = re.compile(r"(?<![A-Za-z_])-?\d+(?:\.\d+)?")
_ID = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:[._][A-Za-z0-9]+)+\b")


def position_text(fit: dict | None) -> str:
    if not fit or fit.get("position_km") is None:
        return ""
    return f" (expected {fit['position_km']:.1f} +/- {fit['tol_km']:.1f} km from {fit['from_ne']})"


def template_detail(slots: dict) -> str:
    d = slots["decision"]
    if d == "committed":
        loc = slots["location"]
        ends = f" ({slots['ends']})" if slots.get("ends") else ""
        pos = ""
        if slots.get("fit") and slots["fit"].get("position_km") is not None:
            f = slots["fit"]
            pos = f", {f['position_km']:.1f} +/- {f['tol_km']:.1f} km from {f['from_ne']}"
        txt = (f"{slots['label']} on {loc}{ends}{pos}. Probability {slots['p']}, none-of-these {slots['nota']}.")
        if slots.get("evidence"):
            txt += " Decisive evidence: " + "; ".join(slots["evidence"]) + "."
        return txt
    if d == "set-reported":
        members = ", ".join(f"{m['mode']} at {m['location']} ({m['p']})" for m in slots.get("set", []))
        return (f"No single cause reached the commit threshold. The 90% set is {members}; "
                f"none-of-these {slots['nota']}." + (f" Suggested next check: {slots['next_check']}."
                                                       if slots.get("next_check") else ""))
    if d == "probably-not-considered":
        return (f"The observations are not explained by any documented fault mode (none-of-these {slots['nota']}). "
                f"Best documented candidate: {slots.get('mode', '-')} at {slots.get('location', '-')} ({slots.get('p', '-')}).")
    if d == "limit-reached":
        return (f"Check budget exhausted before commitment. Leader {slots.get('mode', '-')} at {slots.get('location', '-')} "
                f"({slots.get('p', '-')}), none-of-these {slots['nota']}.")
    return f"Diagnosis in progress: {slots.get('summary', '')}"


def raise_detail(frame: dict, task_id: str) -> str:
    anchor = next((it for it in frame["items"] if it["id"] == frame.get("anchor")), None)
    down = [v["service"] for v in frame["services"] if v["state"] == "down"]
    risk = [v["service"] for v in frame["services"] if v["state"] in ("at-risk", "degraded")]
    parts = []
    if anchor:
        parts.append(f"{anchor['condition']} ({anchor['layer']}) at {anchor['resource']}"
                     + (f" on {anchor['containers'][0]}" if anchor["containers"] else ""))
    parts.append(f"{len(frame['items'])} frame items from {frame['n_alarms']} alarms")
    txt = "; ".join(parts) + "."
    if down:
        txt += f" Service {', '.join(down)} down."
    if risk:
        txt += f" At risk or degraded: {', '.join(risk)}."
    return txt + f" Diagnosis in progress (task {task_id})."


def _allowed(slots: dict) -> tuple[set[str], set[str]]:
    blob = json.dumps(slots, ensure_ascii=False)
    nums = set(_NUM.findall(blob))
    ids = set(_ID.findall(blob))
    return nums, ids


def validate_text(text: str, slots: dict) -> tuple[bool, str]:
    nums, ids = _allowed(slots)
    num_vals = set()
    for n in nums:
        try:
            num_vals.add(round(float(n), 6))
        except ValueError:
            pass
    for n in _NUM.findall(text):
        if n not in nums and round(float(n), 6) not in num_vals:
            return False, f"number {n} not in slots"
    for i in _ID.findall(text):
        if i not in ids and not re.fullmatch(r"[A-Za-z]+\.[A-Za-z]*", i):
            return False, f"id {i} not in slots"
    for k in _slot_keys(slots):
        if "_" in k and re.search(rf"\b{re.escape(k)}\b", text):
            return False, f"slot name {k} in text"
    return True, ""


def _slot_keys(obj) -> set[str]:
    out: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(str(k))
            out |= _slot_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= _slot_keys(v)
    return out


def narrate(llm, slots: dict, *, enabled: bool = True) -> dict[str, Any]:
    tmpl = template_detail(slots)
    if not enabled or llm is None or not llm.available("narrator"):
        return {"detail": tmpl, "source": "template"}
    prompt = (PROMPTS / "narrator.v1.md").read_text().replace("{slots}", json.dumps(slots, ensure_ascii=False, indent=1))
    try:
        resp = llm.chat("narrator", [{"role": "user", "content": prompt}])
    except (LLMUnavailable, CassetteMiss) as e:
        return {"detail": tmpl, "source": "template", "llm_error": str(e)[:200]}
    text = " ".join((resp.get("content") or "").split())
    ok, why = validate_text(text, slots)
    if ok and text:
        return {"detail": text, "source": "llm", "usage": resp.get("usage")}
    return {"detail": tmpl, "source": "template", "rejected": text[:400], "why": why}


def orchestrate(llm, ranked: list[dict], *, enabled: bool = True) -> dict[str, Any]:
    adm = [c for c in ranked if c.get("admissible")][:3]
    if not adm:
        return {"index": None, "source": "advisor"}
    if not enabled or llm is None or not llm.available("orchestrator") or len(adm) == 1:
        return {"index": 0, "source": "advisor"}
    cands = [{"index": i, "check": c["check_id"], "target": c["target"], "bits": round(c["bits"], 4),
              "effort_h": round(c["effort_h"], 4), "bits_per_hour": round(c["rate"], 2), "remote": c["remote"]}
             for i, c in enumerate(adm)]
    prompt = (PROMPTS / "orchestrator.v1.md").read_text().replace("{candidates}", json.dumps(cands, indent=1))
    try:
        resp = llm.chat("orchestrator", [{"role": "user", "content": prompt}], json_mode=True)
        out = parse_json(resp.get("content") or "")
        idx = int(out.get("choice"))
        if 0 <= idx < len(adm):
            return {"index": idx, "source": "llm", "rationale": str(out.get("rationale", ""))[:300]}
        return {"index": 0, "source": "advisor", "rejected": f"choice {idx} out of range"}
    except (LLMUnavailable, CassetteMiss, ValueError, TypeError) as e:
        return {"index": 0, "source": "advisor", "llm_error": str(e)[:200]}
