# 02 — CoALA Architecture of diag_agent

`diag_agent` follows Figure 4 of *Cognitive Architectures for Language Agents* (CoALA):

- **Memory.** A working memory sits at the hub. Around it are three long-term memories: procedural, semantic and episodic.
- **Actions.** The action space divides into *internal* actions (retrieval, reasoning and learning) and *external grounding* actions (dialogue and digital).
- **Decision procedure.** It lives in procedural memory and runs a repeated *decision cycle* of planning (propose, evaluate, select), then execution, then observation.

## 1. Figure 4, instantiated

```
                 ┌──────────────────────── PROCEDURAL MEMORY (read-only at run time) ─────────────────────────┐
                 │  LLM (profile from config)  ·  prompts v#  ·  output schemas  ·  parsers/validators        │
                 │  reasoning procedures: framer, generation lines, propagator, location resolver, window     │
                 │  evaluator, evidence terms, ledger, advisor, tree builder                                  │
                 │  retrieval procedures: named SPARQL queries, KG walk tools, episodic retrieval             │
                 │  learning procedures: episode writer, base-rate updater                                    │
                 │  DECISION PROCEDURE: the LangGraph StateGraph (doc 05)                                     │
                 └───────────────▲───────────────────────────────▲───────────────────────────────▲────────────┘
                                 │ invoke (code + LLM)           │                               │
  ┌─────────────────┐  retrieval ┌┴──────────────────────────────┴┐  retrieval  ┌────────────────┴───────┐
  │ SEMANTIC MEMORY │──────────► │          WORKING MEMORY        │ ◄────────── │    EPISODIC MEMORY     │
  │ RDF dataset:    │            │  (LangGraph state, checkpointed)│            │ closed cases: role     │
  │ RFC 9940 upper, │ ◄───────── │  question · clock · case frame │ ──────────► │ signature, tree,       │
  │ optical domain, │  learning  │  hypotheses · ledger · windows │  learning   │ ledger trajectory,     │
  │ fault-mode docs,│ (lifecycle │  diagnosis tree · plan · budget│  (closure)  │ cause relation,        │
  │ topology(t),    │  writers)  │  pending check · observations  │             │ verification weight    │
  │ incident facts  │            └───────────────▲────────────────┘             └────────────────────────┘
  └─────────────────┘                            │ grounding (external actions)
                       ┌─────────────────────────┼──────────────────────────────────────┐
                       │ DIGITAL: substrate reads (alarms, PPE, YANG), remote checks,   │
                       │          incident notifications to Incident Clients            │
                       │ DIALOGUE: operator question / approval / field-check answers   │
                       │ (PHYSICAL: field checks, done by a human or the replay oracle) │
                       └────────────────────────────────────────────────────────────────┘
```

> **Terminology note.** CoALA uses *grounding* for external actions. The Janz methodology uses *grounding* for resolving an LLM's location term onto KG elements. To avoid the clash, this design calls the latter **location resolution**. *Grounding action* always means an external action in the CoALA sense.

## 2. Working memory

Working memory is the LangGraph state for one diagnosis thread, one per incident (`thread_id = "inc-<incident-no>"`). It is checkpointed after every node, so any step can be replayed, inspected or resumed (doc 05 §4).

| Slot | Content | Written by |
|---|---|---|
| `question` | The anchor (incident, service or element), the question time, and the operator's text if any | Interface (dialogue) |
| `clock` | The current agent time: wall clock in live mode, simulated in replay | Execution and wait actions |
| `frame` | The case frame: onset interval, scope, symptoms `S#`, services `V#`, shared elements `E#`, quiet instruments, context. Built in both an instance form and a role form. | Framer (reasoning) |
| `windows` | The observation windows and their closure times per layer and per predicted effect | Window evaluator |
| `hypotheses` | Each candidate `H#`: fault mode, location, compound members, provenance (line, walk path), parameters | Generation lines, then location resolution |
| `ledger` | The prior pool, the evidence terms with their tau, the log posteriors, NOTA, the entropy history and the commit status | Ledger only |
| `tree` | Diagnosis-tree nodes and edges, appended as deltas | Tree builder |
| `plan` | Ranked checks with expected bits per hour, the chosen action, and the LLM orchestrator's rationale | Advisor and orchestrator |
| `budget` | Checks used and allowed, effort hours, return passes used, LLM calls and tokens | All actions |
| `obs_log` | Every observation fetched, with its evidence id, source and recorded time | Grounding actions |
| `incident` | The ietf-incident view under construction (doc 06) | Incident mapper |

Working memory is the only place where modules meet. A long-term memory never calls another long-term memory, and procedures exchange data only through state slots.

## 3. Procedural memory

Procedural memory holds the LLM and the agent's code. It is versioned with the repository and **read-only at run time**.

| Component | Package | Notes |
|---|---|---|
| LLM provider registry | `procedural_memory/llm/` | Holds named profiles (DeepSeek, local vLLM, none), one per LLM role (doc 05 §6) |
| Prompts | `procedural_memory/prompts/*.md` | Each prompt carries a version and a content hash, which are logged in each episode |
| Output schemas | `procedural_memory/schemas/` | Pydantic models for every LLM output, plus a repair-retry parser |
| Reasoning procedures | `procedural_memory/reasoning/` | Framer, generation lines, propagator, location resolver, window evaluator, evidence terms, ledger, advisor, tree builder (doc 04) |
| Retrieval procedures | `procedural_memory/retrieval/` | Named SPARQL queries (`queries/*.rq`), KG-walk tools, episodic retrieval |
| Learning procedures | `procedural_memory/learning/` | Episode writer, base-rate updater, procedure-candidate extractor |
| Grounding procedures | `procedural_memory/grounding/` | Check executors and the notification emitter. These are thin and call into `environment/`. |
| Decision procedure | `procedural_memory/decision_procedure/` | Builds the LangGraph `StateGraph` (doc 05) |

## 4. Semantic memory

Semantic memory is an RDF dataset of named graphs, specified in doc 03:

| Named graph | Content | Writer (lifecycle event) |
|---|---|---|
| `g:ontology` | `onm:` (RFC 9940), `optx:`, `dx:` TBox | Release |
| `g:kb/<release>` | Fault modes, effects, alarm types, suppression edges, probable causes, location terms, checks, consistency rules | Documentation publication |
| `g:net/<topology_id>/<valid-from>` | Resources and relationships from YANG | Onboarding or inventory sync; the `kg_engine` `topology.*` events |
| `g:incident/<no>` | The RFC 9940 interpretation of a *closed* incident: Symptoms, Problems, Fault, Cause, Incident | Closure |
| `g:stats/<date>` | Base rates by fault mode and resource type | Closure, from verified episodes only |

**Interface.** `SemanticMemory` exposes these operations:

- `sparql(name, params, as_of)`
- `walk(anchor, step, as_of)` (the KG-walk tool primitive)
- `documented_modes(resource)`
- `effects(mode)`
- `resolve_location(term, anchor)`
- `alarm_type(name)`
- `rules()`
- `checks()`
- `publish(lifecycle_event)`

**Backend.** The backend is rdflib with a TriG file store; `pyoxigraph` can be swapped in for speed. Every query takes `as_of = (valid_time, record_time)`.

**Observations are not semantic memory.** Raw alarms and PPE samples stay in the substrate and are reached by named queries. This follows Janz: "observed state stays in the lake, joined by element ID". Only *interpreted* facts (an RFC 9940 incident graph) are written into semantic memory, at closure.

## 5. Episodic memory

Episodic memory is a separate store, kept apart from the graph on purpose. It is SQLite at `data/diag_agent/episodes.sqlite`.

| Table | Columns (main) |
|---|---|
| `episodes` | `episode_id`, `incident_no`, `opened`, `closed` <br> `kb_release`, `topology_id` — the graph as it then stood <br> `role_signature` (JSON), `instance_signature` (JSON) <br> `frame`, `tree` and `ledger_trajectory` (JSON) <br> `conclusion`: mode, a location *relation* in role form, probability <br> `outcome`: right, partial, wrong, IDK or non-convergence, when scored <br> `verification` (`repair_confirmed` = 1.0, `engineer` = 0.7, `agent_unconfirmed` = 0.3, `refuted` = 0) <br> `llm_profile`, `prompt_versions` |
| `followups` | `episode_id`, `at`, `kind` (verification, correction, repair), `payload`. These are dated revisions; an episode itself is never rewritten. |
| `cycles` | Per decision cycle: the action chosen, the entropy before and after, and its cost. Used for planner evaluation, not as evidence. |

**Interface.** `EpisodicMemory` exposes these operations:

- `retrieve(role_signature, k=20) -> [(episode, similarity)]`
- `write_episode(...)` (closure only)
- `add_followup(...)`

**Self-confirmation guard.** The agent's own unconfirmed conclusions carry weight 0.3. They therefore cannot dominate the analogy line, which was a measured pitfall on the Janz test bed.

## 6. Action space

### 6.1 Internal actions

| Kind | Action | Reads | Writes |
|---|---|---|---|
| Retrieval | `kg.walk`, `kg.query`, `kg.documented_modes`, `kg.effects`, `kg.resolve_location` | Semantic memory | Working memory (frame, hypotheses) |
| Retrieval | `episodes.retrieve` | Episodic memory | Working memory (analogy candidates) |
| Reasoning | Frame, generate, pool, propagate, evaluate windows, update ledger, plan, build tree, narrate | Working memory, LLM | Working memory |
| Learning | `episodes.write_episode`, `episodes.add_followup`, `kb.publish(base_rates)`, `kb.publish(incident_graph)`, `procedures.queue_candidate` | Working memory | Episodic or semantic memory; candidates go to a review queue |

### 6.2 External grounding actions

| Space | Action | Implementation (`environment/`) |
|---|---|---|
| Digital | Substrate reads: alarms, PPE, topology | `substrate/simulator_db.py` (replay or live polling), `substrate/kg_engine_sse.py` (live), `substrate/yang_reader.py` |
| Digital | Remote checks: PPE capture, alarm audit, reverse-fibre read, service state, wait | `checks/remote.py` |
| Digital | Incident notifications and RPC responses | `incident_server/` (doc 06) |
| Dialogue | Operator questions, approvals and field-check answers | LangGraph `interrupt()` in live mode; CLI or API resume |
| Physical, through a human | Field checks: OTDR, connector inspection, board swap, loopback | `checks/field.py`. In live mode a human answers through an interrupt; in replay mode `FieldOracle` answers, the only component apart from the scorer allowed to read labels |

### 6.3 Learning policy

The risky writes are restricted:

- **Episodic memory:** the agent may write freely, but only at closure.
- **Base rates in semantic memory:** updated only from episodes with verification weight of at least 0.7.
- **Incident graphs:** written at closure with the episode's verification level.
- **Procedural memory:** never written. A committed tree whose path repeats across verified episodes is proposed as a candidate `dx:Procedure` in `data/diag_agent/review_queue/`, and becomes knowledge only after a human publishes it.

## 7. Decision cycle

Each cycle maps onto CoALA's planning (propose, evaluate, select) followed by execution and observation:

| CoALA stage | diag_agent step | Doc |
|---|---|---|
| Observation | Ingest new observations up to `clock`; update the frame and the window closures | 04 §3, §6 |
| Planning: propose | Generation lines, including the KG-walk-guardrailed LLM proposer, add hypotheses. This runs on the first cycle and on return passes. | 04 §4–5 |
| Planning: evaluate | Windowed evaluation computes evidence terms; the ledger updates the posteriors; the tree builder adds question nodes | 04 §6–7, §9 |
| Planning: select | Choose one: commit, run a check, wait for a window, take a return pass, or stop with "I don't know". The advisor ranks the options and the LLM orchestrator may choose among them. | 04 §8 |
| Execution | A grounding action: run a check, wait, emit an incident notification, or ask the operator | 04 §8, 06 |
| (Learning) | At closure: write the episode, base rates and the incident graph | 02 §6.3 |

The cycle repeats until it commits, reports "I don't know", or hits a limit (12 checks, 8 h of effort, 3 return passes, plus a wall-clock deadline in live mode).

## 8. Decoupling rules

1. **One package per module.** Package names: `working_memory`, `procedural_memory`, `semantic_memory`, `episodic_memory`, `environment`, `incident_server`.
2. **Interfaces are Python `Protocol`s.** They live in `diag_agent/interfaces.py`: `SemanticMemory`, `EpisodicMemory`, `Substrate`, `CheckExecutor`, `LLMProvider`, `Notifier`, `Clock`. Procedures depend on these interfaces only.
3. **Composition at one root.** The concrete backends (rdflib or Oxigraph, SQLite, simulator DB or SSE, DeepSeek or vLLM) are chosen in a single composition root, `diag_agent/app.py`, from configuration.
4. **Import contracts are checked in CI with `import-linter`:**
   - The long-term memories do not import one another.
   - `procedural_memory.reasoning` does not import `environment`.
   - No module imports the simulator's generators (doc 01 §5.3).
5. **Each module has its own tests and a fake implementation.** The fakes are an in-memory graph, an in-memory episodic store, a scripted substrate and a scripted LLM. The decision procedure can therefore be tested without any external system.

## 9. Package layout

```
diag_agent/
  app.py                     # composition root: config → concrete backends → compiled graph
  interfaces.py              # Protocols listed in §8
  config/                    # settings.py (pydantic-settings), diag_agent.yaml loader
  working_memory/            # state.py (DiagnosisState), tree.py (DiagnosisTree), ids.py
  procedural_memory/
    llm/                     # providers.py, structured.py (json_schema / json_mode / repair)
    prompts/                 # proposer.v1.md, orchestrator.v1.md, narrator.v1.md, interface.v1.md
    schemas/                 # proposer.py, orchestrator.py, narrator.py, interface.py
    reasoning/
      framer.py  onset.py  windows.py  propagator.py  location.py
      generation/  walk.py  set_cover.py  correlation.py  analogy.py  llm_proposer.py
                   shared_risk.py  monitoring.py  pooling.py
      terms/       doc_alarm.py  graph_consistency.py  ppe_twin.py  checks.py  neutrality.py
      ledger.py  residual.py  advisor.py  tree_builder.py  narrator.py
    retrieval/               # queries/*.rq, kg_walk_tools.py, episodic_retrieval.py
    learning/                # episode_writer.py, base_rates.py, procedure_candidates.py
    grounding/               # check_dispatch.py, notify.py
    decision_procedure/      # graph.py (StateGraph), nodes.py, routes.py
  semantic_memory/
    store.py                 # rdflib / pyoxigraph Dataset, bitemporal as_of
    ontology/                # onm-rfc9940.ttl, optx-optical.ttl, dx-diagnosis.ttl
    kb/                      # kb-fault-modes.ttl, location-terms.ttl, checks.ttl, rules.ttl
    loaders/                 # yang_to_rdf.py, catalog_to_rdf.py, suppression_to_rdf.py
  episodic_memory/           # store.py, signature.py, retrieval.py
  environment/
    substrate/               # simulator_db.py, kg_engine_sse.py, yang_reader.py, allowlist.py, clock.py
    checks/                  # remote.py, field.py (FieldOracle for replay)
  incident_server/           # model.py, mapper.py, lifecycle.py, rpc.py, notifier.py, api.py, store.py
    yang/                    # diag-agent-incident-ext.yang (+ extracted ietf-incident for validation)
  render/                    # tree_ascii.py (rich), tree_mermaid.py, tree_json.py
  cli.py                     # diag-agent replay|live|serve|tree|kb
tests/diag_agent/            # unit, integration (replay), scenario matrix, leakage tests
config/diag_agent.yaml       # LLM profiles and agent settings (doc 05 §6)
```
