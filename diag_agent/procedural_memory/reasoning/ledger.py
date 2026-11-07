"""Bayesian ledger and decision rule (doc 04 §7).

The ledger is the bookkeeping half of winnowing. Generation, tools and
checks supply candidates and likelihood terms; only the ledger writes a
posterior or commits.

    log P(h | E) = log P0(h) + sum_t tau_t * clip(T_t(h))  (normalised over H + NOTA)

Each term is clipped at `clip_nats` below its best value, so no single source
of evidence can drive a hypothesis to zero.
"""

from __future__ import annotations

import math
from typing import Any

NOTA = "NOTA"


def entropy_bits(p: dict[str, float]) -> float:
    return -sum(v * math.log2(v) for v in p.values() if v > 0)


def synthesize(prior: dict[str, float], terms: list[dict], *, clip_nats: float = 8.0) -> dict[str, Any]:
    hyps = [h for h in prior if h != NOTA]
    log = {h: math.log(max(prior[h], 1e-300)) for h in prior}
    applied = []
    for t in terms:
        vals = {h: float(t["values"].get(h, 0.0)) for h in hyps}
        if t.get("nota") == "neutral":
            z = sum(prior[h] for h in hyps) or 1.0
            nota_v = sum(prior[h] * vals[h] for h in hyps) / z
        else:
            nota_v = float(t.get("nota", 0.0))
        best = max(list(vals.values()) + [nota_v]) if vals else nota_v
        floor = best - clip_nats
        vals = {h: max(v, floor) for h, v in vals.items()}
        nota_v = max(nota_v, floor)
        for h in hyps:
            log[h] += t["tau"] * vals[h]
        log[NOTA] += t["tau"] * nota_v
        applied.append({"name": t["name"], "tau": t["tau"], "values": {h: round(v, 4) for h, v in vals.items()},
                        "nota": round(nota_v, 4), "nota_mode": "neutral" if t.get("nota") == "neutral" else "charged",
                        **({"rules": t["rules"]} if "rules" in t else {})})
    m = max(log.values())
    w = {h: math.exp(v - m) for h, v in log.items()}
    z = sum(w.values())
    post = {h: w[h] / z for h in log}
    ranked = sorted(hyps, key=lambda h: -post[h])
    return {"prior": dict(prior), "terms": applied, "post": post, "log_post": log,
            "ranked": ranked, "leader": ranked[0] if ranked else None,
            "runner_up": ranked[1] if len(ranked) > 1 else None,
            "nota": post[NOTA], "entropy": entropy_bits(post), "entropy_prior": entropy_bits(prior)}


def leader_debits(ledger: dict, evidence: dict[str, dict], hid: str) -> list[dict]:
    ev = evidence.get(hid, {})
    out = list(ev.get("doc_debits", [])) + list(ev.get("ppe_debits", []))
    for r in ev.get("gc_rules", []) + ev.get("gc_ppe_rules", []):
        out.append({"what": f"rule {r} contradicted", "nats": -4.0})
    for t in ledger["terms"]:
        if t["name"].startswith("T_check") and t["values"].get(hid, 0) < 0:
            out.append({"what": f"{t['name']} answer contradicts", "nats": t["values"][hid]})
    return out


def decide(ledger: dict, evidence: dict[str, dict], cfg: dict, budget: dict, history: list[dict],
           *, pass_no: int, confirm_check: dict | None) -> tuple[str, dict]:
    """Return (decision, info); decision in commit | continue | regenerate | idk | limit."""
    lc = cfg["ledger"]
    ac = cfg["advisor"]
    leader = ledger["leader"]
    p = ledger["post"].get(leader, 0.0) if leader else 0.0
    nota = ledger["nota"]
    lead_ev = evidence.get(leader, {}) if leader else {}
    pending_req = [s for s in lead_ev.get("states", {}).values()
                   if s["state"] == "PENDING" and s["strength"] == "Required"]
    debits = leader_debits(ledger, evidence, leader) if leader else []
    worst = min((d["nats"] for d in debits), default=0.0)
    checks = {
        "p_leader": {"ok": p >= lc["commit_p"], "value": round(p, 5), "need": f">= {lc['commit_p']}"},
        "nota": {"ok": nota < lc["nota_max"], "value": round(nota, 5), "need": f"< {lc['nota_max']}"},
        "evaluated": {"ok": not pending_req, "value": [s["pid"] for s in pending_req],
                      "need": "leader has no pending Required prediction"},
        "confirmed": {"ok": confirm_check is None,
                      "value": None if confirm_check is None else f"{confirm_check['check_id']}({confirm_check['target']})",
                      "need": f"Required predictions testable remotely within {lc['confirm_remote_h']} h are tested"},
        "residual": {"ok": -worst <= lc["residual_nats"], "value": round(-worst, 3),
                     "need": f"largest single debit <= {lc['residual_nats']} nats"},
    }
    info: dict[str, Any] = {"leader": leader, "p": p, "nota": nota, "tests": checks, "pass_no": pass_no}
    if leader and all(c["ok"] for c in checks.values()):
        return "commit", info

    stalled = False
    k = lc["stall_terms"]
    if len(history) > k and all(h.get("after_check") for h in history[-k:]):
        stalled = (history[-k - 1]["entropy"] - history[-1]["entropy"]) < lc["stall_bits"]
    info["stalled"] = stalled
    need_regen = (nota > lc["regenerate_nota"]) or stalled or not checks["residual"]["ok"] or leader is None
    info["regen_reasons"] = [r for r, c in (("nota", nota > lc["regenerate_nota"]), ("stall", stalled),
                                            ("residual", not checks["residual"]["ok"]),
                                            ("no hypotheses", leader is None)) if c]
    if need_regen:
        if budget.get("returns", 0) < ac["max_returns"]:
            return "regenerate", info
        return "idk", info
    if budget.get("checks", 0) >= ac["max_checks"] or budget.get("effort_h", 0.0) >= ac["max_effort_h"]:
        return "limit", info
    return "continue", info
