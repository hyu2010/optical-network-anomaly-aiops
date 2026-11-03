"""Composition root (doc 02 §8): the only module that picks concrete backends."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from .config.settings import REPO_ROOT, Settings, load_settings

log = logging.getLogger("diag_agent")


# ====================================================================== semantic memory
def alarm_types_yang_path(settings: Settings) -> Path:
    return settings.path("memory.semantic.path").parent / "yang" / "diag-agent-alarm-types.yang"


def build_semantic(settings: Settings, *, force: bool = False):
    from .semantic_memory.loaders.catalog_to_rdf import alarm_types_yang
    from .semantic_memory.store import SemanticStore
    path = settings.path("memory.semantic.path")
    if path.exists() and not force:
        sem = SemanticStore.load(path)
    else:
        sem = SemanticStore.build(yang_dir=settings.path("substrate.yang_dir"),
                                  catalog_dir=settings.path("substrate.catalog_dir"), repo_root=settings.root,
                                  kb_release=settings.get("memory.semantic.kb_release", "kb-2026.11.0"))
        sem.save(path)
    ypath = alarm_types_yang_path(settings)
    if force or not ypath.exists():
        ypath.parent.mkdir(parents=True, exist_ok=True)
        names = list(sem.alarm_types())
        ypath.write_text(alarm_types_yang(names, {n: a["layer"] for n, a in sem.alarm_types().items()}))
    return sem


_SEM_CACHE: dict[str, Any] = {}


def semantic_cached(settings: Settings):
    key = str(settings.path("memory.semantic.path"))
    if key not in _SEM_CACHE:
        _SEM_CACHE[key] = build_semantic(settings)
    return _SEM_CACHE[key]


# ====================================================================== scenarios
def load_scenario(path: str | Path) -> dict:
    p = Path(path)
    sc = yaml.safe_load(p.read_text()) or {}
    sc["_path"] = str(p)
    for k in ("incident_db", "episodic"):
        if sc.get(k) and not Path(sc[k]).is_absolute():
            sc[k] = str(REPO_ROOT / sc[k])
    llm = sc.get("llm") or {}
    if llm.get("cache", {}).get("path") and not Path(llm["cache"]["path"]).is_absolute():
        llm["cache"]["path"] = str(REPO_ROOT / llm["cache"]["path"])
    return sc


def find_view_incident(db_path: str, raw_id: int, view: str) -> int:
    from .environment.substrate.simulator_db import list_incidents
    if view != "suppressed":
        return raw_id
    raw = next((r for r in list_incidents(db_path, suppressed=None) if r["id"] == raw_id), None)
    if raw is None:
        raise KeyError(f"incident {raw_id} not in {db_path}")
    twin = next((r for r in list_incidents(db_path, suppressed=True) if r["created_at"] == raw["created_at"]), None)
    if twin is None:
        raise KeyError(f"no suppressed twin of incident {raw_id}")
    return int(twin["id"])


# ====================================================================== run
@dataclass
class RunOptions:
    db: str
    incident: int
    scenario: dict = field(default_factory=dict)
    view: str = "raw"
    ppe_access: str | None = None
    llm_profile: str | None = None
    no_llm: bool = False
    cache_mode: str | None = None
    cache_path: str | None = None
    episodic: str | None = None
    cold: bool = False
    incidents_store: str | None = None
    validate: str | None = None
    step: bool = False
    checkpoints: str | None = None
    thread_id: str | None = None
    learn: bool = True
    field_seed: int = 0


class Agent:
    """One replayed diagnosis: deps + compiled graph + thread."""

    def __init__(self, settings: Settings, opts: RunOptions, *, incidents=None,
                 writer: Callable[[dict], None] | None = None):
        from .environment.checks.field import FieldOracle
        from .environment.substrate.simulator_db import SimulatorDBSubstrate
        from .episodic_memory.store import EpisodeStore, NoEpisodes
        from .harness.labels import LabelReader
        from .procedural_memory.decision_procedure.graph import build_graph, recursion_limit
        from .procedural_memory.decision_procedure.nodes import Deps
        from .procedural_memory.grounding.check_dispatch import CheckDispatcher

        overrides = {}
        if opts.no_llm:
            overrides["llm.profile"] = "none"
        elif opts.llm_profile:
            overrides["llm.profile"] = opts.llm_profile
        elif (opts.scenario.get("llm") or {}).get("profile"):
            overrides["llm.profile"] = opts.scenario["llm"]["profile"]
        if opts.ppe_access or opts.scenario.get("ppe_access"):
            overrides["agent.ppe_access"] = opts.ppe_access or opts.scenario.get("ppe_access")
        self.settings = settings = settings.with_overrides(**overrides) if overrides else settings
        self.opts = opts
        self.sem = semantic_cached(settings)
        incident_id = find_view_incident(opts.db, opts.incident, opts.view)
        self.substrate = SimulatorDBSubstrate(opts.db, incident_id, component="substrate")
        self.llm = build_llm(settings, opts)
        if opts.cold:
            self.episodic = NoEpisodes()
        else:
            ep_path = opts.episodic or opts.scenario.get("episodic") or str(settings.path("memory.episodic.path"))
            if not opts.episodic and opts.scenario.get("episodic") and Path(ep_path).exists():
                import shutil
                import tempfile
                tmp = Path(tempfile.mkdtemp(prefix="diag_agent_ep_")) / Path(ep_path).name
                shutil.copy(ep_path, tmp)
                ep_path = str(tmp)
            self.episodic = EpisodeStore(ep_path)
        self.labels = LabelReader(opts.db, opts.incident, component="field_oracle")
        oracle = FieldOracle(self.labels, seed=opts.field_seed)
        self.dispatcher = CheckDispatcher(self.sem, self.substrate, oracle)
        self.incidents = incidents or build_incident_service(settings, opts.incidents_store, opts.validate)
        cfg = settings.agent
        self.deps = Deps(sem=self.sem, substrate=self.substrate, episodic=self.episodic, llm=self.llm,
                         dispatcher=self.dispatcher, incidents=self.incidents, cfg=cfg,
                         kb_release=self.sem.kb_release, llm_profile=settings.llm_profile_name,
                         scenario=opts.scenario, ppe_access=cfg.get("ppe_access", "on_demand"), writer=writer,
                         publish_incident_graph=opts.learn)
        self.checkpointer = None
        if opts.checkpoints:
            import sqlite3
            from langgraph.checkpoint.sqlite import SqliteSaver
            Path(opts.checkpoints).parent.mkdir(parents=True, exist_ok=True)
            self._ck_conn = sqlite3.connect(opts.checkpoints, check_same_thread=False)
            self.checkpointer = SqliteSaver(self._ck_conn)
        elif opts.step:
            from langgraph.checkpoint.memory import InMemorySaver
            self.checkpointer = InMemorySaver()
        self.graph = build_graph(self.deps, checkpointer=self.checkpointer, step=opts.step)
        self.thread_id = opts.thread_id or f"replay-{opts.incident}-{uuid.uuid4().hex[:8]}"
        self.config = {"configurable": {"thread_id": self.thread_id}, "recursion_limit": recursion_limit(cfg)}

    def initial_state(self) -> dict:
        from .working_memory.state import initial_state
        return initial_state(raw_incident_id=self.opts.incident, created_at=self.substrate.created_at)

    def run(self) -> dict:
        state = self.graph.invoke(self.initial_state(), self.config)
        return state

    def stream_steps(self):
        """Step-through: yields (node name, update) and pauses after each STEP_NODE (caller resumes)."""
        inp: Any = self.initial_state()
        while True:
            for chunk in self.graph.stream(inp, self.config, stream_mode="updates"):
                for node, upd in chunk.items():
                    yield node, upd
            snap = self.graph.get_state(self.config)
            if not snap.next:
                return
            inp = None
            yield "__pause__", {"next": list(snap.next)}

    def final_state(self) -> dict:
        return self.graph.get_state(self.config).values if self.checkpointer else {}

    def close(self) -> None:
        self.substrate.close()
        if hasattr(self.episodic, "close"):
            self.episodic.close()


def build_llm(settings: Settings, opts: RunOptions | None = None):
    from .procedural_memory.llm.cache import LLMCache
    from .procedural_memory.llm.provider import NoLLM, OpenAICompatProvider
    if settings.llm_profile_name == "none":
        return NoLLM()
    sc_cache = ((opts.scenario if opts else {}).get("llm") or {}).get("cache") or {}
    mode = (opts.cache_mode if opts else None) or sc_cache.get("mode") or settings.get("llm.cache.mode", "read_write")
    path = (opts.cache_path if opts else None) or sc_cache.get("path") or str(settings.path("llm.cache.path"))
    enabled = settings.get("llm.cache.enabled", True) and mode != "off"
    cache = LLMCache(path, mode) if enabled else None
    return OpenAICompatProvider(settings, cache)


def build_incident_service(settings: Settings, store_path: str | None = None, validate: str | None = None):
    from .incident_server.service import IncidentService
    from .incident_server.store import IncidentStore
    from .incident_server.validate import PayloadValidator
    path = store_path or str(settings.path("incident_server.store"))
    store = IncidentStore(path, start_no=int(settings.get("incident_server.incident_no_start", 1001)))
    policy = validate or str(settings.get("incident_server.validate", "debug"))
    validator = None
    if policy != "off":
        if not alarm_types_yang_path(settings).exists():
            semantic_cached(settings)
        validator = PayloadValidator(settings.root, alarm_types_yang_path(settings), policy=policy)
    host, port = settings.get("incident_server.host", "127.0.0.1"), settings.get("incident_server.port", 8040)
    return IncidentService(store, mode=settings.get("incident_server.payload_mode", "extended"), validator=validator,
                           base_url=f"http://{host}:{port}")


def settings_from(config: str | None = None, **overrides) -> Settings:
    return load_settings(config, overrides={k: v for k, v in overrides.items() if v is not None} or None)
