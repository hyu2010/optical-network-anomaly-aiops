"""LLM proposer, guardrailed by an agent-led KG walk (doc 04 §5).

A bounded ReAct loop: the LLM calls read-only KG tools (every node they
return is added to `walk_visited`), then returns JSON proposals.  Each
proposal is validated in six steps; failures go to the line residual and
become `pruned` nodes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from ...llm.cache import CassetteMiss
from ...llm.provider import LLMUnavailable, parse_json
from ..framer import anchor_resource, item_by_id

PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "proposer.v1.md"


class Location(BaseModel):
    term: str
    anchor: str


class Proposal(BaseModel):
    mode: str
    location: Location
    weight: float = Field(ge=0.0, le=1.0)
    walk: list[str] = []
    rationale: str = ""


class ProposerOutput(BaseModel):
    hypotheses: list[Proposal] = []
    nota: float = Field(default=0.0, ge=0.0, le=1.0)


TOOLS = [
    {"type": "function", "function": {"name": "frame_get", "description": "The case frame: S#, V#, E#, P#, quiet instruments, onset.",
                                      "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "kg_term", "description": "Resources a location term resolves to from a frame reference.",
                                      "parameters": {"type": "object", "properties": {
                                          "term": {"type": "string"}, "anchor": {"type": "string", "description": "S#, V# or E#"}},
                                          "required": ["term", "anchor"]}}},
    {"type": "function", "function": {"name": "kg_modes", "description": "Documented fault modes at a resource's grain.",
                                      "parameters": {"type": "object", "properties": {"resource": {"type": "string"}},
                                                     "required": ["resource"]}}},
    {"type": "function", "function": {"name": "kg_effects", "description": "Primary effects of a fault mode, in words.",
                                      "parameters": {"type": "object", "properties": {"mode": {"type": "string"}},
                                                     "required": ["mode"]}}},
    {"type": "function", "function": {"name": "kg_preview",
                                      "description": "Deterministic preview: items explained, required effects missing (window closed), pending.",
                                      "parameters": {"type": "object", "properties": {
                                          "mode": {"type": "string"}, "term": {"type": "string"}, "anchor": {"type": "string"}},
                                          "required": ["mode", "term", "anchor"]}}},
    {"type": "function", "function": {"name": "kg_neighbors", "description": "Up to 30 neighbours of a visited node, with relation and kind.",
                                      "parameters": {"type": "object", "properties": {"node": {"type": "string"}},
                                                     "required": ["node"]}}},
]


def render_frame(frame: dict) -> dict:
    return {
        "onset_s": frame["onset"], "t_frame_s": frame["t_frame"], "name": frame["name"],
        "items": [{"ref": it["id"], "condition": it["condition"], "at": it["resource"], "layer": it["layer"],
                   "role": it["role"], "alarms": it["root_names"][:6], "folded_alarms": len(it["folded"]),
                   "t_first_s": it["t_first"], "relevant": it["relevant"]} for it in frame["items"]],
        "services": [{"ref": v["id"], "service": v["service"], "state": v["state"]} for v in frame["services"]],
        "shared": [{"ref": e["id"], "resource": e["resource"], "kind": e["kind"]} for e in frame["shared"]],
        "ppe": [{"ref": p["id"], "och": p["och"], "kind": p["kind"], "span": p["span"], "offset_km": p["offset_km"],
                 "magnitude_db": p["magnitude_db"]} for p in frame.get("ppe", [])],
        "quiet_instruments": [{"at": q["resource"], "layer": q["layer"], "window_closes_s": q["window_closes"]}
                              for q in frame["quiet"][:24]],
    }


class KGTools:
    def __init__(self, sem, evaluator, frame: dict, ctx: dict, max_neighbors: int = 30):
        self.sem, self.evaluator, self.frame, self.ctx = sem, evaluator, frame, ctx
        self.max_neighbors = max_neighbors
        self.visited: set[str] = set()
        self.trace: list[dict] = []
        for it in frame["items"]:
            self.visited.add(it["resource"])
        for e in frame["shared"]:
            self.visited.add(e["resource"])

    def _anchor(self, ref: str) -> str | None:
        return anchor_resource(self.sem, self.frame, ref)

    def call(self, name: str, args: dict) -> Any:
        try:
            out = getattr(self, "t_" + name)(**args)
        except TypeError as e:
            out = {"error": f"bad arguments: {e}"}
        except AttributeError:
            out = {"error": f"unknown tool {name}"}
        self.trace.append({"tool": name, "args": args, "result": _short(out)})
        return out

    def t_frame_get(self) -> dict:
        return render_frame(self.frame)

    def t_kg_term(self, term: str, anchor: str) -> dict:
        terms = self.sem.location_terms()
        if term not in terms:
            return {"error": f"unknown term {term!r}", "terms": sorted(terms)}
        if term == "shared":
            x = item_by_id(self.frame, anchor)
            if x is None or not anchor.startswith("E"):
                return {"error": "term 'shared' takes an E# anchor"}
            self.visited.add(x["resource"])
            return {"resources": [x["resource"]]}
        res = self._anchor(anchor)
        if res is None:
            return {"error": f"unknown frame reference {anchor!r}"}
        out = self.sem.resolve_location(term, res)
        self.visited.update(out)
        return {"resources": out}

    def t_kg_modes(self, resource: str) -> dict:
        if not self.sem.exists(resource):
            return {"error": f"unknown resource {resource!r}"}
        self.visited.add(resource)
        return {"resource": resource, "kind": self.sem.kind_of(resource),
                "modes": [{"mode": m, "label": self.sem.mode(m)["label"]} for m in self.sem.documented_modes(resource)]}

    def t_kg_effects(self, mode: str) -> dict:
        if mode not in self.sem.modes():
            return {"error": f"{mode!r} is not a documented fault mode"}
        m = self.sem.mode(mode)
        return {"mode": mode, "label": m["label"], "applies_to": m["applies_to"],
                "effects": [f"{e['strength']}: {e['condition']} in {e['layer']} at {self.sem.role(e['role'])['term']}"
                            + (f" after {e['delay_ms']} ms" if e["delay_ms"] else "") for e in m["effects"]]}

    def t_kg_preview(self, mode: str, term: str, anchor: str) -> dict:
        if mode not in self.sem.modes():
            return {"error": f"{mode!r} is not a documented fault mode"}
        r = self.t_kg_term(term, anchor)
        if "error" in r:
            return r
        out = []
        for loc in r["resources"][:3]:
            if self.sem.mode(mode)["applies_to"] not in self.sem.classes_of(loc):
                out.append({"location": loc, "error": f"{mode} does not apply to a {self.sem.kind_of(loc)}"})
                continue
            pv = self.evaluator.preview(mode, loc, self.ctx)
            out.append({"location": loc, "explained": len(pv["explained"]), "explained_refs": pv["explained"],
                        "unexplained_refs": pv["unexplained"], "missing": pv["missing"][:6],
                        "pending": pv["pending"][:6]})
        return {"mode": mode, "previews": out}

    def t_kg_neighbors(self, node: str) -> dict:
        if node not in self.visited:
            return {"error": f"{node!r} has not been visited; reach it through kg_term or kg_modes first"}
        nb = self.sem.neighbors(node, limit=self.max_neighbors)
        self.visited.update(n["node"] for n in nb)
        return {"node": node, "neighbors": nb}


def _short(x: Any, n: int = 400) -> str:
    s = json.dumps(x, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…"


def validate(sem, frame: dict, out: ProposerOutput, visited: set[str]) -> dict[str, Any]:
    terms = sem.location_terms()
    dist: dict[str, float] = {}
    hyps: dict[str, dict] = {}
    pruned: list[dict] = []
    rejected = 0.0
    total = sum(p.weight for p in out.hypotheses) + out.nota
    scale = 1.0 / total if total > 0 else 1.0
    for p in out.hypotheses:
        w = p.weight * scale
        label = f"{p.mode}@{p.location.term}({p.location.anchor})"
        if p.mode not in sem.modes():
            pruned.append({"key": label, "reason": "undocumented mode", "weight": w, "check": 1})
            rejected += w
            continue
        if p.location.term not in terms or item_by_id(frame, p.location.anchor) is None:
            pruned.append({"key": label, "reason": "unknown term or anchor", "weight": w, "check": 2})
            rejected += w
            continue
        if p.location.term == "shared":
            x = item_by_id(frame, p.location.anchor)
            locs = [x["resource"]] if p.location.anchor.startswith("E") else []
        else:
            res = anchor_resource(sem, frame, p.location.anchor)
            locs = sem.resolve_location(p.location.term, res) if res else []
        if not locs:
            pruned.append({"key": label, "reason": "location term resolves to nothing", "weight": w, "check": 3})
            rejected += w
            continue
        applies = sem.mode(p.mode)["applies_to"]
        good = [l for l in locs if applies in sem.classes_of(l)]
        if not good:
            pruned.append({"key": label, "reason": f"grain: {p.mode} applies to {applies}, resolved to "
                                                   f"{sem.kind_of(locs[0])} {locs[0]}", "weight": w, "check": 4})
            rejected += w
            continue
        seen = [l for l in good if l in visited]
        if not seen:
            pruned.append({"key": label, "reason": f"no walk provenance for {good[0]}", "weight": w, "check": 5})
            rejected += w
            continue
        for l in seen:
            key = f"{p.mode}@{l}"
            dist[key] = dist.get(key, 0.0) + w / len(seen)
            h = hyps.setdefault(key, {"mode": p.mode, "location": l, "provenance": {"llm": {
                "term": p.location.term, "anchor": p.location.anchor, "rationale": p.rationale[:300],
                "walk": p.walk[:12]}}})
            _ = h
    residual = out.nota * scale + rejected
    if total <= 0:
        residual = 1.0
    return {"dist": dist, "hyps": hyps, "pruned": pruned, "residual": min(1.0, residual)}


def run(sem, evaluator, llm, frame: dict, ctx: dict, *, max_tool_calls: int = 8, max_neighbors: int = 30,
        pass_no: int = 0) -> dict[str, Any]:
    base = {"line": "llm", "dist": {}, "hyps": {}, "pruned": [], "residual": 1.0}
    if llm is None or not llm.available("proposer"):
        return dict(base, status="abstain", detail={"reason": "no LLM configured"})
    tools = KGTools(sem, evaluator, frame, ctx, max_neighbors=max_neighbors)
    terms_text = "\n".join(f"   - `{t}`: {d['meaning']}" for t, d in sorted(sem.location_terms().items()))
    system = PROMPT.read_text().replace("{location_terms}", terms_text).replace("{max_tool_calls}", str(max_tool_calls))
    user = (f"Incident: {frame['name']}. Agent time +{frame['t_frame']:.1f} s after the incident base time. "
            f"Generation pass {pass_no + 1}. Propose candidate root causes.")
    messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    calls, usage = 0, {"input": 0, "output": 0}
    n_llm = 0
    try:
        while True:
            resp = llm.chat("proposer", messages, tools=TOOLS if calls < max_tool_calls else None,
                            json_mode=calls >= max_tool_calls)
            n_llm += 1
            usage["input"] += resp["usage"]["input"]
            usage["output"] += resp["usage"]["output"]
            if resp["tool_calls"] and calls < max_tool_calls:
                messages.append({"role": "assistant", "content": resp["content"] or "",
                                 "tool_calls": resp["tool_calls"]})
                for tc in resp["tool_calls"]:
                    calls += 1
                    result = tools.call(tc["name"], tc["args"]) if calls <= max_tool_calls else \
                        {"error": "tool budget exhausted; answer now"}
                    messages.append({"role": "tool", "tool_call_id": tc["id"],
                                     "content": json.dumps(result, ensure_ascii=False)})
                if calls >= max_tool_calls:
                    messages.append({"role": "user", "content": "Tool budget used. Return the JSON object now."})
                continue
            text = resp["content"] or ""
            try:
                parsed = ProposerOutput.model_validate(parse_json(text))
            except (ValueError, ValidationError) as e:
                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content": f"That was not valid JSON for the schema ({str(e)[:300]}). "
                                                            "Return only the JSON object."})
                resp = llm.chat("proposer", messages, json_mode=True)
                n_llm += 1
                parsed = ProposerOutput.model_validate(parse_json(resp["content"] or ""))
            break
    except (LLMUnavailable, CassetteMiss, ValueError, ValidationError) as e:
        return dict(base, status="abstain", detail={"reason": f"{type(e).__name__}: {str(e)[:200]}",
                                                    "tool_trace": tools.trace}, visited=sorted(tools.visited))
    v = validate(sem, frame, parsed, tools.visited)
    status = "ok" if v["dist"] else "abstain"
    return {"line": "llm", "status": status, "dist": v["dist"], "hyps": v["hyps"], "pruned": v["pruned"],
            "residual": v["residual"] if v["dist"] else 1.0, "visited": sorted(tools.visited),
            "detail": {"tool_trace": tools.trace, "tool_calls": calls, "llm_calls": n_llm, "usage": usage,
                       "raw": parsed.model_dump()}}
