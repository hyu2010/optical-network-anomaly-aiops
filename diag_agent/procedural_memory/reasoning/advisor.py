"""Check advisor (doc 04 §8): answer-class mutual information per hour of effort.

For each admissible check the advisor predicts, for every hypothesis, the
answer class the check would return.  Hypotheses that make no prediction
(and NOTA) are spread uniformly over the classes.  Rate = I(H; A) / effort.
Field checks of >= gate_hours must be able to decide: some answer with
probability >= gate_p has to lead to a commit.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from .ledger import NOTA
from .terms import predicted_field_answer
from .windows import predicted_ppe_key

MIN_P = 1e-3
MIN_BITS = 0.01


def _site(sem, check_id: str, target: str) -> str | None:
    k = sem.kind_of(target)
    if k in ("span", "otu"):
        return sem.value(target, "sourceNE")
    if k == "section":
        spans = sem.subjects_with("partOfSection", target)
        return sem.value(spans[0], "sourceNE") if spans else None
    return sem.hosting_ne(target)


def effort_h(sem, cfg: dict, check_id: str, target: str, budget: dict, wait_s: float | None = None) -> float:
    chk = sem.checks()[check_id]
    if check_id == "CHK_WAIT":
        return max(wait_s or 0.0, 1.0) / 3600.0
    h = chk["minutes"] / 60.0
    if not chk["remote"]:
        site = _site(sem, check_id, target)
        if site and site not in budget.get("sites", []):
            h += cfg["advisor"]["site_hours"]
    if chk["interrupting"]:
        h += cfg["advisor"]["interrupt_hours"]
    return h


OPEN_CLASSES = {
    "CHK_PPE_PROFILE": ["clean", "other"],
    "CHK_OTDR": ["break", "loss", "reflective", "none"],
    "CHK_CONNECTOR_INSPECT": ["dirty", "clean"],
    "CHK_BOARD_RESEAT": ["recovers", "no-change"],
    "CHK_LOOPBACK": ["inside", "outside"],
    "CHK_FAN_INSPECT": ["failed", "ok"],
}


def mutual_information(post: dict[str, float], answers: dict[str, str | None],
                       extra: list[str] | None = None) -> tuple[float, dict[str, float], list[str]]:
    classes = sorted({a for a in answers.values() if a is not None} | set(extra or []))
    if not classes:
        return 0.0, {}, []
    k = len(classes)
    pa: dict[str, float] = defaultdict(float)
    h_cond = 0.0
    for h, p in post.items():
        a = answers.get(h) if h != NOTA else None
        if a is None:
            for c in classes:
                pa[c] += p / k
            h_cond += p * math.log2(k) if k > 1 else 0.0
        else:
            pa[a] += p
    h_a = -sum(v * math.log2(v) for v in pa.values() if v > 0)
    return max(0.0, h_a - h_cond), dict(pa), classes


def conditional(post: dict[str, float], answers: dict[str, str | None], cls: str, k: int) -> dict[str, float]:
    w = {}
    for h, p in post.items():
        a = answers.get(h) if h != NOTA else None
        w[h] = p * (1.0 if a == cls else 0.0) if a is not None else p / max(k, 1)
    z = sum(w.values()) or 1.0
    return {h: v / z for h, v in w.items()}


def _would_commit(p: dict[str, float], cfg: dict) -> tuple[bool, str | None]:
    lead = max((h for h in p if h != NOTA), key=lambda h: p[h], default=None)
    if lead is None:
        return False, None
    return (p[lead] >= cfg["ledger"]["commit_p"] and p.get(NOTA, 0) < cfg["ledger"]["nota_max"]), lead


class Advisor:
    def __init__(self, sem, cfg: dict):
        self.sem = sem
        self.cfg = cfg

    # ------------------------------------------------------------------ candidates
    def candidates(self, ctx: dict, post: dict[str, float]) -> list[dict]:
        sem, cfg = self.sem, self.cfg
        hyps, evidence, clock = ctx["hyps"], ctx["evidence"], ctx["clock"]
        done = set(ctx.get("done", []))
        live = [h for h in hyps if post.get(h, 0) >= MIN_P]
        out: list[dict] = []

        # PPE profiles
        ochs: dict[str, dict[str, str | None]] = defaultdict(dict)
        for h in live:
            for s in evidence[h]["states"].values():
                if s["layer"] == "PPE" and s["state"] == "UNOBSERVABLE" and s.get("checkable", False):
                    pred = evidence[h]["preds"][s["pid"]]
                    ochs[s["resource"]].setdefault(h, predicted_ppe_key(pred))
        for och, ans in sorted(ochs.items()):
            if f"CHK_PPE_PROFILE({och})" in done:
                continue
            answers = {h: ans.get(h) for h in hyps}
            out.append(self._score("CHK_PPE_PROFILE", och, answers, post, ctx))

        # windows: wait / audit
        pend: dict[str, list[tuple[str, dict]]] = defaultdict(list)
        for h in live:
            for s in evidence[h]["states"].values():
                if s["state"] == "PENDING" and s["strength"] in ("Required", "Expected") and s["layer"] != "PPE":
                    pend[h].append((f"{s['condition']}@{s['resource']}", s))
        all_pending = [s for lst in pend.values() for _, s in lst]
        if all_pending:
            times = sorted({round(s["closes_at"], 1) for s in all_pending
                            if s["closes_at"] and s["closes_at"] > clock + 1.0})
            for t_w in times[:3]:
                answers = {}
                for h in hyps:
                    keys = sorted(k for k, s in pend.get(h, []) if s["closes_at"] <= t_w + 1e-6)
                    answers[h] = "seen:" + ",".join(keys) if keys else "nothing"
                c = self._score("CHK_WAIT", f"+{t_w:.1f}s", answers, post, ctx, wait_s=t_w - clock)
                c["until"] = t_w
                out.append(c)
            nes = sorted({sem.hosting_ne(s["resource"]) for s in all_pending
                          if sem.kind_of(s["resource"]) in ("port", "board", "ne")} - {None})
            for ne in nes:
                if f"CHK_ALARM_AUDIT({ne})" in done:
                    continue
                answers = {}
                for h in hyps:
                    keys = sorted(k for k, s in pend.get(h, []) if sem.hosting_ne(s["resource"]) == ne)
                    answers[h] = "seen:" + ",".join(keys) if keys else "nothing"
                out.append(self._score("CHK_ALARM_AUDIT", ne, answers, post, ctx))

        # field checks
        targets: dict[str, set[str]] = defaultdict(set)
        for h in live:
            m, loc = hyps[h]["mode"], hyps[h]["location"]
            k = sem.kind_of(loc)
            if k == "span":
                targets["CHK_OTDR"].add(loc)
                targets["CHK_CONNECTOR_INSPECT"].add(loc)
            elif k == "section":
                for s in sem.subjects_with("partOfSection", loc):
                    targets["CHK_OTDR"].add(s)
            elif k == "board":
                targets["CHK_BOARD_RESEAT"].add(loc)
            elif k == "port":
                targets["CHK_LOOPBACK"].add(loc)
            elif k == "ne" and m == "FAN_FAILURE":
                targets["CHK_FAN_INSPECT"].add(loc)
        for cid, tset in targets.items():
            for tgt in sorted(tset):
                if f"{cid}({tgt})" in done:
                    continue
                answers = {h: predicted_field_answer(sem, cid, tgt, hyps[h]) for h in hyps}
                out.append(self._score(cid, tgt, answers, post, ctx))

        out = [c for c in out if c["bits"] >= MIN_BITS]
        for c in out:
            c["admissible"], c["why"] = self._admissible(c, post)
        out.sort(key=lambda c: (not c["admissible"], -c["rate"], -c["bits"], c["check_id"], c["target"]))
        return out

    def _score(self, cid: str, target: str, answers: dict, post: dict, ctx: dict, wait_s: float | None = None) -> dict:
        bits, pa, classes = mutual_information(post, answers, OPEN_CLASSES.get(cid))
        eff = effort_h(self.sem, self.cfg, cid, target, ctx["budget"], wait_s)
        return {"check_id": cid, "target": target, "bits": bits, "effort_h": eff, "rate": bits / eff if eff else 0.0,
                "answers": answers, "p_answer": pa, "classes": classes, "remote": self.sem.checks()[cid]["remote"]}

    def _admissible(self, c: dict, post: dict) -> tuple[bool, str]:
        ac = self.cfg["advisor"]
        if c["effort_h"] >= ac["gate_hours"]:
            k = len(c["classes"])
            for cls in c["classes"]:
                if c["p_answer"].get(cls, 0) >= ac["gate_p"]:
                    ok, _ = _would_commit(conditional(post, c["answers"], cls, k), self.cfg)
                    if ok:
                        return True, f"answer '{cls}' (p {c['p_answer'][cls]:.3f}) would decide"
            return False, f"no answer with p >= {ac['gate_p']} would decide"
        return True, ""

    # ------------------------------------------------------------------ confirmation rule
    def confirm_check(self, ctx: dict, leader: str) -> dict | None:
        """A Required prediction of the leader testable by a remote check <= confirm_remote_h."""
        limit = self.cfg["ledger"]["confirm_remote_h"]
        ev = ctx["evidence"][leader]
        done = set(ctx.get("done", []))
        groups: dict[str, list[dict]] = defaultdict(list)
        for s in ev["states"].values():
            if s["layer"] == "PPE" and s["strength"] == "Required":
                groups[ev["preds"][s["pid"]].get("rule") or s["condition"]].append(s)
        for _, sts in sorted(groups.items()):
            if any(s["state"] != "UNOBSERVABLE" or not s.get("checkable") for s in sts):
                continue
            for s in sorted(sts, key=lambda s: s["resource"]):
                if f"CHK_PPE_PROFILE({s['resource']})" in done:
                    continue
                if effort_h(self.sem, self.cfg, "CHK_PPE_PROFILE", s["resource"], ctx["budget"]) <= limit:
                    return {"check_id": "CHK_PPE_PROFILE", "target": s["resource"], "pid": s["pid"]}
        return None

    # ------------------------------------------------------------------ plan with forward branches
    def plan(self, ctx: dict, post: dict[str, float], *, confirm: dict | None = None) -> dict[str, Any] | None:
        cands = self.candidates(ctx, post)
        chosen = None
        if confirm is not None:
            chosen = next((c for c in cands if c["check_id"] == confirm["check_id"] and c["target"] == confirm["target"]),
                          None)
            if chosen is None:
                answers = {h: None for h in ctx["hyps"]}
                ev = ctx["evidence"][ctx["leader"]]
                answers[ctx["leader"]] = predicted_ppe_key(ev["preds"][confirm["pid"]])
                chosen = self._score(confirm["check_id"], confirm["target"], answers, post, ctx)
                chosen["admissible"], chosen["why"] = True, ""
            chosen = dict(chosen, reason="confirm")
        else:
            adm = [c for c in cands if c["admissible"]]
            chosen = adm[0] if adm else None
        if chosen is None:
            return None
        chosen["branches"] = self.forward(ctx, post, chosen, depth=1)
        return {"chosen": chosen, "ranked": cands[:8]}

    def forward(self, ctx: dict, post: dict, chosen: dict, depth: int) -> list[dict]:
        ac = self.cfg["advisor"]
        k = len(chosen["classes"])
        out = []
        for cls in sorted(chosen["classes"], key=lambda c: -chosen["p_answer"].get(c, 0)):
            pa = chosen["p_answer"].get(cls, 0)
            if pa < ac["forward_min_mass"] and len(out) >= 1:
                continue
            cond = conditional(post, chosen["answers"], cls, k)
            ok, lead = _would_commit(cond, self.cfg)
            hyps_in = [h for h in ctx["hyps"] if chosen["answers"].get(h) == cls]
            b = {"answer": cls, "p": pa, "hyps": hyps_in, "leader": lead, "p_leader": cond.get(lead, 0) if lead else 0}
            if ok:
                b["next"] = f"COMMIT {ctx['hyps'][lead]['mode']} @ {ctx['hyps'][lead]['location']}"
            elif cond.get(NOTA, 0) > self.cfg["ledger"]["regenerate_nota"]:
                b["next"] = "return to generation"
            elif depth < ac["forward_depth"]:
                sub = dict(ctx, done=list(ctx.get("done", [])) + [f"{chosen['check_id']}({chosen['target']})"])
                nxt = [c for c in self.candidates(sub, cond) if c["admissible"]]
                b["next"] = f"{nxt[0]['check_id']}({nxt[0]['target']})" if nxt else "no admissible check"
            else:
                b["next"] = "continue"
            out.append(b)
        return out
