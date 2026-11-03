# Knowledge-Graph Schema

The canonical structural reference for the materialised KG in `data/kg/`. For
prose, build instructions, and rationale see [`README.md`](README.md); this file
is the *spec* — node types, edge types, provenance, and how the layers bind.

---

## 1. The model

Every fact is one flat row:

```
(subject, predicate, object,  source, weight, incident_id)
└──────── the triple ───────┘ └──────── audit columns ────┘
```

There is no nesting. The graph emerges because URIs reference each other. Three
conceptual layers share the one store, separated by `incident_id`:

| Layer | File | `incident_id` |
|---|---|---|
| Static topology (shared by all incidents) | `topology_triples.csv` | `0` |
| Per-incident alarm facts + labels + correlations | `alarm_triples.csv` | `>0` |
| Per-incident PPE trace / anomaly facts | `ppe_triples.csv` | `>0` |
| Per-incident summary index (not triples) | `incident_manifest.csv` | — |

**One incident's subgraph** = all topology rows ∪ its `alarm_triples` rows ∪ its
`ppe_triples` rows (filter `incident_id ∈ {0, N}`).

### Audit columns

| Column | Meaning |
|---|---|
| `source` | Provenance: `topology` \| `alarm` \| `derived` \| `ppe` \| `rule:R1..R4`. The most important filter — separates fact / ground-truth / computed / heuristic. |
| `weight` | Confidence. `1.0` for deterministic facts; `0.1`–`0.4` for the weighted correlation rules. |
| `incident_id` | `0` for topology; the `incidents.db → incidents.id` otherwise. |

`alarm_triples.csv` and `ppe_triples.csv` add two convenience columns —
`subject_name` / `object_name` — carrying the human-readable alarm name when the
endpoint is an `alarm:` URI (empty otherwise). `topology_triples.csv` omits them.

---

## 2. Node types (entity URI scheme)

Every node is `{type_prefix}:{id}`. The prefix **is** the node's type (also the
node type for GNN consumption). 13 types in three families:

### 2.1 Physical hierarchy

| Type | URI pattern | Example | Source |
|---|---|---|---|
| `NE` | `ne:{id}` | `ne:A` | `inventory.json` |
| `Board` | `board:{uuid}` | `board:A.OTU` | `inventory.json` |
| `Port` | `port:{uuid}` | `port:A.FIU.line_to_B` | `inventory.json` |

### 2.2 Logical / optical overlay

| Type | URI pattern | Example | Source |
|---|---|---|---|
| `Span` | `span:{link_id}` | `span:span_OMS3_BC` | `topology.json` (optical-topo) |
| `OTUPipe` | `otu:{link_id}` | `otu:otu_OMS3_BC` | `topology.json` (otn-topo) |
| `OChTunnel` | `och:{name}` | `och:och_ch2` | `tunnels.json` (wdm-tunnel) |
| `ODUTunnel` | `odu:{name}` | `odu:odu_ch2` | `tunnels.json` (otn-tunnel) |
| `Service` | `svc:{name}` | `svc:ch2` | `nfms:client-service-name` |
| `Layer` | `layer:{name}` | `layer:OTS` | constant (OTS/OMS/OCH/OTU/ODU/SDH/client) |

### 2.3 Observations (per incident)

| Type | URI pattern | Example | Source |
|---|---|---|---|
| `Alarm` | `alarm:{incident_id}:{row_id}` | `alarm:42:3` | `incidents.db.alarms` |
| `PpeTrace` | `ppe:{incident_id}:{channel}:{span_id}` | `ppe:42:och_ch2:span_OMS3_BC` | `incidents.db.ppe_traces` |
| `PpeAnomaly` | `ppe_anom:{incident_id}:{channel}:{span_id}:{idx}` | `ppe_anom:42:och_ch2:span_OMS3_BC:0` | `incidents.db.ppe_anomalies` |
| `FibrePosition` | `fpos:{span_id}:{z_km*10}` | `fpos:span_OMS3_BC:94` (= 9.4 km) | derived from anomaly `z_km` |

> **URI identity.** `alarm:` / `ppe:` / `ppe_anom:` embed `incident_id` (the same
> row id recurs across incidents — the prefix keeps each instance globally
> unique). `fpos:` is **incident-agnostic**: `fpos:span_OMS3_BC:94` is shared by
> every anomaly landing at 9.4 km on that span, across all incidents (it is the
> position entity the GNN learns an embedding for).

---

## 3. Edge types (predicates) by provenance

### 3.1 `source = topology` — static structure

| Predicate | Subject → Object | Meaning |
|---|---|---|
| `Hosts` | NE → Board ; Board → Port | containment hierarchy |
| `Connected` | Port → Port | the two endpoints of one optical span (directed) |
| `CarriedBy` | OTUPipe → Span ; OChTunnel → Span | underlay (rides over) |
| `RidesOver` | ODUTunnel → OTUPipe | overlay hop |
| `SupportingTunnel` | ODUTunnel → OChTunnel | overlay binding |
| `Carries` | ODUTunnel → Service | client service realised |

### 3.2 `source = alarm` — extracted per alarm + ground-truth labels

| Predicate | Subject → Object | Meaning |
|---|---|---|
| `IsLayer` | Alarm → Layer | layer the alarm fires on |
| `AtNE` | Alarm → NE | NE it fired at |
| `AtBoard` | Alarm → Board | board (may be absent) |
| `AtPort` | Alarm → Port | port (may be absent) |
| `AtSpan` | Alarm → Span | container is a `span_*` id |
| `OnPipe` | Alarm → OTUPipe | container is an `otu_*` id |
| `AtChannel` | Alarm → OChTunnel/ODUTunnel | container is an `och_*`/`odu_*` tunnel |
| `Container` | Alarm → entity | fallback (port/board/other) |
| **`RootCause`** | Alarm → **itself** | **ground truth**: this alarm is the root (`is_root=1`) |
| **`Secondary`** | Alarm → its root Alarm | **ground truth**: this alarm is a symptom of that root |

`AtSpan` / `OnPipe` / `AtChannel` / `Container` are **mutually exclusive** — one
container edge per alarm.

### 3.3 `source = derived` — computed locality / fusion (weight 1.0)

| Predicate | Subject → Object | Condition |
|---|---|---|
| `OnSameNE` | Alarm → Alarm | same `ne` |
| `OnSameBoard` | Alarm → Alarm | same `board` |
| `OnSamePort` | Alarm → Alarm | same `port` |
| `OnSameContainer` | Alarm → Alarm | same `container_id` |
| `SameSpan` | Alarm → Alarm | both containers are the same span |
| `OnSameLink` | Alarm → Alarm | their ports are `Connected` peers |
| `AllClose` | incident → `"true"` | whole incident fits the 5-min Close window |
| `AllWithinSix` | incident → `"true"` | whole incident fits the 6-min window |
| `PpeIndicates` | PpeAnomaly → Alarm | anomaly spatially co-located with this alarm |

Locality edges are emitted once per unordered pair (`id_a < id_b`). Temporal
relations are compacted to the two incident-level flags rather than N² per-pair
edges (use `--materialise-temporal` at build time for explicit per-pair edges).

### 3.4 `source = rule:R1..R4` — weighted, **heuristic** correlation

| Source | Predicate | Subject → Object | Weight | Captures |
|---|---|---|---|---|
| `rule:R1` | `Correlated` | Alarm → Alarm | 0.1 | pair ≤ 5 min apart in a 6-min group |
| `rule:R2` | `Correlated` | Alarm → Alarm | 0.2 | OTU-layer + OCh-layer alarm on the same OTU pipe |
| `rule:R3` | `Correlated` | Alarm → Alarm | 0.4 | two ODU alarms sharing a client service |
| `rule:R4` | `Correlated` | Alarm → Alarm | 0.3 | two alarms whose OCh paths share a span |

> **Treat as weak evidence.** R1 fires on essentially every close-in-time pair, so
> it dominates row counts. Weight correlations; never call two alarms "the same
> incident" on R1 alone. (R3 doesn't fire in the v3 lab data — every ODU has a
> unique service.)

### 3.5 `source = ppe` (+ `topology`/`derived`) — Power Profile Estimation

| Predicate | Subject → Object | `source` | Meaning |
|---|---|---|---|
| `MeasuredOn` | PpeTrace → OChTunnel | `ppe` | trace reconstructed by this OCh's receiver |
| `MeasuresSpan` | PpeTrace → Span | `ppe` | trace covers this span end-to-end |
| `MeasuredBy` | PpeTrace → Board | `ppe` | the drop OTU board that produced it |
| `HasAnomaly` | PpeTrace → PpeAnomaly | `ppe` | one per anomaly on the trace |
| `LocatedAt` | PpeAnomaly → FibrePosition | `ppe` | quantised position along the fibre |
| `OnSpan` | FibrePosition → Span | `topology` | structural binding of a position to its span |
| **`PpeRootCause`** | PpeAnomaly → `"true"` | `ppe` | **ground truth**: this is the planted anomaly |
| `PpeIndicates` | PpeAnomaly → Alarm | `derived` | cross-modal: the alarms this anomaly explains |

Which edges fire depends on the anomaly `kind`: `step` / `slope` carry a position
(`LocatedAt` + `FibrePosition` + `OnSpan`); `null` / `rx_offset` have no `z_km`,
so only `HasAnomaly` (+ `PpeRootCause` if planted) fire.

---

## 4. How the layers bind (cross-layer structure)

In topology alone the physical hierarchy and the logical overlay are **separate
connected components** — they meet only through per-incident `Alarm` and
`PpeTrace` nodes. That is why graph queries spanning layers need an `incident_id`.

```
   PHYSICAL                                       LOGICAL / OPTICAL
   ne:C ─Hosts─► board:C.OTU ─Hosts─► port:C.FIU.line_from_A
        │                                   │
        │                       span:span_OMS1_AC ◄─CarriedBy─ och:och_ch1
        │                          ▲    ▲                ▲
        │              MeasuresSpan │    │ OnSpan         │ SupportingTunnel
        │                          │    │                │
   ┌────┴──── alarm:1:2 (IN_PWR_LOW) ───┼────────────── odu:odu_ch1 ─Carries─► svc:ch1
   │   AtNE/AtBoard/  │ AtSpan           │
   │   AtPort         │ ▲          fpos:span_OMS1_AC:703 (=70.3 km)
   │                  │ │ PpeIndicates    ▲
   │                  │ │                 │ LocatedAt
   │  ppe:1:och_ch1 ─HasAnomaly─► ppe_anom:1:och_ch1:0 ─PpeRootCause─► "true"
   │       │ MeasuredBy (board:C.OTU)
   └───────┘
            alarm:1:1 ◄─Secondary─ (every symptom points to its RootCause alarm)
```

An incident's structure, in words: a **root alarm** (`RootCause` self-loop) with
**`Secondary`** symptoms; each alarm pinned to topology via `At*` edges;
`Correlated` / `OnSame*` edges cross-linking alarms; and — for optical faults — a
**PPE trace** that `MeasuresSpan`, carries a **`PpeRootCause`** anomaly `LocatedAt`
a `FibrePosition` `OnSpan`, and `PpeIndicates` the alarms it explains.

---

## 5. Ground-truth labels

The KG carries **two parallel ground truths**, enabling both alarm-level and
spatial RCA:

| Label | Edge | Answers |
|---|---|---|
| `RootCause` | Alarm → itself | *which alarm* is the root (with `Secondary` symptoms) |
| `PpeRootCause` | PpeAnomaly → `"true"` | *which optical anomaly* — and via `LocatedAt`→`fpos`→km, *where on the span* |

Everything tagged `derived` or `rule:*` is auxiliary (locality features, noisy
correlations) — for a reasoner/GNN to weight or ignore.

---

## 6. `incident_manifest.csv` (per-incident index)

| Column | Meaning |
|---|---|
| `incident_id` | FK into `incidents.db → incidents.id` |
| `fault_type` | ground-truth fault class (`+`-joined for compound incidents) |
| `fault_target` | targeted span/NE/board/port id (or `R1:… \| R2:…` for compound) |
| `target_kind` | `link` \| `ne` \| `board` \| `port` \| `multi` |
| `suppressed` | `True` = operator-filtered view; `False` = raw cascade (both recorded per trigger) |
| `topology_id` | 12-char SHA-1 of the YANG topology (reproducibility) |
| `n_alarms` | alarm rows in this incident |
| `n_root` | alarms with `is_root=1` (1 for single-fault; ≥2 for compound) |
| `n_triples` | rows this incident contributes to `alarm_triples.csv` |
| `n_ppe_triples` | rows it contributes to `ppe_triples.csv` (`0` if built without `--with-ppe`) |

---

## 7. Not in the KG (kept in `incidents.db`)

- Raw PPE sample arrays (`samples_blob` / `baseline_blob`) — load on demand via
  `simulator/simulator/db.py::load_trace_samples`.
- PPE anomaly numerics (`magnitude_db`, detector confidence) — join on the
  `PpeAnomaly` URI's `(incident_id, channel, span_id, idx)` tuple.
- Alarm-name → alarm-name suppression edges — live in the simulator runtime
  (`engine.apply_suppression()`), not duplicated as graph edges.
- Explicit per-pair temporal edges — compacted to `AllClose` / `AllWithinSix`
  unless built with `--materialise-temporal`.

---

## 8. Quick reference — counts on the bundled dataset

Approximate row counts by `source` in the current `data/kg/` (varies with the
dataset that was generated):

```
derived    ~637k   (locality + PpeIndicates fan-out)
rule:R1    ~217k   (fires on every close pair — noisy)
alarm       ~61k   (location + RootCause/Secondary)
ppe         ~3.3k  (MeasuredOn/MeasuresSpan/MeasuredBy/HasAnomaly/LocatedAt/PpeRootCause)
rule:R2       ~550
topology      ~560 (static, incident_id=0)
rule:R4         few
```

Rebuild after a new dataset: `python -m simulator.kg build` (PPE on by default;
`--no-ppe` to skip, `--no-derived` to skip locality/temporal).

---

## See also

- [`README.md`](README.md) — full prose schema, rationale, loading recipes.
- `Design/04_knowledge_graph_plan.md` — conceptual design + ML survey.
- `Design/08_root_cause_rules.md` — the R1–R4 rule definitions.
- `Design/12_ppe_simulation.md` §8 — the PPE-triple flow.
- `simulator/kg/schema.py` — the builder's canonical predicate/URI definitions.
- `knowledge_engine/kg_abduction/` — the read surface over these files (`README.md` there).
