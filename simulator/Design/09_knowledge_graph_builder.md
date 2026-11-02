# Knowledge Graph Builder — Design Document

Status: **Draft for review.**
Scope: Phase-3 deliverable. Phase-2 (simulator) is complete and has produced `data/incidents.db`; Phase-4 (GNN training) will consume the KG this document specifies.
Related: `Design/04_knowledge_graph_plan.md` (concepts and tech survey), `Design/08_root_cause_rules.md` (rule predicates and inference targets).

---

## 1. What the Knowledge Graph Is

The Knowledge Graph (KG) is a **labelled, heterogeneous graph** that encodes everything a root-cause analysis (RCA) model needs to know about one fault incident:

- **Where** each alarm occurred (NE / board / port / span / tunnel).
- **When** each alarm was observed (physical propagation time + NMS polling delay).
- **How** alarms are topologically related (shared spans, underlay/overlay tunnels, connected ports).
- **Which** alarm is the root cause and which are derived (the prediction target).

Each piece of information is a **triple** of the form `(subject, predicate, object)`. The full KG is the union of:

1. A **static topology graph** (~250 triples) shared across all incidents.
2. **Per-incident alarm subgraphs** (~160 triples each, on average) containing alarm-location, temporal, and locality edges.

The downstream consumer is a **Graph Neural Network** (R-GCN over PyTorch Geometric) that treats `RootCause` as a node-classification task on `Alarm` nodes.

### Why not just use the raw alarm table?

The alarm table in `incidents.db` is flat — one row per alarm with scalar columns. A GNN needs **relational structure**: which alarms share a span, which tunnel carries which service, which port is connected to which. The KG materialises these relationships as explicit edges so the GNN's message-passing layers can traverse them.

---

## 2. Data Sources

| Source | File | What it provides |
|---|---|---|
| YANG topology | `data/yang/topology.json` | Optical spans, OTU pipes, termination points |
| YANG tunnels | `data/yang/tunnels.json` | OCh tunnels, ODU tunnels, service names, EROs |
| YANG inventory | `data/yang/inventory.json` | NEs, boards, ports, hardware hierarchy |
| Alarm dataset | `data/incidents.db` | Per-incident alarm cascades with location, severity, timestamps, root labels |
| Suppression edges | `data/alarm_suppression.json` | 50 expert-curated alarm→alarm suppression pairs |
| Alarm metadata | `data/alarms_detail.json` | Possible-cause text (used for L2 keyword expansion in the simulator) |

The topology files are already loaded by `src/simulator/yang_loader.py` into `Topology` objects. The KG builder reuses this loader directly.

---

## 3. KG Schema

### 3.1 Entity Types (KG Nodes)

Each entity gets a URI of the form `{type_prefix}:{identifier}` to avoid namespace collisions.

| Entity Type | URI Pattern | Source | Example |
|---|---|---|---|
| `Alarm` | `alarm:{incident_id}:{alarm_row_id}` | `incidents.db → alarms.id` | `alarm:42:3` |
| `Layer` | `layer:{name}` | constant | `layer:OTS` |
| `NE` | `ne:{ne_id}` | `inventory.json` | `ne:A` |
| `Board` | `board:{uuid}` | `inventory.json` | `board:A.OTU` |
| `Port` | `port:{uuid}` | `inventory.json` | `port:A.FIU.line_to_B` |
| `Span` | `span:{link_id}` | `topology.json` (optical-topo) | `span:span_OMS3_BC` |
| `OTUPipe` | `otu:{link_id}` | `topology.json` (otn-topo) | `otu:otu_OMS3_BC` |
| `OChTunnel` | `och:{name}` | `tunnels.json` (wdm-tunnel) | `och:och_ch2` |
| `ODUTunnel` | `odu:{name}` | `tunnels.json` (otn-tunnel) | `odu:odu_ch2` |
| `Service` | `svc:{name}` | `nfms:client-service-name` | `svc:ch2` |

### 3.2 Predicates (KG Edges)

Predicates are grouped by how they are produced.

#### Observed predicates — extracted directly from data

| Predicate | Subject → Object | Source | How extracted |
|---|---|---|---|
| `Hosts` | NE → Board, Board → Port | `inventory.json` | hierarchy via `parent` field |
| `Connected` | Port → Port | `topology.json` | optical link endpoints |
| `CarriedBy` | OChTunnel → Span | `tunnels.json` | WDM tunnel ERO hops |
| `CarriedBy` | OTUPipe → Span | `topology.json` | `supporting-link-id` |
| `RidesOver` | ODUTunnel → OTUPipe | `tunnels.json` | OTN tunnel ERO hops |
| `SupportingTunnel` | ODUTunnel → OChTunnel | `tunnels.json` | `supporting-tunnel` ref |
| `Carries` | ODUTunnel → Service | `tunnels.json` | `client-service-name` |
| `IsLayer` | Alarm → Layer | `alarms.layer` | direct column read |
| `AtNE` | Alarm → NE | `alarms.ne` | direct column read |
| `AtBoard` | Alarm → Board | `alarms.board` | direct column read |
| `AtPort` | Alarm → Port | `alarms.port` | direct column read |
| `AtSpan` | Alarm → Span | `alarms.container_id` | classified against `topo.optical_links` |
| `OnPipe` | Alarm → OTUPipe | `alarms.container_id` | classified against `topo.otn_links` |
| `AtChannel` | Alarm → OChTunnel or ODUTunnel | `alarms.container_id` | classified against `topo.wdm_tunnels` / `topo.otn_tunnels` |
| `Container` | Alarm → entity | `alarms.container_id` | fallback when type is board/port/other |

#### Derived predicates — computed from observed triples

| Predicate | Subject → Object | Condition |
|---|---|---|
| `Earlier` | Alarm → Alarm | `t_obs(a1) < t_obs(a2)` within same incident |
| `Close` | Alarm → Alarm | `|t_obs(a1) − t_obs(a2)| ≤ 5 min` |
| `WithinSix` | Alarm → Alarm | `|t_obs(a1) − t_obs(a2)| ≤ 6 min` |
| `OnSameNE` | Alarm → Alarm | `a1.ne == a2.ne` |
| `OnSameBoard` | Alarm → Alarm | `a1.board == a2.board` |
| `OnSamePort` | Alarm → Alarm | `a1.port == a2.port` |
| `OnSameContainer` | Alarm → Alarm | `a1.container_id == a2.container_id` |
| `SameSpan` | Alarm → Alarm | both container_id values are spans and equal |
| `OnSameLink` | Alarm → Alarm | both alarms' ports are Connected in the topology |

#### Target predicates — the labels the ML model predicts

| Predicate | Subject → Object | Meaning |
|---|---|---|
| `RootCause` | Alarm → Alarm (self) | This alarm is the root cause of its incident |
| `Secondary` | Alarm → Alarm | First alarm is a derived symptom of the second |
| `SameIncident` | Alarm → Alarm | Both alarms belong to the same root-cause event |
| `Correlated` | Alarm → Alarm | At least one of R1–R4 fires on this pair (with weight) |

### 3.3 Container-ID Classification

The `container_id` column in the `alarms` table can hold a span ID, OTU pipe ID, tunnel name, port UUID, or board UUID. The builder classifies it by checking membership in the `Topology` object's dictionaries:

```python
def classify_container(container_id: str, topo: Topology) -> str:
    if container_id in topo.optical_links:   return "span"
    if container_id in topo.otn_links:       return "otu"
    if container_id in topo.wdm_tunnels:     return "och"
    if container_id in topo.otn_tunnels:     return "odu"
    if container_id in topo.ports:           return "port"
    return "other"
```

Classification determines which predicate to emit (`AtSpan`, `OnPipe`, `AtChannel`, or generic `Container`).

---

## 4. Pipeline Architecture

```
                        ┌─────────────────────────────────────────┐
                        │          YANG Instance Files            │
                        │  topology.json, tunnels.json,           │
                        │  inventory.json                         │
                        └──────────────┬──────────────────────────┘
                                       │ yang_loader.load()
                                       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                         KG Builder Pipeline                                  │
│                                                                              │
│  ┌─────────────────┐   ┌─────────────────┐   ┌───────────────────────────┐   │
│  │ Stage 1         │   │ Stage 2         │   │ Stage 3                  │   │
│  │ Topology        │   │ Alarm Facts     │   │ Derived Predicates       │   │
│  │ Extraction      │   │ Extraction      │   │                          │   │
│  │                 │   │                 │   │  • Temporal (Earlier,    │   │
│  │ Hosts,          │   │ IsLayer, AtNE,  │   │    Close, WithinSix)     │   │
│  │ Connected,      │   │ AtBoard, AtPort,│   │  • Locality (OnSameNE,  │   │
│  │ CarriedBy,      │   │ AtSpan, OnPipe, │   │    OnSamePort, SameSpan) │   │
│  │ RidesOver,      │   │ AtChannel,      │   │                          │   │
│  │ SupportingTunnel│   │ Container,      │   │  Optimisation: compact   │   │
│  │ Carries         │   │ RootCause,      │   │  flag when all-pairs     │   │
│  │                 │   │ Secondary       │   │  are within threshold    │   │
│  │ ~250 triples    │   │ ~8 triples /    │   │                          │   │
│  │                 │   │ alarm row       │   │  ~400 triples / incident  │   │
│  └────────┬────────┘   └────────┬────────┘   └────────────┬─────────────┘   │
│           │                     │                          │                  │
│           └─────────────────────┼──────────────────────────┘                  │
│                                 ▼                                            │
│                    ┌────────────────────────┐                                │
│                    │ Triple Store           │                                │
│                    │ (subject, predicate,   │                                │
│                    │  object, source,       │                                │
│                    │  weight, incident_id)  │                                │
│                    └────────────┬───────────┘                                │
└─────────────────────────────────┼────────────────────────────────────────────┘
                                  │
                                  ▼
                    ┌──────────────────────────────┐
                    │ Output                       │
                    │  topology_triples.csv (~250) │
                    │  alarm_triples.parquet (~14M)│
                    │  per-incident subgraphs      │
                    │  incident_manifest.csv       │
                    └──────────────────────────────┘
```

### Stage 1: Topology Extraction

Runs once. Reads the `Topology` object from `yang_loader.load()` and emits one triple per entity relationship. The topology graph is shared across all incidents.

### Stage 2: Alarm Fact Extraction

Runs per incident. Reads alarm rows from `incidents.db` and emits:
- **Location triples** (IsLayer, AtNE, AtBoard, AtPort, AtSpan/OnPipe/AtChannel)
- **Label triples** (RootCause for `is_root=1` alarms; Secondary linking each non-root alarm to its group's root via `group_id`)

### Stage 3: Derived Predicates

Runs per incident. Computes pairwise temporal and locality predicates between alarms within the same incident.

**Temporal optimisation**: For simulator-generated data, the physical propagation time is under 200ms and polling jitter is at most 5 minutes per layer. This means virtually all alarm pairs within a single incident satisfy `Close` and `WithinSix`. Instead of materialising N^2 edges, the builder emits a compact metadata triple `(incident:{sid}, AllClose, true)` and lets downstream consumers assume all intra-incident pairs satisfy both predicates. Full pairwise materialisation is available for real-world data where temporal windows actually discriminate.

---

## 5. Output Format

### 5.1 Triple Store

The canonical on-disk format is a flat table with one row per triple:

```
subject, predicate, object, source, weight, incident_id
```

| Column | Type | Description |
|---|---|---|
| `subject` | string | Entity URI (e.g. `alarm:42:3`, `ne:A`) |
| `predicate` | string | Relation name (e.g. `Hosts`, `AtSpan`, `Close`) |
| `object` | string | Entity URI |
| `source` | string | Provenance: `topology`, `alarm`, `derived`, `rule` |
| `weight` | float | Confidence (1.0 for deterministic; 0.1–0.4 for R1–R4) |
| `incident_id` | int | 0 for topology triples; >0 for incident-specific triples |

Files produced:

| File | Format | Content | Approximate size |
|---|---|---|---|
| `data/kg/topology_triples.csv` | CSV | ~250 topology triples | ~10 KB |
| `data/kg/alarm_triples.parquet` | Parquet | ~14M incident triples | ~500 MB |
| `data/kg/incident_manifest.csv` | CSV | One row per incident: id, fault_type, n_alarms, n_root | ~1 MB |

### 5.2 NetworkX Graph

For interactive exploration and small-scale experiments, the builder can construct a `nx.MultiDiGraph` where:
- Node attribute `entity_type` stores the type (`NE`, `Alarm`, `Span`, etc.)
- Edge attributes store `predicate`, `source`, `weight`

The full graph (~14M edges) does not fit in memory. The builder provides a `load_incident_subgraph(incident_id)` function that returns the topology graph plus one incident's alarm subgraph — typically ~500 nodes and ~1000 edges.

### 5.3 PyTorch Geometric HeteroData (for Phase 4)

The builder will provide a `to_pyg_heterodata()` converter that maps the KG into PyG's `HeteroData` format:

- **Node types**: `Alarm`, `NE`, `Board`, `Port`, `Span`, `OTUPipe`, `OChTunnel`, `ODUTunnel`, `Service`, `Layer`
- **Edge types**: `(src_type, predicate, dst_type)` — e.g. `(Alarm, AtSpan, Span)`, `(OChTunnel, CarriedBy, Span)`
- **Node features**: severity one-hot (Alarm), layer one-hot (Alarm), entity-type embedding
- **Labels**: `RootCause` as a binary mask on `Alarm` nodes

Each incident becomes one `HeteroData` object. A `DataLoader` iterates over the incident manifest.

---

## 6. Scale Estimates

Based on the current v3 dataset (`data/incidents.db`, ~25,600 incidents, ~505,000 alarm rows):

| Component | Per incident | Total |
|---|---|---|
| Topology triples | — | ~250 |
| Alarm-location triples | ~8 × 20 alarms = 160 | ~8M |
| Label triples (RootCause + Secondary) | ~20 | ~512K |
| Temporal triples (pairwise) | ~190 pairs | ~5M (or 1 compact flag) |
| Locality triples (pairwise) | ~190 pairs | ~5M |
| **Grand total** | ~530 | **~14M (or ~9M with compact flags)** |

Memory: Topology graph fits in ~1 MB. Per-incident subgraphs fit in ~50 KB. The full triple store as Parquet is ~500 MB.

---

## 7. Module Structure

All code goes under `src/kg/`:

```
src/kg/
    __init__.py           Package marker; exports build_kg()
    schema.py             Triple dataclass, entity URI helpers, predicate sets,
                          classify_container()
    topology_graph.py     build_topology_triples(topo) -> list[Triple]
    alarm_graph.py        build_alarm_triples(sid, rows, topo) -> list[Triple]
    temporal.py           derive_temporal_triples(sid, rows) -> list[Triple]
    locality.py           derive_locality_triples(sid, rows, topo) -> list[Triple]
    builder.py            Orchestrator: topology + alarm + derived -> full KG
    io.py                 CSV/Parquet write, NetworkX build, PyG export stub
    __main__.py           CLI: python -m src.kg build / inspect / export-pyg
```

Dependencies on existing modules:

- `src/simulator/yang_loader.py` → `load()` for the `Topology` object
- `src/simulator/topology.py` → data classes (`Topology`, `NE`, `Board`, `Port`, `Link`, `Tunnel`)
- `src/simulator/db.py` → `open_db()`, schema constants

No new external dependencies required beyond NetworkX (already standard in data science) and Pandas/PyArrow (already used for export).

---

## 8. CLI Interface

```
python -m src.kg build                              # full build
python -m src.kg build --db data/incidents.db    # specify DB
python -m src.kg build --max-incidents 100          # limit for testing
python -m src.kg build --no-derived                  # skip temporal/locality
python -m src.kg inspect topology                    # print topology stats
python -m src.kg inspect incident 42                 # show incident subgraph
python -m src.kg stats                               # triple count summary
```

---

## 9. Implementation Phases

| Phase | Deliverable | Estimate |
|---|---|---|
| **C-1** | Skeleton + schema + topology extraction | 1–2 days |
| **C-2** | Alarm fact extraction (Stage 2) | 2–3 days |
| **C-3** | Derived predicates (temporal + locality, Stage 3) | 1–2 days |
| **C-4** | Output (CSV/Parquet + NetworkX + inspect CLI) | 1–2 days |
| **C-5** | PyG HeteroData export | 2–3 days |
| **C-6** | Full-scale build + optimisation | 1–2 days |

---

## 10. Downstream Consumption

### For the GNN (Phase 4)

Each incident subgraph becomes one training sample. The GNN sees:
- **Input**: the heterogeneous graph (topology + alarm nodes + edges)
- **Task**: node-level binary classification — predict `RootCause` on each `Alarm` node
- **Supervision**: the `is_root` column from the simulator (ground truth)
- **Features**: alarm name embedding, severity one-hot, layer one-hot, topology distance features

### For the Rule Engine (Phase 3, parallel)

The rule engine (Design/08) operates on the same triple set. It applies deterministic rules (layer cascade, equipment trump) and weighted rules (R1–R4) to produce `Secondary` and `Correlated` edges. These can be:
- Added to the KG as additional edges (enriching the GNN's input)
- Compared against the GNN's predictions (for explainability)

### For the Analyst

The `inspect` CLI and CSV output allow manual exploration:
- "Which alarms share a span with `OTS_LOS`?"
- "Show all alarms in incident 42 that are at layer OTU or higher."
- "List all OCh tunnels that traverse `span_OMS3_BC`."

---

## 11. Open Questions

1. **Temporal edge materialisation** — emit compact `AllClose` flags for simulator data (recommended) or always materialise pairwise? Compact is much smaller but loses the absolute time deltas that some GNN architectures can use as edge features.
2. **Suppression edges in the KG** — include the 50 edges from `alarm_suppression.json` as `Suppresses` predicates? They are currently only used by `engine.apply_suppression()` but could enrich the KG.
3. **Node features for non-alarm entities** — NE, Board, Port are currently featureless. Should we add topology-derived features (degree, distance-to-nearest-fault) at KG build time or let the GNN compute them via message passing?
4. **Multi-root incidents** — the v3 simulator supports `/api/trigger_multi` with `group_id` tags. Should the KG builder handle `group_id` to produce per-root `Secondary` edges, or treat the whole incident as one incident?
