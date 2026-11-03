# Knowledge Graph — Output Files

This directory holds the materialised knowledge graph produced by
`python -m src.kg build`. The KG encodes everything a root-cause-analysis
(RCA) consumer — a rule engine today, a GNN tomorrow — needs to know about
the network topology and each alarm cascade.

The graph is stored as a **flat triples table**: every fact is a row of the
shape `(subject, predicate, object)` with three audit columns. That format
is friendly to both relational queries (SQL/pandas) and graph loaders
(NetworkX, PyTorch Geometric).

```
data/kg/
├── README.md                  (this file)
├── README.zh-CN.md            (Chinese translation)
├── topology_triples.csv       static topology facts (174 rows on v3 lab mesh)
├── alarm_triples.csv          per-incident alarm facts + labels + derived edges
├── ppe_triples.csv            per-incident PPE-trace / anomaly facts (Design/12 §8)
└── incident_manifest.csv      one row per incident, for quick lookup / filtering
```

---

## 1. Column schemas

### 1.1 `topology_triples.csv` (6 columns)

| Column        | Type   | Meaning                                                                                  |
|---|---|---|
| `subject`     | string | Entity URI of the fact's subject (e.g. `ne:A`, `board:A.OTU`).                           |
| `predicate`   | string | Relation name (e.g. `Hosts`, `Connected`, `CarriedBy`).                                  |
| `object`      | string | Entity URI of the fact's object.                                                         |
| `source`      | string | Always `topology` for this file.                                                         |
| `weight`      | float  | Always `1.0` for topology facts (deterministic).                                         |
| `incident_id` | int    | Always `0` (topology is shared across every incident).                                   |

### 1.2 `alarm_triples.csv` (8 columns)

| Column         | Type   | Meaning                                                                                  |
|---|---|---|
| `subject`      | string | Entity URI of the fact's subject (e.g. `alarm:42:3`, `ne:A`).                            |
| `subject_name` | string | Human-readable alarm name when the subject is an `Alarm` URI (e.g. `OTS_LOS`); empty otherwise — non-alarm URIs like `port:A.FIU.line_to_B` are already self-describing. |
| `predicate`    | string | Relation name (e.g. `IsLayer`, `AtSpan`, `RootCause`, `Correlated`).                     |
| `object`       | string | Entity URI of the fact's object.                                                         |
| `object_name`  | string | Human-readable alarm name when the object is an `Alarm` URI; empty otherwise.            |
| `source`       | string | Provenance tag: `alarm`, `derived`, or `rule:Rn` (where Rn is R1–R4).                    |
| `weight`       | float  | Confidence — `1.0` for deterministic facts, `0.1`–`0.4` for the four weighted rules.     |
| `incident_id`  | int    | The `incidents.id` this fact belongs to (always `>0` in this file).                      |

`(subject, predicate, object)` is the fact. `subject_name` and `object_name`
are convenience columns so a human reader of the CSV can see *what alarm*
each `alarm:{sid}:{row}` URI refers to without having to join against
`incidents.db`. Example row:

```
alarm:3:227,ODU_PM_SSF,Correlated,alarm:3:236,ODU_PM_SSF,rule:R4,0.3,3
```

reads as: in incident 3, the alarm-row 227 (an `ODU_PM_SSF` on `odu_ch4`)
is correlated with alarm-row 236 (an `ODU_PM_SSF` on `odu_ch2`) via rule R4
with weight 0.3.

`source` / `weight` / `incident_id` are bookkeeping that lets a consumer
filter by provenance, weight predictions by confidence, or select a single
incident's subgraph.

### 1.3 `ppe_triples.csv` (8 columns)

Same column shape as `alarm_triples.csv` so a single loader can read both:

| Column         | Type   | Meaning                                                                                  |
|---|---|---|
| `subject`      | string | Entity URI of the fact's subject — typically a `PpeTrace`, `PpeAnomaly`, or `FibrePosition` URI; the `OnSpan` rows have a `FibrePosition` here. |
| `subject_name` | string | Empty for PPE URIs (they are already self-describing — the channel and span are in the URI). |
| `predicate`    | string | One of the eight PPE predicates documented in §5.1.                                       |
| `object`       | string | Entity URI of the fact's object — can be an `OChTunnel`, `Span`, `Board`, `PpeAnomaly`, `FibrePosition`, `Alarm`, or the literal `"true"` (for `PpeRootCause`). |
| `object_name`  | string | Populated only when the object is an `Alarm` URI (i.e. on `PpeIndicates` rows) — same alarm-name lookup as `alarm_triples.csv`. |
| `source`       | string | One of `ppe`, `topology`, `derived` — see §5.3.                                            |
| `weight`       | float  | Always `1.0` in this file (PPE provenance is binary — either the receiver saw it or it didn't). |
| `incident_id`  | int    | The `incidents.id` this fact belongs to.                                                  |

---

## 2. Entity URI scheme

Every node uses a `{type_prefix}:{identifier}` URI so the ten entity
namespaces never collide. The prefix is also the node's "entity type" for
GNN consumption.

| Entity      | URI pattern                          | Example                          | Source                          |
|---|---|---|---|
| `Alarm`     | `alarm:{incident_id}:{alarm_row_id}` | `alarm:42:3`                     | `incidents.db.alarms.id`        |
| `Layer`     | `layer:{name}`                       | `layer:OTS`                      | constant (OTS, OMS, OCH, …)     |
| `NE`        | `ne:{ne_id}`                         | `ne:A`                           | `inventory.json`                |
| `Board`     | `board:{uuid}`                       | `board:A.OTU`                    | `inventory.json`                |
| `Port`      | `port:{uuid}`                        | `port:A.FIU.line_to_B`           | `inventory.json`                |
| `Span`      | `span:{link_id}`                     | `span:span_OMS3_BC`              | `topology.json` (optical-topo)  |
| `OTUPipe`   | `otu:{link_id}`                      | `otu:otu_OMS3_BC`                | `topology.json` (otn-topo)      |
| `OChTunnel` | `och:{name}`                         | `och:och_ch2`                    | `tunnels.json` (wdm-tunnel)     |
| `ODUTunnel` | `odu:{name}`                         | `odu:odu_ch2`                    | `tunnels.json` (otn-tunnel)     |
| `Service`   | `svc:{name}`                         | `svc:ch2`                        | `nfms:client-service-name` tag  |
| `PpeTrace`     | `ppe:{incident_id}:{channel}:{span_id}`            | `ppe:42:och_ch2:span_OMS3_BC`            | `incidents.db.ppe_traces`        |
| `PpeAnomaly`   | `ppe_anom:{incident_id}:{channel}:{span_id}:{idx}` | `ppe_anom:42:och_ch2:span_OMS3_BC:0`     | `incidents.db.ppe_anomalies`     |
| `FibrePosition`| `fpos:{span_id}:{z_km*10}`                         | `fpos:span_OMS3_BC:94` (= 9.4 km)        | derived from anomaly `z_km`      |

`Alarm` URIs include the incident id because the same alarm-row id can
re-appear across separate incidents; the incident prefix keeps every
alarm-instance globally unique. `PpeTrace` and `PpeAnomaly` URIs likewise
embed the incident id; `FibrePosition` is **incident-agnostic** — the same
`fpos:span_OMS3_BC:94` URI is shared by every anomaly that lands at 9.4 km
on that span, across all incidents. This is the entity the GNN learns a
position embedding for, so it deliberately collapses across time.

---

## 3. `topology_triples.csv`

The **static topology graph** — entity-to-entity relationships derived
from the three YANG instance files. ~174 triples on the v3 lab mesh;
identical for every incident, so we materialise it once with
`incident_id = 0`.

### Predicates

| Predicate          | Subject → Object               | Meaning                                                    | Source file             |
|---|---|---|---|
| `Hosts`            | NE → Board                     | The chassis hosts this board.                              | `inventory.json`        |
| `Hosts`            | Board → Port                   | The board hosts this port.                                 | `inventory.json`        |
| `Connected`        | Port → Port                    | Two ports are the endpoints of one optical span (directed).| `topology.json` (links) |
| `CarriedBy`        | OTUPipe → Span                 | This OTU pipe rides over this optical span.                | `supporting-link-id`    |
| `CarriedBy`        | OChTunnel → Span               | This OCh tunnel's ERO traverses this span.                 | wdm-tunnel ERO          |
| `RidesOver`        | ODUTunnel → OTUPipe            | This ODU tunnel rides over this OTU pipe (per ERO hop).    | otn-tunnel ERO          |
| `SupportingTunnel` | ODUTunnel → OChTunnel          | The OCh tunnel that carries this ODU.                      | `supporting-tunnel`     |
| `Carries`          | ODUTunnel → Service            | The client service this ODU realises.                      | `client-service-name`   |

### Why these (and not more)

These predicates capture the **layered structure** the RCA rules need:
- `Connected` is what R1 / peer-port deductions walk.
- `CarriedBy` + `RidesOver` + `SupportingTunnel` are the underlay/overlay
  edges R2 (OTU↔OCh) and R4 (OCh paths sharing a span) traverse.
- `Carries` is the only link that ties an ODU tunnel to a client service,
  needed for R3 (same client service).
- `Hosts` materialises the NE/board/port hierarchy so an alarm at port P
  can be reached from its NE via two graph hops.

We do **not** include alarm-name→alarm-name suppression edges from
`alarm_suppression.json` at this level. They live in the simulator's
runtime (`engine.apply_suppression()`); duplicating them as topology edges
would mix two different abstraction layers.

---

## 4. `alarm_triples.csv`

The **per-incident facts**. One block of rows per incident, with
`incident_id` matching `incidents.db → incidents.id`. Predicates fall into
four buckets, distinguishable by the `source` column:

### 4.1 `source = "alarm"` — direct extraction from the alarm row

| Predicate     | Subject → Object         | Meaning                                                    |
|---|---|---|
| `IsLayer`     | Alarm → Layer            | The layer the alarm fires on (OTS, OMS, OCH, …).          |
| `AtNE`        | Alarm → NE               | The NE where the alarm fires.                              |
| `AtBoard`     | Alarm → Board            | The board (may be absent for NE-level alarms).             |
| `AtPort`      | Alarm → Port             | The port (may be absent).                                  |
| `AtSpan`      | Alarm → Span             | Container is a `span_*` id.                                |
| `OnPipe`      | Alarm → OTUPipe          | Container is an `otu_*` id.                                |
| `AtChannel`   | Alarm → OChTunnel/ODUTunnel | Container is an `och_*` or `odu_*` tunnel name.        |
| `Container`   | Alarm → entity           | Fallback when the container is a port / board / other id.  |
| `RootCause`   | Alarm → Alarm (self)     | This alarm is the ground-truth root (`is_root=1` in DB).   |
| `Secondary`   | Alarm → Alarm            | Non-root alarm linked to its group's root via `group_id`.  |

`AtSpan` / `OnPipe` / `AtChannel` / `Container` are mutually exclusive —
the container classifier picks one based on which YANG dictionary the
`container_id` value lives in.

### 4.2 `source = "derived"` — computed from observed triples

| Predicate           | Subject → Object  | Condition                                                       |
|---|---|---|
| `OnSameNE`          | Alarm → Alarm     | Both alarms have the same `ne`.                                 |
| `OnSameBoard`       | Alarm → Alarm     | Same `board` uuid.                                              |
| `OnSamePort`        | Alarm → Alarm     | Same `port` uuid.                                               |
| `OnSameContainer`   | Alarm → Alarm     | Same `container_id`.                                            |
| `SameSpan`          | Alarm → Alarm     | Both `container_id` values are spans and equal.                 |
| `OnSameLink`        | Alarm → Alarm     | Their ports are `Connected` peers in the optical-topo.          |
| `AllClose`          | incident → "true" | The whole incident fits inside the 5-minute Close window.       |
| `AllWithinSix`      | incident → "true" | The whole incident fits inside the 6-minute WithinSix window.   |

Locality edges are emitted as one directed triple per unordered pair
(`alarm_id_a < alarm_id_b`) so the file isn't twice as large as needed.

**About the `AllClose` / `AllWithinSix` compact flags:** simulator-generated
cascades are physically very short (alarms fire within milliseconds; poll
jitter spreads them across at most a few minutes), so the entire alarm set
typically fits inside the rule R1 window. Instead of writing N² pairwise
`Close` / `WithinSix` edges (which would balloon the file to gigabytes),
we emit a single compact flag per incident. Downstream consumers may treat
every intra-incident alarm pair as `Close` / `WithinSix` when the
corresponding flag is `true`.

Pass `--materialise-temporal` to the builder if you need explicit per-pair
`Close` / `WithinSix` / `Earlier` edges (e.g. for a GNN that uses time
deltas as edge features).

### 4.3 `source = "rule:R1" / "rule:R2" / "rule:R3" / "rule:R4"`

The four weighted correlation rules from `Design/08_root_cause_rules.md`
§2 (verbatim from `Library/Alarm correlation logical rules.txt`):

| Source     | Predicate    | Subject → Object | Weight | What it captures                                      |
|---|---|---|---|---|
| `rule:R1`  | `Correlated` | Alarm → Alarm    | 0.1    | Alarm pair falls within a 6-minute group, ≤5 min apart. |
| `rule:R2`  | `Correlated` | Alarm → Alarm    | 0.2    | An OTU-layer and an OCh-layer alarm on the same OTU pipe. |
| `rule:R3`  | `Correlated` | Alarm → Alarm    | 0.4    | Two ODU-layer alarms whose ODUs share a client service. |
| `rule:R4`  | `Correlated` | Alarm → Alarm    | 0.3    | Two alarms whose carrying OCh paths share an optical span. |

R1 is emitted on every pair when the incident's `AllClose` flag is set —
that's the same condition the literal rule requires, just evaluated once.

**Note on R4:** Design/08 §7 states R4 at the OCH layer literally. The v3
simulator never emits OCH-layer alarms with OCh-tunnel containers (the
containers are spans or OTU pipes), so we resolve each alarm to its
carrying OCh via `SupportingTunnel` (for ODU-layer alarms) or via direct
OCh-tunnel containment (when present) and fire R4 when the resolved OChes
differ and share a span. The rule's intent — cross-service shared-span
correlation — is preserved. See `src/kg/correlation.py` for the exact
implementation.

R3 doesn't fire in v3 because every ODU in the default topology has a
unique `client-service-name`; it's wired up for future multi-segment
service support.

### 4.4 Why the four-bucket split (`alarm` / `derived` / `rule:Rn`)

The `source` column is what makes the file usable. A downstream consumer
can:
- Train a GNN on **just** `source = "alarm"` (ground-truth + location);
  treat `derived` and `rule:*` as auxiliary edge features.
- Compare a rule engine's predicted `RootCause` against the `alarm`-sourced
  `RootCause` ground truth for evaluation.
- Reweight or drop the rule edges (the simulator's data is noisy enough
  that some rule firings are uninformative — e.g. R1 fires on every pair
  in a 5-min incident).

---

## 5. `ppe_triples.csv`

Per-incident **Power Profile Estimation** (PPE) facts (Design/12 §8). One
block of rows per incident whose simulator run also emitted PPE traces
(i.e. when `--with-ppe` was on — the default). The file uses the same
8-column shape as `alarm_triples.csv`, so a single loader can read both.

A typical incident contributes 10–30 rows: three structural triples per
trace, plus per-anomaly triples, plus a fan-out of `PpeIndicates` edges to
co-located alarms.

### 5.1 The eight new predicates

| Predicate      | Subject → Object              | Source     | Weight | Meaning                                                                     |
|---|---|---|---|---|
| `MeasuredOn`   | PpeTrace → OChTunnel          | `ppe`      | 1.0    | The trace was reconstructed by the receiver of this OCh channel.            |
| `MeasuresSpan` | PpeTrace → Span               | `ppe`      | 1.0    | The trace covers this OMS span end-to-end.                                  |
| `MeasuredBy`   | PpeTrace → Board              | `ppe`      | 1.0    | Provenance — the drop OTU board (e.g. `board:C.OTU`) that produced the trace. |
| `HasAnomaly`   | PpeTrace → PpeAnomaly         | `ppe`      | 1.0    | One row per anomaly attached to the trace (both the planted ground truth and any detector-found anomalies). |
| `LocatedAt`    | PpeAnomaly → FibrePosition    | `ppe`      | 1.0    | The anomaly's quantised position along the fibre. Omitted for `null` / `rx_offset` anomalies (no `z_km`). |
| `OnSpan`       | FibrePosition → Span          | `topology` | 1.0    | A FibrePosition belongs to exactly one Span. Tagged `topology` because the binding is structural, but emitted per incident as positions are discovered. |
| `PpeIndicates` | PpeAnomaly → Alarm            | `derived`  | 1.0    | Cross-modal fusion edge: this anomaly is spatially co-located with this alarm in the same incident (same span container, or same OCh-channel container). |
| `PpeRootCause` | PpeAnomaly → `"true"`         | `ppe`      | 1.0    | Ground-truth label: this anomaly is the one the simulator *planted* (`is_root = 1` in `ppe_anomalies`). Parallels the existing `RootCause` self-loop on alarms. |

### 5.2 The three source tags in this file

| Source     | Predicates                                                                | Why this tag                                                                            |
|---|---|---|
| `ppe`      | `MeasuredOn`, `MeasuresSpan`, `MeasuredBy`, `HasAnomaly`, `LocatedAt`, `PpeRootCause` | First-class PPE facts read directly from `ppe_traces` / `ppe_anomalies`.                |
| `topology` | `OnSpan`                                                                  | Static binding from a FibrePosition to its parent Span — structural, not learned.       |
| `derived`  | `PpeIndicates`                                                            | Cross-modal heuristic, computed at build time by matching trace `span_id` to alarm `container_id`. |

A consumer that wants only the receiver-reported facts can filter
`source = "ppe"`; one that wants only the structural / inferred edges can
filter `source ∈ {"topology", "derived"}`. Same provenance pattern as
`alarm_triples.csv`.

### 5.3 The four PPE anomaly kinds → which predicates fire

Anomaly `kind` (from `ppe_anomalies.kind` in `incidents.db`) determines
whether the `LocatedAt` edge is emitted, because some anomalies have no
spatial position:

| Anomaly `kind` | `LocatedAt` emitted? | Typical fault                                                          |
|---|---|---|
| `step`         | yes                  | fibre cut (~25 dB), board power-off, NE power-off, dirty connector     |
| `slope`        | yes                  | `fiber_attenuation_high`                                               |
| `null`         | no (z_km is NULL)    | post-cut signal-loss region — the curve is flat noise floor             |
| `rx_offset`    | no (z_km is NULL)    | `rx_power_drift` — whole-curve vertical offset, no spatial localisation |

For `null` / `rx_offset`, only `HasAnomaly` (and, if it's the planted root, `PpeRootCause`) fire — no `LocatedAt`, no FibrePosition, no `OnSpan`.

### 5.4 Sample subgraph for one trace

Incident 1 has a fibre-cut-style fault on span `span_OMS1_AC`. The PPE
block for the OCh channel `och_ch1` (which terminates on the OTU board at
NE C) reads:

```
ppe:1:och_ch1:span_OMS1_AC          MeasuredOn     och:och_ch1
ppe:1:och_ch1:span_OMS1_AC          MeasuresSpan   span:span_OMS1_AC
ppe:1:och_ch1:span_OMS1_AC          MeasuredBy     board:C.OTU
ppe:1:och_ch1:span_OMS1_AC          HasAnomaly     ppe_anom:1:och_ch1:span_OMS1_AC:0
ppe_anom:1:och_ch1:span_OMS1_AC:0   LocatedAt      fpos:span_OMS1_AC:703     (= 70.3 km)
fpos:span_OMS1_AC:703               OnSpan         span:span_OMS1_AC
ppe_anom:1:och_ch1:span_OMS1_AC:0   PpeRootCause   true
ppe_anom:1:och_ch1:span_OMS1_AC:0   PpeIndicates   alarm:1:1
ppe_anom:1:och_ch1:span_OMS1_AC:0   PpeIndicates   alarm:1:2
… (more PpeIndicates rows fanning out to the alarms in this incident)
```

Reading: in incident 1, NE C's OTU board reconstructed a PPE trace for the
`och_ch1` wavelength as it traverses `span_OMS1_AC`. The trace carries one
ground-truth anomaly at 70.3 km, cross-linked to the alarms that fired in
the same incident on that span.

### 5.5 What's *not* in this file

- **`Correlated`/R1-R4 rule edges** stay in `alarm_triples.csv` — the four
  weighted rules are alarm-to-alarm correlations, not PPE-related.
- **Raw sample arrays** (`samples_blob`, `baseline_blob`) — those are in
  `incidents.db.ppe_traces` as binary blobs. The KG carries only the
  derived facts; the curves themselves are loaded on demand via
  `src/simulator/db.py::load_trace_samples`.
- **PPE detector confidence and `magnitude_db`** — those are anomaly
  attributes inside `incidents.db.ppe_anomalies`. The KG flattens them into
  predicate rows but keeps the numeric attributes in the source DB. Join
  on the `PpeAnomaly` URI's `(incident_id, channel, span_id, idx)` tuple
  when you need them.

### 5.6 When PPE rows are missing

PPE is **strictly additive** — incidents in `incidents.db` produced without
`--with-ppe` (or with `--no-ppe`) contribute zero rows to this file.
Consumers should not assume every incident in `incident_manifest.csv` has
matching PPE rows; check the `n_ppe_triples` column in the manifest first.

---

## 6. `incident_manifest.csv`

One row per incident in `incidents.db` that has at least one alarm. Useful
for filtering before loading the full triple set — e.g. "give me only the
single-fault `fiber_cut` incidents in the raw view".

| Column           | Type   | Meaning                                                                  |
|---|---|---|
| `incident_id`    | int    | Foreign key into `incidents.db → incidents.id`.                          |
| `fault_type`     | string | The simulator's ground-truth fault class. For compound incidents, `+`-joined (e.g. `fiber_cut+ne_power_off`). |
| `fault_target`   | string | What was targeted: a span/NE/board/port id, or for compound `R1:<t> \| R2:<t>`. |
| `target_kind`    | string | One of `link`, `ne`, `board`, `port`, `multi`.                            |
| `suppressed`     | bool   | `True` = the operator-friendly view (suppression filter applied); `False` = raw cascade. The simulator records both for every trigger. |
| `topology_id`    | string | 12-char SHA-1 prefix of the YANG topology — reproducibility check.       |
| `n_alarms`       | int    | Number of alarm rows in this incident.                                   |
| `n_root`         | int    | Number of alarms with `is_root = 1`. Always 1 for single-fault incidents; 2+ for compound (one root per `group_id`). |
| `n_triples`      | int    | Total triples this incident contributes to `alarm_triples.csv` — the sum of location + label + derived + rule edges. |
| `n_ppe_triples`  | int    | Total triples this incident contributes to `ppe_triples.csv`. `0` when the incident was generated without `--with-ppe`. |

### Why these columns

- `incident_id`, `suppressed`, `topology_id` are the bare minimum to join
  back to `incidents.db` and verify the source-of-truth.
- `fault_type` + `fault_target` + `target_kind` are the **ground-truth
  labels** for any supervised model. They sit in the manifest (not in the
  alarm triples) because they're incident-level, not per-alarm.
- `n_alarms` + `n_root` + `n_triples` are summary statistics that let you
  filter or stratify before loading the heavy `alarm_triples.csv`
  (e.g. "give me only incidents with `n_root ≥ 2`" for compound-incident
  evaluation, or "skip incidents with `n_alarms < 5`" if you want to
  exclude near-empty suppressed views).

---

## 7. Why this format

### 7.1 Flat triples, not JSON / GraphML / Parquet

A single CSV with a fixed six-column schema is:
- Greppable from the command line.
- Loadable in one line by pandas, DuckDB, sqlite import, R, or Excel.
- Easily filtered by `incident_id` or `source` without parsing nested
  structures.
- Convertible to NetworkX / PyG / Neo4j with a ~20-line script
  (`src/kg/io.py::to_networkx` is the reference).

Parquet would be 5–10× smaller at the cost of an extra dependency
(`pyarrow`). For an early-stage project where the dataset still fits on a
laptop's SSD, the trade-off is "stay readable" — CSV wins.

### 7.2 Static topology + per-incident alarms split into separate files

The topology is ~250 KB and identical across every incident. The alarm
file is hundreds of MB and grows linearly in incident count. Keeping them
separate means:
- A GNN loader caches the topology graph once and unions in just the
  incident's alarm subgraph per training sample.
- The topology file is human-inspectable in a text editor.
- A schema update to the topology layer doesn't require regenerating the
  alarm file (the URIs are stable across builds).

### 7.3 The `source` column is the most important bookkeeping

`source` exposes provenance. A consumer that distrusts the rule engine's
correlations can filter `source != "rule:*"`. A consumer that wants only
ground truth uses `source = "alarm"`. A consumer training a GNN can use
all of them as different edge types in a heterogeneous graph.

### 7.4 The `weight` column is a confidence handle

For `topology` / `alarm` / `derived` triples the weight is always `1.0`
(deterministic). For the four weighted correlation rules it carries the
published rule weight (0.1, 0.2, 0.3, 0.4) so a rule reasoner can sum
weights across all rules that fire on a pair to compute a per-pair
correlation score.

---

## 8. Loading the KG

### Python + pandas

```python
import pandas as pd
topo = pd.read_csv("data/kg/topology_triples.csv")
alms = pd.read_csv("data/kg/alarm_triples.csv")
ppe  = pd.read_csv("data/kg/ppe_triples.csv")
man  = pd.read_csv("data/kg/incident_manifest.csv")

# One incident's full subgraph (alarms + PPE + shared topology):
sid = 42
sub = pd.concat([topo,
                 alms[alms.incident_id == sid],
                 ppe[ppe.incident_id == sid]])
print(sub.predicate.value_counts())
```

### NetworkX MultiDiGraph

```python
from src.kg.io import to_networkx
from src.kg.builder import build_kg

kg = build_kg()                       # rebuilds in memory (~150 s on 5120 incidents)
g  = to_networkx(kg["topology_triples"]
                 + kg["alarm_triples"][:1000]
                 + kg["ppe_triples"][:1000])
```

### Re-build after a new dataset

```powershell
& .venv\Scripts\python.exe -m src.kg build              # full build (PPE on by default)
& .venv\Scripts\python.exe -m src.kg build --no-ppe     # alarms-only, skip ppe_triples.csv
& .venv\Scripts\python.exe -m src.kg build --no-derived # skip locality+temporal
& .venv\Scripts\python.exe -m src.kg stats              # quick triple counts
```

---

## 9. See also

- `Design/04_knowledge_graph_plan.md` — conceptual design and ML survey.
- `Design/08_root_cause_rules.md` — the rule predicates and inference targets.
- `Design/09_knowledge_graph_builder.md` — the original builder spec (treat as suggestive — see notes in `src/kg/` module docstrings for deviations).
- `Design/12_ppe_simulation.md` §8 — the PPE schema additions documented in §5 of this README, plus the end-to-end flow from `incidents.db` PPE rows to KG triples.
- `src/kg/` — the builder source; `schema.py` is the canonical predicate / URI list, `ppe.py` is the PPE-triple emitter.
- `src/rules/` — the rule engine that consumes the KG.
