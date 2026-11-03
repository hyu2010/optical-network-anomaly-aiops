"""Semantic memory: an rdflib Dataset of named graphs (doc 02 §4, doc 03 §5).

Named graphs
  g:ontology             onm: + optx: + dx: TBox
  g:kb/<release>         fault modes, effects, roles, cascades, checks, rules, alarm types
  g:net/<tid>/<from>     lab-mesh resources from YANG
  g:incident/<no>        closed incidents (closure lifecycle writer)
  g:stats/<date>         base rates (verified episodes only)
The default graph holds the catalogue: dx:validFrom / dx:recordedAt /
dx:writtenBy per named graph (bitemporal bookkeeping).

Walk patterns are SPARQL group graph patterns; each is evaluated once over the
whole network (as a relation anchor -> target) and indexed both ways, so the
propagator, the abduction walk and the LLM tools are dictionary lookups.
"""

from __future__ import annotations

import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rdflib import Dataset, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from .namespaces import CASE, DX, G, KB, ONM, OPTX, net_ns, sparql_prefixes

HERE = Path(__file__).resolve().parent
ONTOLOGY_FILES = ["ontology/onm-rfc9940.ttl", "ontology/optx-optical.ttl", "ontology/dx-diagnosis.ttl"]
KB_FILES = ["kb/kb-fault-modes.ttl"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def local(iri: Any) -> str:
    s = str(iri)
    for sep in ("#", "/"):
        if sep in s:
            s = s.rsplit(sep, 1)[1]
    return s


class SemanticStore:
    """Concrete `SemanticMemory` over rdflib."""

    def __init__(self, ds: Dataset, topology_id: str, kb_release: str):
        self.ds = ds
        self.topology_id = topology_id
        self.kb_release = kb_release
        self.NET = net_ns(topology_id)
        self._lock = threading.RLock()
        self._index()

    # ================================================================== build
    @classmethod
    def build(cls, *, yang_dir: Path, catalog_dir: Path, repo_root: Path,
              kb_release: str = "kb-2026.11.0", valid_from: str = "2026-11-01T00:00:00Z") -> "SemanticStore":
        from .loaders.catalog_to_rdf import catalog_graph
        from .loaders.yang_to_rdf import load_topology, topology_to_graph

        ds = Dataset(default_union=True)
        ds.bind("onm", ONM), ds.bind("optx", OPTX), ds.bind("dx", DX), ds.bind("kb", KB), ds.bind("g", G)
        recorded = _now_iso()

        onto = ds.graph(G["ontology"])
        for f in ONTOLOGY_FILES:
            onto.parse(HERE / f, format="turtle")
        cls._catalogue(ds, G["ontology"], valid_from, recorded, "Release", "diag_agent/semantic_memory/ontology")

        kb_g = ds.graph(G[f"kb/{kb_release}"])
        curated = Graph()
        for f in KB_FILES:
            curated.parse(HERE / f, format="turtle")
        for t in curated:
            kb_g.add(t)
        for t in catalog_graph(catalog_dir, curated):
            kb_g.add(t)
        cls._catalogue(ds, G[f"kb/{kb_release}"], valid_from, recorded, "DocumentationPublication",
                       "kb-fault-modes.ttl + data/alarm_*.json")

        topo = load_topology(yang_dir, repo_root)
        net_graph, tid = topology_to_graph(topo)
        gname = G[f"net/{tid}/{valid_from[:10]}"]
        ng = ds.graph(gname)
        for t in net_graph:
            ng.add(t)
        cls._catalogue(ds, gname, valid_from, recorded, "Onboarding", "data/yang/{topology,tunnels,inventory}.json",
                       comment=f"topology_id {tid}")
        return cls(ds, tid, kb_release)

    @staticmethod
    def _catalogue(ds: Dataset, gname: URIRef, valid_from: str, recorded: str, event: str, used: str,
                   comment: str | None = None) -> None:
        from rdflib import BNode
        from rdflib.namespace import Namespace, XSD
        PROV = Namespace("http://www.w3.org/ns/prov#")
        d = ds.default_context
        d.add((gname, DX.validFrom, Literal(valid_from, datatype=XSD.dateTime)))
        d.add((gname, DX.recordedAt, Literal(recorded, datatype=XSD.dateTime)))
        ev = BNode()
        d.add((gname, DX.writtenBy, ev))
        d.add((ev, RDF.type, DX.LifecycleEvent))
        d.add((ev, RDFS.label, Literal(event)))
        d.add((ev, PROV.used, Literal(used)))
        if comment:
            d.add((gname, RDFS.comment, Literal(comment)))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self.ds.serialize(destination=str(path), format="trig")

    @classmethod
    def load(cls, path: Path) -> "SemanticStore":
        ds = Dataset(default_union=True)
        ds.parse(str(path), format="trig")
        tid, release = None, None
        for gname in ds.default_context.subjects(DX.validFrom, None):
            s = str(gname)
            if "/graph/net/" in s:
                tid = s.split("/graph/net/", 1)[1].split("/", 1)[0]
            if "/graph/kb/" in s:
                release = s.split("/graph/kb/", 1)[1]
        if not tid or not release:
            raise ValueError(f"{path} is not a diag_agent semantic memory (no net/kb graphs)")
        return cls(ds, tid, release)

    # ================================================================== indexing
    def q(self, query: str, **bindings) -> list:
        with self._lock:
            return list(self.ds.query(sparql_prefixes() + query, initBindings=bindings or None))

    def iri(self, yang_id: str) -> URIRef:
        return self._iri_of.get(yang_id) or self.NET[yang_id]

    def yid(self, node: Any) -> str:
        return self._yid_of.get(node) or local(node)

    def _index(self) -> None:
        ds = self.ds
        self._yid_of: dict[URIRef, str] = {}
        self._iri_of: dict[str, URIRef] = {}
        for s, _, o in ds.triples((None, OPTX.yangId, None)):
            self._yid_of[s] = str(o)
            self._iri_of[str(o)] = s

        # class closure
        sub = defaultdict(set)
        for s, _, o in ds.triples((None, RDFS.subClassOf, None)):
            sub[s].add(o)

        def supers(c, seen=None):
            seen = seen or set()
            for p in sub.get(c, ()):
                if p not in seen:
                    seen.add(p)
                    supers(p, seen)
            return seen
        self._supers = {c: supers(c) | {c} for c in set(sub) | {o for v in sub.values() for o in v}}
        self._classes: dict[str, set[str]] = defaultdict(set)
        for node, yid in self._yid_of.items():
            for c in ds.objects(node, RDF.type):
                for sc in self._supers.get(c, {c}):
                    self._classes[yid].add(local(sc))

        # layers
        self._layers: dict[str, dict] = {}
        for lay in ds.subjects(RDF.type, OPTX.Layer):
            key = ds.value(lay, OPTX.layerKey)
            info = {"iri": lay, "id": local(lay), "rank": int(ds.value(lay, OPTX.layerRank) or 0),
                    "cadence_ms": int(ds.value(lay, OPTX.pollCadenceMs) or 60000)}
            self._layers[local(lay)] = info
            if key is not None:
                self._layers[str(key)] = info

        # alarm types
        self._alarm_types: dict[str, dict] = {}
        for at in ds.subjects(RDF.type, OPTX.AlarmType):
            name = ds.value(at, OPTX.alarmName)
            if name is None:
                continue
            lay = ds.value(at, OPTX.alarmLayer)
            cond = ds.value(at, OPTX.indicatesCondition)
            self._alarm_types[str(name)] = {
                "iri": at, "name": str(name), "layer": local(lay) if lay else "OTHER",
                "condition": local(cond) if cond else "OtherCondition",
                "severity": str(ds.value(at, OPTX.defaultSeverity) or ""),
                "keywords": sorted({str(k) for k in ds.objects(at, OPTX.causeKeyword)}),
                "possible_causes": sorted({local(m) for m in ds.objects(at, DX.possibleCause)}),
                "suppresses": sorted({str(ds.value(d, OPTX.alarmName) or local(d)) for d in ds.objects(at, OPTX.suppresses)}),
            }
        self._descendants: dict[str, set[str]] = {}

        # condition closure
        self._cond_supers = {local(c): {local(x) for x in v} for c, v in self._supers.items()}

        # roles and terms (relations)
        self._roles: dict[str, dict] = {}
        for role in set(ds.subjects(RDF.type, DX.LocationRole)) | set(ds.subjects(RDF.type, DX.LocationTerm)):
            pat = ds.value(role, DX.walkPattern)
            if pat is None:
                continue
            self._roles[local(role)] = {
                "id": local(role), "term": str(ds.value(role, DX.termId) or local(role)),
                "anchor_kind": str(ds.value(role, DX.anchorKind) or "any"),
                "pattern": str(pat), "meaning": str(ds.value(role, DX.meaning) or ""),
                "result_class": local(ds.value(role, DX.resultClass)) if ds.value(role, DX.resultClass) else None,
                "is_term": (role, RDF.type, DX.LocationTerm) in ds,
            }
        self._rel_fwd: dict[str, dict[str, list[str]]] = {}
        self._rel_rev: dict[str, dict[str, list[str]]] = {}

        # fault modes
        self._modes: dict[str, dict] = {}
        for m in ds.subjects(RDF.type, DX.FaultMode):
            effects = []
            for e in ds.objects(m, DX.hasEffect):
                sig = ds.value(e, DX.ppeSignature)
                sigd = None
                if sig is not None:
                    sigd = {"kind": str(ds.value(sig, DX.sigKind) or ""),
                            "min_db": _f(ds.value(sig, DX.sigMagnitudeMinDb)),
                            "max_db": _f(ds.value(sig, DX.sigMagnitudeMaxDb)),
                            "position": str(ds.value(sig, DX.sigPosition) or "")}
                effects.append({
                    "condition": local(ds.value(e, DX.predictsCondition)),
                    "role": local(ds.value(e, DX.atRole)),
                    "layer": local(ds.value(e, DX.inLayer)) if ds.value(e, DX.inLayer) else "OTHER",
                    "strength": local(ds.value(e, DX.strength)) if ds.value(e, DX.strength) else "Possible",
                    "delay_ms": int(ds.value(e, DX.physDelayMs) or 0),
                    "lasting": bool(ds.value(e, DX.lasting)) if ds.value(e, DX.lasting) is not None else False,
                    "no_cascade": bool(ds.value(e, DX.noCascade)) if ds.value(e, DX.noCascade) is not None else False,
                    "keyword_anchor": bool(ds.value(e, DX.keywordAnchor)) if ds.value(e, DX.keywordAnchor) is not None else False,
                    "signature": sigd,
                })
            effects.sort(key=lambda e: (not e["keyword_anchor"], e["layer"] == "PPE", e["role"], e["condition"]))
            params = []
            for p in ds.objects(m, DX.hasParameter):
                params.append({"name": str(ds.value(p, DX.paramName)), "min": _f(ds.value(p, DX.paramMin)),
                               "max": _f(ds.value(p, DX.paramMax)), "unit": str(ds.value(p, DX.paramUnit) or "")})
            self._modes[local(m)] = {
                "id": local(m), "iri": m, "label": str(ds.value(m, RDFS.label) or local(m)),
                "nature": local(ds.value(m, DX.nature)) if ds.value(m, DX.nature) else "",
                "applies_to": local(ds.value(m, DX.appliesTo)),
                "base_rate": _f(ds.value(m, DX.baseRatePerYear)) or 0.01,
                "keywords": sorted({str(k) for k in ds.objects(m, DX.causeKeyword)}),
                "effects": effects, "params": params,
                "services_impact": local(ds.value(m, DX.servicesImpact)) if ds.value(m, DX.servicesImpact) else None,
                "cause_name": str(ds.value(m, DX.incidentCauseName) or "ietf-incident:hardware-failure"),
                "cause_name_ext": str(ds.value(m, DX.incidentCauseNameExt) or ""),
                "category": str(ds.value(m, DX.incidentCategory) or "ietf-incident:network"),
                "resolve_advice": str(ds.value(m, DX.resolveAdvice) or ""),
                "simulator_fault": str(ds.value(m, DX.simulatorFault) or ""),
            }

        # cascade rules
        self._cascades = []
        for r in ds.subjects(RDF.type, DX.CascadeRule):
            self._cascades.append({
                "id": local(r), "from_condition": local(ds.value(r, DX.fromCondition)),
                "from_layer": local(ds.value(r, DX.fromLayer)), "to_condition": local(ds.value(r, DX.toCondition)),
                "to_layer": local(ds.value(r, DX.toLayer)), "pattern": str(ds.value(r, DX.viaPattern)),
                "transfer": local(ds.value(r, DX.strengthTransfer)), "add_delay_ms": int(ds.value(r, DX.addDelayMs) or 0)})
        self._cascades.sort(key=lambda c: c["id"])

        # checks
        self._checks: dict[str, dict] = {}
        for c in ds.subjects(RDF.type, DX.Check):
            cid = str(ds.value(c, DX.checkId))
            self._checks[cid] = {
                "id": cid, "minutes": float(ds.value(c, DX.minutes) or 0), "remote": bool(ds.value(c, DX.remote)),
                "interrupting": bool(ds.value(c, DX.serviceInterrupting)),
                "target_class": local(ds.value(c, DX.targetClass)) if ds.value(c, DX.targetClass) else None,
                "tau": float(ds.value(c, DX.tau) or 1.0), "answers_with": str(ds.value(c, DX.answersWith) or ""),
                "position_tol_km": _f(ds.value(c, DX.positionToleranceKm)) or 0.4,
                "clean_slope_max": _f(ds.value(c, DX.cleanSlopeMaxDbPerKm)) or 0.08,
                "comment": str(ds.value(c, RDFS.comment) or ""),
            }

        # consistency rules
        self._rules = []
        for r in ds.subjects(RDF.type, DX.ConsistencyRule):
            self._rules.append({"id": str(ds.value(r, DX.ruleId)), "label": str(ds.value(r, RDFS.label) or ""),
                                "penalty": float(ds.value(r, DX.penaltyNats) or 4.0),
                                "ask": str(ds.value(r, DX.askQuery))})
        self._rules.sort(key=lambda r: int(r["id"][1:]) if r["id"][1:].isdigit() else 99)

        # dependsOn and neighbours
        self._depends: dict[str, set[str]] = defaultdict(set)
        for s, _, o in ds.triples((None, DX.dependsOn, None)):
            self._depends[self.yid(s)].add(self.yid(o))
        self._modes_at: dict[str, list[str]] = {}

    # ================================================================== relations
    def relation(self, pattern: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        if pattern not in self._rel_fwd:
            rows = self.q(f"SELECT DISTINCT ?anchor ?target WHERE {{ ?anchor optx:yangId ?_aid . {pattern} "
                          f"?target optx:yangId ?_tid . }}")
            fwd, rev = defaultdict(list), defaultdict(list)
            for a, t in rows:
                ay, ty = self.yid(a), self.yid(t)
                if ty not in fwd[ay]:
                    fwd[ay].append(ty)
                if ay not in rev[ty]:
                    rev[ty].append(ay)
            for d in (fwd, rev):
                for k in d:
                    d[k].sort()
            self._rel_fwd[pattern], self._rel_rev[pattern] = dict(fwd), dict(rev)
        return self._rel_fwd[pattern], self._rel_rev[pattern]

    def walk(self, pattern: str, anchor: str) -> list[str]:
        return list(self.relation(pattern)[0].get(anchor, []))

    def walk_reverse(self, pattern: str, target: str) -> list[str]:
        return list(self.relation(pattern)[1].get(target, []))

    def role(self, role_id: str) -> dict:
        return self._roles[role_id]

    def role_walk(self, role_id: str, anchor: str) -> list[str]:
        return self.walk(self._roles[role_id]["pattern"], anchor)

    def role_reverse(self, role_id: str, target: str) -> list[str]:
        return self.walk_reverse(self._roles[role_id]["pattern"], target)

    def location_terms(self) -> dict[str, dict]:
        return {r["term"]: r for r in self._roles.values() if r["is_term"]}

    def resolve_location(self, term: str, anchor: str) -> list[str]:
        t = self.location_terms().get(term)
        if t is None:
            return []
        return self.walk(t["pattern"], anchor)

    # ================================================================== lookups
    def sparql(self, query: str, bindings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        rows = self.q(query, **(bindings or {}))
        out = []
        for r in rows:
            out.append({str(k): (self.yid(v) if isinstance(v, URIRef) else (v.toPython() if v is not None else None))
                        for k, v in r.asdict().items()})
        return out

    def modes(self) -> dict[str, dict]:
        return self._modes

    def mode(self, mode_id: str) -> dict:
        return self._modes[mode_id]

    def cascade_rules(self) -> list[dict]:
        return self._cascades

    def checks(self) -> dict[str, dict]:
        return self._checks

    def rules(self) -> list[dict]:
        return self._rules

    def alarm_type(self, name: str) -> dict:
        from .loaders.catalog_to_rdf import condition_for, layer_family, normalise
        n = normalise(name)
        at = self._alarm_types.get(n)
        if at is None:
            lay = layer_family(n)
            at = {"iri": None, "name": n, "layer": self.layer(lay)["id"], "condition": local(condition_for(n, lay)),
                  "severity": "", "keywords": [], "possible_causes": [], "suppresses": []}
            self._alarm_types[n] = at
        return at

    def alarm_types(self) -> dict[str, dict]:
        return self._alarm_types

    def descendants(self, name: str) -> set[str]:
        """Transitive suppression children of an alarm type (same location)."""
        if name not in self._descendants:
            seen, stack = set(), [name]
            while stack:
                cur = stack.pop()
                for d in self._alarm_types.get(cur, {}).get("suppresses", []):
                    if d not in seen:
                        seen.add(d)
                        stack.append(d)
            self._descendants[name] = seen
        return self._descendants[name]

    def alarm_types_for(self, condition: str) -> list[str]:
        return [n for n, a in self._alarm_types.items() if self.cond_subsumes(condition, a["condition"])]

    def layer(self, key: str) -> dict:
        return self._layers.get(key) or self._layers.get(str(key).upper()) or self._layers["OTHER"]

    def cond_subsumes(self, general: str, specific: str) -> bool:
        return general == specific or general in self._cond_supers.get(specific, set())

    def classes_of(self, yang_id: str) -> set[str]:
        return self._classes.get(yang_id, set())

    def is_a(self, yang_id: str, cls: str) -> bool:
        return cls in self._classes.get(yang_id, set())

    def exists(self, yang_id: str) -> bool:
        return yang_id in self._iri_of

    def kind_of(self, yang_id: str) -> str:
        c = self.classes_of(yang_id)
        for k, name in (("OmsSpan", "span"), ("OtuLink", "otu"), ("OchTunnel", "och"), ("OduTunnel", "odu"),
                        ("OmsSection", "section"), ("Port", "port"), ("Board", "board"),
                        ("NetworkElement", "ne"), ("ClientService", "service"), ("Amplifier", "amplifier")):
            if k in c:
                return name
        return "other"

    def resource(self, yang_id: str) -> dict:
        node = self.iri(yang_id)
        out: dict[str, Any] = {"id": yang_id, "kind": self.kind_of(yang_id), "classes": sorted(self.classes_of(yang_id))}
        for p, o in self.ds.predicate_objects(node):
            if p in (RDF.type, OPTX.yangId, OPTX.yangPath, DX.dependsOn, OPTX.hasHop):
                continue
            key = local(p)
            val = self.yid(o) if isinstance(o, URIRef) else o.toPython() if hasattr(o, "toPython") else str(o)
            if key in out:
                if not isinstance(out[key], list):
                    out[key] = [out[key]]
                out[key].append(val)
            else:
                out[key] = val
        return out

    def value(self, yang_id: str, prop: str) -> Any:
        v = self.ds.value(self.iri(yang_id), OPTX[prop])
        if v is None:
            return None
        return self.yid(v) if isinstance(v, URIRef) else v.toPython()

    def values(self, yang_id: str, prop: str) -> list:
        return sorted(self.yid(o) if isinstance(o, URIRef) else o.toPython()
                      for o in self.ds.objects(self.iri(yang_id), OPTX[prop]))

    def subjects_with(self, prop: str, yang_id: str) -> list[str]:
        return sorted(self.yid(s) for s in self.ds.subjects(OPTX[prop], self.iri(yang_id)))

    def hops(self, och: str) -> list[tuple[int, str, float, float]]:
        """Ordered (index, span, start_km, length_km) of an OCh route."""
        out = []
        for h in self.ds.objects(self.iri(och), OPTX.hasHop):
            idx = int(self.ds.value(h, OPTX.hopIndex))
            span = self.yid(self.ds.value(h, OPTX.hopLink))
            start = float(self.ds.value(h, OPTX.hopStartKm))
            out.append((idx, span, start, float(self.value(span, "lengthKm") or 0)))
        return sorted(out)

    def neighbors(self, yang_id: str, limit: int = 30) -> list[dict]:
        node = self.iri(yang_id)
        out = []
        for p, o in self.ds.predicate_objects(node):
            if isinstance(o, URIRef) and o in self._yid_of and p not in (RDF.type,):
                out.append({"relation": local(p), "direction": "out", "node": self.yid(o),
                            "kind": self.kind_of(self.yid(o))})
        for s, p in self.ds.subject_predicates(node):
            if s in self._yid_of and p != DX.dependsOn:
                out.append({"relation": local(p), "direction": "in", "node": self.yid(s),
                            "kind": self.kind_of(self.yid(s))})
        out.sort(key=lambda d: (d["relation"] == "dependsOn", d["relation"], d["node"]))
        return out[:limit]

    def depends_on(self, yang_id: str) -> set[str]:
        return set(self._depends.get(yang_id, set()))

    def resources_of_kind(self, kind: str) -> list[str]:
        return sorted(y for y in self._iri_of if self.kind_of(y) == kind)

    def documented_modes(self, resource: str) -> list[str]:
        if resource not in self._modes_at:
            cls = self.classes_of(resource)
            self._modes_at[resource] = sorted(m for m, d in self._modes.items() if d["applies_to"] in cls)
        return self._modes_at[resource]

    def hosting_ne(self, yang_id: str) -> str | None:
        k = self.kind_of(yang_id)
        if k == "ne":
            return yang_id
        if k == "port":
            b = self.board_of_port(yang_id)
            return self.hosting_ne(b) if b else None
        if k == "board":
            nes = self.subjects_with("hostsBoard", yang_id)
            return nes[0] if nes else None
        if k in ("span", "otu", "och", "odu"):
            return self.value(yang_id, "sourceNE")
        if k == "section":
            spans = self.subjects_with("partOfSection", yang_id)
            return self.value(spans[0], "sourceNE") if spans else None
        if k == "amplifier":
            b = self.value(yang_id, "amplifierBoard")
            return self.hosting_ne(b) if b else None
        return None

    def board_of_port(self, port: str) -> str | None:
        b = self.subjects_with("hasPort", port)
        return b[0] if b else None

    # ================================================================== rules
    def ask_rules(self, case_triples: Graph, hypothesis_iris: dict[str, URIRef]) -> dict[str, list[str]]:
        """Evaluate every consistency rule for every hypothesis over {KG + case graph}.

        Returns {hypothesis id: [contradicted rule ids]}.
        """
        out: dict[str, list[str]] = {h: [] for h in hypothesis_iris}
        gname = CASE[f"g-{uuid.uuid4().hex}"]
        with self._lock:
            g = self.ds.graph(gname)
            for t in case_triples:
                g.add(t)
            try:
                for rule in self._rules:
                    query = sparql_prefixes() + rule["ask"]
                    for hid, hiri in hypothesis_iris.items():
                        if bool(self.ds.query(query, initBindings={"h": hiri}).askAnswer):
                            out[hid].append(rule["id"])
            finally:
                self.ds.remove_graph(g)
        return out

    # ================================================================== learning
    def publish(self, graph_name: str, triples_ttl: str, meta: dict) -> None:
        gname = G[graph_name]
        with self._lock:
            g = self.ds.graph(gname)
            g.parse(data=triples_ttl, format="turtle")
            self._catalogue(self.ds, gname, meta.get("valid_from", _now_iso()), _now_iso(),
                            meta.get("event", "Closure"), meta.get("used", "diag_agent"))
            if meta.get("valid_to"):
                from rdflib.namespace import XSD
                self.ds.default_context.add((gname, DX.validTo, Literal(meta["valid_to"], datatype=XSD.dateTime)))

    def graphs(self) -> list[dict]:
        out = []
        for gname in sorted(set(self.ds.default_context.subjects(DX.validFrom, None)), key=str):
            out.append({"graph": str(gname), "valid_from": str(self.ds.default_context.value(gname, DX.validFrom)),
                        "recorded_at": str(self.ds.default_context.value(gname, DX.recordedAt)),
                        "triples": len(self.ds.graph(gname))})
        return out

    def stats(self) -> dict:
        return {"topology_id": self.topology_id, "kb_release": self.kb_release, "modes": len(self._modes),
                "alarm_types": len(self._alarm_types), "checks": len(self._checks), "rules": len(self._rules),
                "location_terms": len(self.location_terms()), "graphs": self.graphs()}


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
