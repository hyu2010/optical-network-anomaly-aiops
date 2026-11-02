# Knowledge-Graph-Based RCA — Concepts, Tech Survey, and Project Plan

Status: **Plan — KG builder still pending (#5 / #7 in the project task list).** The schema, storage, and rule design described here are unchanged by the v3 cut-over. The two project-state assumptions to refresh when reading: (a) the simulator output the KG will consume now comes from the **v3 lab-mesh** topology (6 hubs A–F, 8 OMS spans, 6 OCh tunnels — see `Design/07_lab_mesh_topology.md`) — `svc_A` / `svc_B` examples in this doc become `ch2` / `ch4` over `span_OMS3_BC`; (b) alarm-name authority is the **OSN 9800** catalog with 8800 fallback (see `Design/05_fault_event_catalog.md` §8). Some of the KG building blocks have already shipped inside the six-agent runtime — `fault_management_agent/knowledge-engine/knowledge_gateway.py` builds an ad-hoc KG over the v3 topology + an alarm set as a step toward the full design here.
Inputs read: `Library/KnowlegeGraphFormat.txt`, `Library/RuleFormat.txt`, `Library/WorkFlowForKGBasedMLEngineTraining.txt`, plus a domain literature scan (see §9 Sources).

This doc is in two parts. **Part A** is a primer on knowledge graphs and the techniques that ride on top of them, written for someone seeing the area for the first time. **Part B** maps everything onto our specific project: the schema we will build, the storage we will use, the rules and ML approach we will use, and a phased plan.

---

# Part A — Primer

## 1. What is a knowledge graph?

A **knowledge graph (KG)** is a graph whose nodes are *entities* (things — alarms, ports, NEs, services) and whose edges are *typed relations* between them (`AtPort`, `Connected`, `CarriedBy`, `IsLayer`, …). Stripped to its formal core, a KG is a set of **triples** of the form

```
( subject , predicate , object )
```

For example, from `Library/KnowlegeGraphFormat.txt`:

```
Alarm_101    IsLayer       OTS
Alarm_101    AtPort        NE_1_port_1
Alarm_104    AtChannel     OCH_5
OCH_5        CarriedBy     OMS_A_B
NE_1_port_1  Connected     NE_2_port_2
```

Each triple is a *fact*. The entire KG is a directed labelled multigraph that can be traversed (`Alarm_104 → AtChannel → OCH_5 → CarriedBy → OMS_A_B`), queried (`give me all alarms at OTS layer that share a span`), and reasoned over (`if A and B both AtSpan X within 5 minutes, they are correlated`).

A KG differs from a plain database in three useful ways:

1. **Schema-light, relation-rich.** You don't need to know all relations upfront; you can add new predicates as you discover them. Each fact is locally typed.
2. **Multi-source by design.** Facts from different sources can sit side by side as long as the entities have shared identities. In our project, the same NE port appears in alarms (from logs), in inventory (from `inventory.json`), and in topology (from `topology.json`) — the KG is what stitches them together.
3. **A natural substrate for reasoning.** Logical rules and graph algorithms operate directly on the triple set without an ORM or join schema.

## 2. Two graph flavours: RDF vs. property graph

There are two formal traditions for storing KGs.

| | **RDF triple store** (e.g. Apache Jena, GraphDB) | **Property graph** (e.g. Neo4j) |
|---|---|---|
| Atom of data | a triple `(s, p, o)` | a node-with-properties / edge-with-properties |
| Edge identity | edges of the same type are anonymous and indistinguishable | each edge has its own id and can carry properties |
| Query language | SPARQL | Cypher |
| Reasoning | OWL / RDFS inferencing built in | bring your own |
| Standardisation | W3C standards (RDF, OWL, SHACL) | de-facto (no formal standard) |
| Sweet spot | open-world knowledge integration, ontologies | operational queries, ML pipelines |

`KnowlegeGraphFormat.txt` is written **as RDF triples**. That's the natural way to *describe* the graph. But for *implementation*, property graphs are more practical for ML pipelines — relationships can carry timestamps, weights, `is_root` flags, etc. without reification gymnastics.

**Our project will model conceptually as triples (matching the file) and store as a property graph — the engineering convenience side of the trade-off.** See §6.1 for storage choice.

## 3. What you can do with a KG once you have one

Three distinct *reasoning paradigms* sit on top of a KG. They aren't mutually exclusive — most modern systems combine all three.

### 3.1 Symbolic reasoning (rules)

Express domain knowledge as logical rules and apply them to the triples. Two common dialects:

- **Datalog** — "for-each-fact" inference, deterministic, fast. Good for hard rules like "OTS_LOS suppresses MUT_LOS at the same NE".
- **First-Order Logic (FOL)** — what `Library/RuleFormat.txt` uses. More expressive (variables, quantifiers), often translated to Datalog or Prolog for execution.

Pros: explainable (you can show *which* rule fired), no training data needed, fast.
Cons: humans have to write the rules; rules don't capture statistical patterns.

### 3.2 KG embeddings

Each entity and each relation is mapped to a vector (typically 50–300 dimensions). Triples are scored by an algebraic operation on those vectors; high score = "this fact is plausible".

Common models:

| Model | Idea | Strengths |
|---|---|---|
| **TransE** | relation = translation: `head + relation ≈ tail` | simple, fast |
| **DistMult** | relation = diagonal matrix multiplication | works for symmetric relations |
| **ComplEx** | uses complex numbers, captures asymmetric relations | better than DistMult for asymmetry |
| **RotatE** | relation = rotation in complex space | captures symmetry, antisymmetry, inversion, composition |

Trained on observed triples; used at inference for **link prediction** ("does this candidate triple hold?"). The big payoff: a model that completes missing links — including *missing causal links* between alarms.

Pros: data-driven, captures patterns the rule-writers missed.
Cons: not directly explainable; needs many triples to train well.

### 3.3 Graph Neural Networks (GNN)

A GNN learns a vector representation for each node by **passing messages along edges** and aggregating across neighbours, repeated for several layers. The node vectors then feed a classifier — root cause vs. secondary alarm, for instance.

Variants relevant to us:

- **GCN** — uses a single shared weight per layer; treats all edges identically.
- **R-GCN (Relational GCN)** — uses *one weight matrix per relation type*. Crucial for KG-style heterogeneous graphs where `AtPort`, `AtChannel`, `Connected` should be processed differently.
- **GAT (Graph Attention Network)** — learns per-edge weights via attention. Useful when some neighbours matter more than others (e.g., the OTS-layer alarm matters more than a fan alarm when the cascade's root is fiber-related).

In recent telecom RCA literature there's an entire family of "alarm propagation GNNs" (APGNN) and GAT-based root-alarm detectors specifically for **optical transport networks** — see §9 for citations.

Pros: learns from data, handles cascades naturally, state-of-the-art accuracy.
Cons: opaque without extra explanation methods; needs labelled training data — which is what our simulator produces.

### 3.4 Neuro-symbolic — combining the three

The strongest published systems use all three: rules for guarantees and explanations, embeddings for missing-link completion, GNN for prediction. A few specific neuro-symbolic frameworks:

- **Markov Logic Network (MLN)** — every FOL rule has a learned weight; it's a probabilistic logic that becomes a Markov random field.
- **DeepProbLog / Scallop** — differentiable Datalog/Prolog that can be plugged into a neural training loop end-to-end.
- **Rule-distilled GNN** — train a GNN, then extract symbolic rules from it for explainability.

For our project the natural fit is **rules first, GNN second, embeddings as an upgrade** — see §7.

## 4. The KG-based RCA loop, in one picture

```
┌──────────────────┐      ┌────────────────────────┐      ┌─────────────────────┐
│ data sources     │      │ KG construction        │      │ inference engine    │
│                  │      │ (data consolidation +  │      │ (rules + ML model)  │
│ • topology YANG  │      │  fact extraction)      │      │                     │
│ • tunnels YANG   │ ──▶  │ produces (s,p,o)       │ ──▶  │ ranks each alarm    │
│ • inventory YANG │      │ triples                │      │ as Root / Secondary │
│ • alarm logs     │      │                        │      │                     │
└──────────────────┘      └────────────────────────┘      └─────────────────────┘
                                       ▲                            ▲
                                       │                            │
                            human-knowledge ingest          human-knowledge fine-tune
                            (typed predicates,              (additional FOL rules,
                            ontology constraints)           Markov weights)
```

This is exactly the workflow `Library/WorkFlowForKGBasedMLEngineTraining.txt` describes.

---

# Part B — How this maps onto our project

## 5. The KG schema we will build

Read directly from `Library/KnowlegeGraphFormat.txt`. Generalising the example:

### 5.1 Entity types (KG nodes)

| Type | Source | Examples |
|---|---|---|
| `Alarm` | alarm logs (simulator's `incidents.db`) | `Alarm_101`, `Alarm_102` … |
| `Layer` | constants | `OTS`, `OMS`, `OCH`, `OTU`, `ODU`, `client` |
| `NE` | `inventory.json` | `ne1`, `ne2`, `ne3`, `ne4` |
| `Port` | `inventory.json` (component class = `port`) | `ne1.FIU.line_to_ne2`, `ne2.OTU.line_rx_a` |
| `Board` | `inventory.json` (component class = `module`) | `ne1.OTU`, `ne2.FIU`, … |
| `Span` | `topology.json` (`optical-topo` link) | `span_12`, `span_23` … |
| `OTUPipe` | `topology.json` (`otn-topo` link) | `otu-12-a`, `otu-23-a` … |
| `OChTunnel` | `tunnels.json` (`ietf-wdm-tunnel:wdm-tunnel`) | `och-A`, `och-B` |
| `ODUTunnel` | `tunnels.json` (`ietf-otn-tunnel:otn-tunnel`) | `odu-A`, `odu-B` |
| `Service` | `tunnels.json` `nfms:client-service-name` | `svc_A`, `svc_B` |

### 5.2 Predicate set (KG edges)

The predicates in `KnowlegeGraphFormat.txt` plus a few we'll add:

| Predicate | Sub → Obj | Source | Notes |
|---|---|---|---|
| `IsLayer`        | Alarm → Layer    | alarm logs | which OTN/SDH layer the alarm fired on |
| `AtPort`         | Alarm → Port     | logs + inventory | physical location |
| `AtChannel`      | Alarm → OChTunnel | logs + tunnels | which optical channel |
| `AtSpan`         | Alarm → Span     | logs + topology | which fiber span |
| `OnPipe`         | Alarm → OTUPipe  | logs + topology | which OTU pipe (for OTU/ODU layer alarms) |
| `Connected`      | Port → Port      | topology | the directed fiber link between two ports |
| `CarriedBy`      | OChTunnel → Span (×N) | tunnels | the WDM tunnel's ERO; one triple per hop |
| `RidesOver`      | ODUTunnel → OTUPipe (×N) | tunnels | the ODU tunnel's ERO |
| `SupportingTunnel` | ODUTunnel → OChTunnel | tunnels | which OCh carries this ODU |
| `Carries`        | ODUTunnel → Service | `nfms:client-service-name` | the client service tag |
| `Hosts`          | NE → Board, Board → Port | inventory | physical hierarchy |
| `Earlier`        | Alarm → Alarm    | derived from logs | a₁ before a₂ |
| `Close`          | Alarm → Alarm    | derived from logs | within 5 minutes |
| `WithinSix`      | Alarm → Alarm    | derived from logs | within 6-minute group span |
| `SameSpan`       | Alarm → Alarm    | derived | both have `AtSpan X` for the same `X` |
| `OnSameLink`     | Alarm → Alarm    | derived | both have `AtPort` whose ports are connected |
| `RootCause`      | Alarm            | **target — predicted** | unary "this is the root" |
| `Secondary`      | Alarm → Alarm    | **target — predicted** | "a is a secondary of b" |
| `SameIncident`   | Alarm → Alarm    | **target — predicted** | "these alarms belong to the same root event" |

The last three are **the predictions our model produces.** Everything else is observed input or derived from rules.

## 6. Tech choices

### 6.1 Storage: NetworkX → Neo4j

| Phase | Pick | Why |
|---|---|---|
| v1 (prototype) | **NetworkX** (Python, in-memory, multidigraph) | zero install, fits on a laptop, perfect for ≤ 100K triples. |
| v2 (real datasets) | **Neo4j** (free, official Python driver) | scales beyond memory, good Cypher tooling, GNN libraries (e.g. PyG, DGL) have direct loaders. |
| v3 (rigour) | **Apache Jena / GraphDB** if we ever need OWL inferencing or SPARQL endpoints | likely never, but documented for completeness. |

Either way, the on-disk artefact is **a triples table** (CSV/Parquet `subject, predicate, object, source, timestamp`) — that table is the canonical KG. NetworkX/Neo4j are just views over it.

### 6.2 Rule engine — for `RuleFormat.txt`

The rules in `RuleFormat.txt` are written in FOL. Three viable runtimes:

- **Pure Python** matcher over the triples table. Fine for ≤ 50 rules. Trivial to debug.
- **pyDatalog** — Python Datalog with backward chaining. Good for the layer-hierarchy rules (deterministic, recursive).
- **Markov Logic Network** (PyMLN, ProbCog) — when we want **learned weights** on rules, not just hard fire/no-fire.

**Plan for v1: pure-Python matcher** with each rule implemented as a function that takes the triples table and returns inferred triples. The output is itself a set of triples (e.g. `Alarm_X SameIncident Alarm_Y`) appended to the KG. This keeps the entire pipeline transparent.

### 6.3 ML: progression GNN → embeddings → neuro-symbolic

Three increasingly sophisticated tiers:

| Tier | Model | What it predicts | Library |
|---|---|---|---|
| **T1 baseline** | rules-only (no learning) | `SameIncident`, `Secondary`, `RootCause` from the FOL rules | none |
| **T2 GNN** | **R-GCN** over the heterogeneous KG | `RootCause(a)` as a node-classification head | **PyTorch Geometric** or **DGL** |
| **T3 hybrid** | T2 + KG embedding (RotatE) for missing-link completion | predicts unseen propagation edges | DGL-KE |
| **T4 neuro-symbolic** *(future)* | T2 + soft FOL rules as a Markov layer | weighted rules learned from data | Scallop / DeepProbLog |

**Recommendation:** start at **T1** — pure rules over the simulator-generated triples. This produces a working baseline with explainable output, validates the KG schema end-to-end, and gives us the labels we'll need to train T2. Move to T2 once the rules are stable and the simulator has produced ≥ 1000 incidents.

The OTN-specific reference implementation worth studying: **"Joint Alarm Correlation and Root Alarm Detection in Optical Transport Networks with Graph Attention"** (GitHub repo in §9). It uses GAT, reports F1 ≈ 0.92 on alarm correlation and ≈ 97% accuracy on root detection. Their model architecture is a sensible blueprint for our T2.

## 7. End-to-end pipeline (what we will build, in order)

```
┌─────────────┐  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐  ┌──────────────┐
│ simulator   │→ │ KG builder      │→ │ rule engine     │→ │ training         │→ │ inference    │
│             │  │ (yang+logs →    │  │ (apply RuleFmt) │  │ (R-GCN on KG)   │  │ (predict     │
│ incidents.db│  │  triples.csv)   │  │ adds derived   │  │                 │  │  RootCause)  │
└─────────────┘  └─────────────────┘  │ triples         │  └─────────────────┘  └──────────────┘
       ▲                              └─────────────────┘
       │
   GUI → triggers fault → records alarms → loops back
```

Components, mapped to source code:

| Component | Module path | Stage |
|---|---|---|
| simulator (already designed) | `src/simulator/` | Phase 2 (this project) |
| KG builder | `src/kg/builder.py` | Phase 3 |
| rule engine | `src/kg/rules.py` | Phase 3 |
| GNN training | `src/ml/train_rgcn.py` | Phase 4 |
| inference + explanation | `src/ml/infer.py` | Phase 4 |
| evaluation | `src/ml/eval.py` | Phase 4 |

## 8. Phased plan (incremental, demo at every step)

| Phase | Deliverable | Why this order |
|---|---|---|
| **A** *(done)* | YANG instance files for the diamond+crossbar topology | foundation — every other piece depends on it |
| **B** | simulator (faults → alarm cascade → `incidents.db`) | gives us labelled data |
| **C** | **KG builder** — load 3 YANGs + incidents.db → `triples.csv`; write tests that the schema in §5 is fully populated | turns raw data into the KG |
| **D** | **rule engine** — implement the FOL rules in `RuleFormat.txt` over the triples; output `SameIncident`, `Secondary`, `RootCause` | T1 baseline, fully explainable |
| **E** | **GNN training** — build heterogeneous KG in PyG/DGL, train R-GCN to predict `RootCause`; evaluate vs. T1 baseline | data-driven uplift |
| **F** | **inference + explanation** — at inference time, return both the predicted root and the rule chain or attention weights that justify it | production-quality output |
| **G** *(optional)* | **neuro-symbolic upgrade** — add learned weights on rules (MLN) or differentiable Datalog (Scallop) | when the simple GNN plateaus |

Phases C/D can run in parallel with the simulator's fault catalogue extension, since the KG schema is fixed.

## 9. Sources (literature scan, April 2026)

These are the papers / code repos I would recommend reading first.

- **Domain — KG + RCA for telecom networks**
  - [Beyond Correlation: AWS network digital twin graph + agentic AI](https://aws.amazon.com/blogs/database/beyond-correlation-finding-root-causes-using-a-network-digital-twin-graph-and-agentic-ai/) — the modern industry blueprint.
  - [Influence-based Root Cause Alarm Discovery in Telecom Networks (arXiv 2105.03092)](https://ar5iv.labs.arxiv.org/html/2105.03092)
  - [Telco Knowledge Graphs in 2026 — TheFastMode](https://www.thefastmode.com/expert-opinion/47580-autonomous-networks-need-shared-understanding-why-telco-knowledge-graphs-will-matter-in-2026)
  - [Alarm Root Cause Analysis Based on KG and Bayesian Network (IEEE 2024)](https://ieeexplore.ieee.org/document/10729793/)
  - [KGroot: a KG-enhanced method for RCA (Expert Systems with Applications, 2024)](https://www.sciencedirect.com/science/article/abs/pii/S095741742401546X)
- **Domain — GNN / GAT for alarm RCA**
  - [APGNN: Alarm Propagation GNN for fault detection and alarm RCA (Computer Networks, 2022)](https://www.sciencedirect.com/science/article/abs/pii/S1389128622005199)
  - [Joint Alarm Correlation and Root Alarm Detection in Optical Transport Networks with Graph Attention — GitHub](https://github.com/LynchXLQ/Joint-Alarm-Correlation-and-Root-Alarm-Detection-in-Optical-Transport-Networks-with-Graph-Attention) — **closest to our problem; sensible reference architecture for our T2.**
- **KG embeddings**
  - [DGL-KE: Introduction to KG Embedding](https://aws-dglke.readthedocs.io/en/latest/kg.html)
  - [RotatE paper (ICLR 2019)](https://arxiv.org/pdf/1902.10197)
  - [Stanford CS224W tutorial — Simple Schemes for KG Embedding](https://medium.com/stanford-cs224w/simple-schemes-for-knowledge-graph-embedding-dd07c61f3267)
- **Neuro-symbolic / rule + ML hybrid**
  - [Neuro-symbolic AI — Wikipedia overview](https://en.wikipedia.org/wiki/Neuro-symbolic_AI)
  - [Neural-Symbolic Reasoning over Knowledge Graphs (KDD survey)](https://www.kdd.org/exploration_files/p124-Neural_Symbolic_KGR_survey.pdf)
- **RDF vs property graph**
  - [Neo4j: RDF triple stores vs. property graphs](https://neo4j.com/blog/rdf-vs-property-graphs-knowledge-graphs/)

## 10. Open questions for review

1. **Storage** — go straight to Neo4j, or keep v1 in-memory NetworkX as I'm proposing? (Neo4j adds an install + service; NetworkX is one Python import.)
2. **GNN library** — **PyTorch Geometric** (academic-friendly, more papers' code) vs **DGL** (better heterogeneous-graph tooling, Amazon-backed). I lean PyG; flag if you have a preference.
3. **Rule format** — keep `RuleFormat.txt` as the source-of-truth (humans edit it, `rules.py` parses it) vs. write rules directly in Python. The former is closer to the project's style; the latter is faster to iterate. I lean toward parser.
4. **Granularity of `Alarm` IDs** — every alarm fired in the simulator gets its own KG node? Or coarser (one node per (alarm-type, NE, port) tuple, multiple firings collapse into properties)? The literature mostly does per-firing.
5. **Embedding integration** — punt to T3 / never-do-it, or add early as a separate experimental track?

Once §10 is settled I'll start Phase C — the KG builder that turns the three YANG files plus `incidents.db` into `triples.csv`.
