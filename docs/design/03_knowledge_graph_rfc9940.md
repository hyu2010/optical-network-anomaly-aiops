# 03 — Knowledge Graph: RFC 9940 Schema Layer and Semantic Memory

The knowledge graph is the content of `diag_agent`'s **semantic memory**. It is RDF/OWL with these files:

| File | Role |
|---|---|
| `ontology/onm-rfc9940.ttl` | Schema layer: upper terms (RFC 9940) |
| `ontology/optx-optical.ttl` | Schema layer: optical domain |
| `ontology/dx-diagnosis.ttl` | Schema layer: diagnostic knowledge |
| `ontology/kb-fault-modes.ttl` | Populated documentation |
| `ontology/example-lab-mesh.trig` | Populated instances |

All five files parse with `rdflib`. All the rule queries compile, and the walk patterns resolve on the example network (§9).

## 1. Layering

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│ L0  onm:  RFC 9940 upper layer — Resource, Characteristic, Value, Change, Event,    │
│           Condition, State, Detection, Relevance, Occurrence, Fault, Problem,       │
│           Cause, Incident, Symptom, Anomaly, Alert, Alarm (+ Transient, …)          │
│           — terms and relationships only, no fault knowledge                        │
├─────────────────────────────────────────────────────────────────────────────────────┤
│ L1  optx: optical domain — NE, Board(OTU/OA/FIU/SCC/FAN), Port, OmsSpan, OmsSection,│
│           OtuLink, Amplifier, OchTunnel, OduTunnel, ClientService, Layer(+cadence), │
│           Characteristics (InputPower, PowerProfile, …), Conditions (LossOfSignal,  │
│           ServerSignalFail, ProfileStep, …), AlarmType, AlarmInstance, PpeAnomaly   │
├─────────────────────────────────────────────────────────────────────────────────────┤
│ L2  dx:   diagnostic knowledge schema — FaultMode, Effect, Strength, Nature,        │
│           LocationRole/LocationTerm, CascadeRule, Check, ConsistencyRule,           │
│           Procedure, Hypothesis, Prediction, FrameSymptom, DiagnosisTree,           │
│           bitemporal + lifecycle + completeness vocabulary                          │
├─────────────────────────────────────────────────────────────────────────────────────┤
│ KB  kb:   populated documentation — 13 fault modes, 6 cascade rules, 15 effect      │
│           roles + 13 LLM location terms, alarm types, 11 checks, 7 consistency rules                  │
├─────────────────────────────────────────────────────────────────────────────────────┤
│ I   net:/obs:  instances — lab-mesh resources from YANG (bitemporal named graphs);  │
│           RFC 9940 interpretation of closed incidents                               │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

Every class in L1 and L2 is reconciled to an L0 term with `rdfs:subClassOf`. Vendor or domain vocabulary is therefore always interpreted against a single upper layer. RFC 9940 itself says this is its role: it "defines the terms … but carries no fault knowledge".

## 2. L0 — RFC 9940 terms as OWL

| RFC 9940 term (§3.2/§3.3) | OWL | Key relationships |
|---|---|---|
| Network system | `onm:NetworkSystem` | `onm:hasResource` (1:n, Figure 1) |
| Resource (recursive) | `onm:Resource` | `onm:hasPart` / `onm:partOf` (transitive) |
| Characteristic | `onm:Characteristic` | `onm:hasCharacteristic` (Resource→Characteristic) |
| Value | `onm:Value` | `onm:hasValue`, `onm:literalValue`, `onm:unit`, `onm:observedAt` |
| Change (over a period) | `onm:Change` | `onm:changeOf`, `onm:fromValue`, `onm:toValue`, `onm:startTime`, `onm:endTime` |
| Event (a distinct moment) | `onm:Event` | `onm:eventTime`, `onm:observationOf` (an Event may be the observation of a Change) |
| Condition (an interpretation of Values) | `onm:Condition` | `onm:interprets` (→ Characteristic), `onm:conditionOf` (→ Resource) |
| State (a Condition at a time) | `onm:State` ⊑ Condition | `onm:inState` / `onm:stateOf` |
| Detect | `onm:Detection` | `onm:detected` / `onm:detectedBy`, `onm:observer` |
| Relevance | `onm:RelevanceAssessment` | `onm:assesses`, `onm:isRelevant`, `onm:policy` |
| Occurrence (a Relevant Event or Change) | `onm:Occurrence` | `onm:aggregates`, `onm:occursOn` |
| Fault | `onm:Fault` ⊑ Occurrence | `onm:hasCause`, `onm:indicatesState` (Figure 3: Fault → Problem) |
| Problem | `onm:Problem` ⊑ State | `onm:resolutionStatus`, `onm:perspective` (the historic and per-perspective notes) |
| Cause (the Events that gave rise to it) | `onm:Cause` | `onm:causeEvent` (→ Event), `onm:givesRiseTo` |
| Incident | `onm:Incident` ⊑ Occurrence | `onm:resultsFrom` (→ Problem; Figure 5), `onm:affectsService` |
| Symptom | `onm:Symptom` | `onm:observable` (the Value, Change, State, Event or Condition in the Symptom role), `onm:indicates` (→ Problem) |
| Anomaly | `onm:Anomaly` | `onm:deviatesFrom` (→ the expected Value) |
| Alert | `onm:Alert` | `onm:alertFor` (→ Fault) |
| Alarm (RFC 8632) | `onm:Alarm` ⊑ State | `onm:issuesAlert`, `onm:alarmedResource` |
| Intermittent, Transient, Recurrent | `onm:IntermittentState`, `onm:TransientState`, `onm:RecurrentProblem` | — |
| Telemetry, Monitoring, Analytics, Observability (§3.1) | `onm:Process` subclasses | `onm:feeds` (the cascade) |

### 2.1 The RFC 9940 workflows as graph paths

- **Figure 3 (Event → Occurrence → Fault → Problem/Cause; Alert → Alarm):** `obs:raise a onm:Event` ← `onm:aggregates` ← `onm:Occurrence`. The concluded `onm:Fault` then links by `onm:indicatesState` to an `onm:Problem` and by `onm:hasCause` to an `onm:Cause`. The `optx:AlarmInstance` ⊑ `onm:Alarm` points to an `onm:Alert` through `onm:issuesAlert`.
- **Figure 4 (State → Relevance → Problem → Symptom/Cause/Incident):** the framer's relevance policy is recorded as `onm:RelevanceAssessment`. Each frame item is a `dx:FrameSymptom` ⊑ `onm:Symptom`, which `onm:indicates` a Problem.
- **Figure 5 (Fault → Problem → Incident; Cause ↔ Symptom):** `onm:Incident` links to its Problems by `onm:resultsFrom`. The Cause–Symptom relation is reached through the hypothesis's predictions (`dx:Prediction` with `dx:matchedBy` to a `dx:FrameSymptom`).

### 2.2 Disjointness that protects reconciliation

`onm:Cause`, `onm:Condition`, `onm:Symptom`, `onm:Alert`, `onm:Resource`, `onm:Characteristic`, `onm:Value` and `onm:Detection` are pairwise disjoint, and `onm:Event` and `onm:Change` are disjoint from `onm:Condition`. Two consequences follow:

- **An alarm reports a Condition** (`optx:indicatesCondition`, e.g. `OTS_LOS` → `optx:LossOfSignal`).
- **Its documented probable causes are fault modes**, a separate and disjoint kind of thing (`dx:possibleCause`, e.g. `OTS_LOS` → `kb:FIBRE_CUT`).

The Janz test bed found that typing Cause and Condition separately prevented reconciliation errors. A reasoner, or the SHACL shapes in §9, flags any data that conflates "loss of signal" (a Condition) with "fibre cut" (a Cause).

## 3. L1 — Optical domain, aligned with the simulator's YANG

### 3.1 YANG → RDF mapping (the `yang_to_rdf` loader)

| YANG source (`data/yang/*.json`) | RDF |
|---|---|
| `inventory: network-element/uuid` | `net:ne_X a optx:NetworkElement ; optx:yangId "X"` |
| `component` class `module`, named OTU/OA/FIU/SCC/FAN | `optx:OTUBoard` / `OABoard` / `FIUBoard` / `SCCBoard` / `FanBoard`, linked by `optx:hostsBoard` |
| `component` class `port` | `optx:Port`, linked by `optx:hasPort` |
| `network[optical-topo]/link` | `optx:OmsSpan` with `optx:sourceNE`/`destNE` and `optx:txPort`/`rxPort` (TP→inventory leafref), `optx:lengthKm` (`nfms:length-km`), `optx:spanCount`, `optx:alphaDbPerKm`, `optx:fibreType` |
| span pairs `span_OMS{n}_{xy}` and `span_OMS{n}_{yx}` | `optx:reverseOf` (symmetric) plus `optx:partOfSection net:OMS{n}`, the canonical cable-section key |
| `nfms:amplifiers` / node OA at the launch | `optx:Amplifier`, `optx:launchAmplifier`, `optx:amplifierBoard` |
| `network[otn-topo]/link` with `supporting-link` | `optx:OtuLink`, `optx:supportedBy` |
| `te/tunnels/tunnel[wdm]` with ERO | `optx:OchTunnel`; ordered `optx:hasHop` (`hopIndex`, `hopLink`, `hopStartKm` as the cumulative PPE path coordinate); `optx:traverses`; `optx:dropBoard`; `optx:txChannelPowerDbm` |
| `te/tunnels/tunnel[otn]` | `optx:OduTunnel`, `optx:supportingTunnel`, `optx:traverses` (OTU links), `optx:clientPort`, `optx:rateType` |
| `nfms:client-service-name` | `optx:ClientService`, linked by `optx:carries` |
| OTU `nfms:ppe-*` | `optx:ppeEnabled`, `optx:ppeDeltaZm`, `optx:ppePollMs` |
| (derived) | `dx:dependsOn`, the transitive closure from a service or tunnel to every resource on its path, used by rule R5 and the scope computation |

Every resource carries two attributes:

- `optx:yangId`: the exact string used in `alarms.ne`, `board`, `port` and `container_id`. It is the join key to observations.
- `optx:yangPath`: the provenance of the resource.

The loader reads only the published YANG instance files. It may reuse `simulator.simulator.yang_loader` purely as a JSON parser (doc 01 §5.3).

### 3.2 Observations interpreted in RDF terms

Observations stay in the substrate. When a frame or a closed incident needs them as RDF, they are interpreted as follows:

| Observation | RDF interpretation |
|---|---|
| `alarms` row | `optx:AlarmInstance` ⊑ `onm:Alarm`, with: <br>• `optx:alarmType` → `optx:AlarmType` <br>• `onm:alarmedResource` → port, board or NE <br>• `optx:containerRef` → span, OTU, OCh, ODU, port or board <br>• `optx:raiseEvent` → an `onm:Event` whose `eventTime` = `created_at + t_observed_ms` and whose `onm:detectedBy` carries the observer <br>• `optx:eventId "alm-<incident>-<row>"` |
| `ppe_traces` row | `optx:PpeTraceValue` ⊑ `onm:Value` of the OCh's `optx:PowerProfile` Characteristic, observed by the drop OTU, which is an `optx:Instrument` |
| `ppe_anomalies` row (`is_root = 0`) | `optx:PpeAnomaly` ⊑ `onm:Anomaly` and `onm:Change` (against the baseline profile), with `optx:anomalyKind`, `optx:zKm`, `optx:onSpan`, `optx:offsetInSpanKm`, `optx:magnitudeDb`, `optx:detectorConfidence` |
| Quiet instrument | An in-scope `optx:Instrument` with no Detection in the window. This is recorded in the frame as `dx:Prediction … dx:instrumentAlive true` with no match. |

### 3.3 Layers carry the observation model

`optx:Layer` individuals carry `optx:layerRank` (the cascade order of Design/08 §3) and `optx:pollCadenceMs`:

| Layer | Cadence |
|---|---|
| OTS, OMS | 5 s |
| OCh | 10 s |
| OTU, SDH | 30 s |
| ODU, FEC, client | 60 s |
| Equipment | 120 s |
| Environment | 300 s |
| PPE | 30 s |

This polling model is declarative knowledge. The window evaluator reads it to decide when an absence becomes evidence (doc 04 §6).

## 4. L2 and KB — Diagnostic knowledge

### 4.1 Fault modes and effects

A `dx:FaultMode` is documentation of a *kind* of Fault. Each mode carries the following:

| Attribute | What it gives |
|---|---|
| `dx:nature` | Hard, soft, incipient, transient, equipment, configuration or monitoring |
| `dx:appliesTo` | The grain filter, e.g. `optx:OmsSpan` |
| `dx:baseRatePerYear` | The prior rate |
| `dx:hasParameter` | Parameters, e.g. `position_km` |
| `dx:servicesImpact` | The service impact |
| `dx:incidentCauseName` and `dx:incidentCategory` | The ietf-incident mapping (doc 06 §5) |

Each mode also lists its **primary** `dx:Effect`s. An effect has these parts:

- **Condition:** `dx:predictsCondition` gives the Condition class. `dx:predictsAbsence` marks a predicted absence.
- **Placement:** `dx:atRole` gives where the effect appears relative to the fault location, and `dx:inLayer` gives its layer, which sets the observation cadence.
- **Strength:** `dx:strength` is Required, Expected or Possible. Missing effects cost 2, 0.6 and 0 nats respectively.
- **Timing:** `dx:physDelayMs` and `dx:lasting`.
- **PPE signature:** optional, as `dx:ppeSignature`, giving the kind, the magnitude range and the position (free-in-span, span-start, receiver or none).

| Fault mode | Nature | Applies to | Primary effects | Simulator fault (harness only) |
|---|---|---|---|---|
| `FIBRE_CUT` | Hard | OmsSpan | LOS at span rx (Required); PPE step ≤ −25 dB, free-in-span (Required) | `fiber_cut` |
| `SECTION_CUT` | Hard | OmsSection | LOS at both fibres' rx | (compound or extension) |
| `SPAN_LOSS_INCREASE` | Soft | OmsSpan | Low input power (Required); pre-FEC degraded (Expected); PPE slope 0.05–0.3 dB/km (Expected) | `fiber_attenuation_high` |
| `CONNECTOR_DIRTY` | Soft | OmsSpan | PPE step −0.5 to −3 dB (Required); pre-FEC degraded (Possible) | `connector_dirty` |
| `SDH_B1_ERRORS` / `SDH_B2_ERRORS` / `FEC_DEGRADATION` | Soft | OtuLink | B1, B2 or pre-FEC errors (Required); OTU_DEG (Expected); clean PPE (Expected) | `b1_exc_high` / `b2_exc_high` / `fec_pre_cor_high` |
| `RX_POWER_DRIFT` | Incipient | Port | Input power abnormal (Required); PPE clean or rx_offset (Expected) | `rx_power_drift` |
| `CONFIG_MISMATCH` | Configuration | Port | Trace or payload mismatch (Required); clean PPE (Expected) | `config_mismatch` |
| `NE_POWER_LOSS` | Equipment | NE | Equipment down (Required); LOS at the rx of every span from the NE (Required) | `ne_power_off` |
| `BOARD_POWER_LOSS` | Equipment | Board | Equipment down (Required); LOS at the peer rx (Required); LOS at downstream OTU rx (Required) | `board_power_off` |
| `FAN_FAILURE` | Equipment | NE | Cooling failure (Required); over-temperature on boards (Expected, +120 s) | `fan_failure` |
| `AMP_FAIL` | Hard | OABoard | Equipment down (Required); LOS at the launched span's rx (Required); PPE step at span start (Required) | (documented only) |

`AMP_FAIL` is documented but never injected by the simulator. This is deliberate. On the Janz test bed, missing modes produced *wrong* commitments to look-alike documented modes. Keeping a look-alike in the catalogue forces the agent to separate it with evidence, as it does in the fibre-cut test (doc 07).

### 4.2 Cascades are written once

Modes document only their primary effects. The propagator closes them under six `dx:CascadeRule`s, which encode the layer cascade of Design/08 §3 (OTS → OCh → OTU → ODU → client):

| Rule | From | To | Via | Strength |
|---|---|---|---|---|
| CR1 | LOS at OTS layer at a span's rx | LOS at OCh layer | rx of every OTU pipe the span supports | Required, +5 ms |
| CR2 | LOS at OCh | Server signal fail at OTU | Same port | Required |
| CR3 | OTU server signal fail | Backward defect (BDI) | The OTU pipe's tx (upstream) | Expected |
| CR4 | OTU server signal fail | ODU server signal fail | Client port of each ODU over the pipe | Required |
| CR5 | ODU server signal fail | Service down | The service the ODU carries | Required |
| CR6 | OTU degraded | Service degraded | Carried services | Expected |

Same-location children predicted from `optx:suppresses` (the simulator's L1 step), and alarm types whose `dx:possibleCause` includes the mode at the root's location (the L2 step), are predicted with strength **Possible**. They are explained if seen, and cost nothing if absent.

### 4.3 Location roles and LLM location terms

A `dx:LocationRole` is a declarative walk: a SPARQL group pattern from `?anchor` to `?target` (`dx:walkPattern`), with a result-class grain filter. Roles are used in two directions:

- **Effect roles** (fault location → where its effects appear), for example `SPAN_RX_PORT`, `RX_OF_SUPPORTED_OTU`, `DROP_RX_OF_OCH_OVER_SPAN`, `PEER_RX_OF_LINKS_FROM_BOARD` and `DOWNSTREAM_OTU_RX_OF_BOARD`.
- **Location terms** (`dx:LocationTerm`; symptom → where the cause may be). These are the only location vocabulary the LLM may use (doc 04 §5):

| Term | Meaning (anchored on a symptom S#, service V# or shared element E#) |
|---|---|
| `this_port` | The port the symptom is reported against |
| `this_unit` | The board hosting it |
| `this_ne` | The NE hosting it |
| `upstream_span` | The directed span whose rx is this port |
| `upstream_section` | Both fibres of that span |
| `upstream_amplifier` | The OA launching the upstream span |
| `upstream_tx_unit` | The board transmitting into the upstream span |
| `upstream_ne` | The far-end NE of the upstream span |
| `supporting_span` | For an OTU symptom: the span supporting its pipe |
| `far_end_transmitter` | For an OTU symptom: the transmitting board |
| `path_to_far_end` | For an ODU or client symptom: every span on the supporting OCh route |
| `reverse_span` | The other fibre |
| `shared` | A frame shared element E# |

### 4.4 Checks, rules, procedures and completeness

- **Check catalogue.** Eleven `dx:Check`s: 6 remote and 5 field (doc 04 §8.3). Each check has its minutes, remote or field flag, service-interruption flag, target class, observed Conditions, answer tolerance and tau.
- **Graph-consistency rules.** Seven `dx:ConsistencyRule`s, R1–R7, each a SPARQL `ASK` where *true* means the hypothesis `$h` is contradicted. Each contradicted rule costs 4 nats, and at most two count:

  | Rule | Contradiction it detects |
  |---|---|
  | R1 | A Required effect whose window has closed was not seen by a live instrument |
  | R2 | Predicted and observed service impact differ |
  | R3 | Fewer than 70% of relevant symptoms are explained |
  | R4 | The mode is not documented for this resource class |
  | R5 | A single hard cause is not on every down service's path |
  | R6 | A resource predicted dead reported an alarm itself |
  | R7 | A located PPE anomaly is not where the hypothesis predicts. This generalises the Design/12 rules R5–R7 into one graph rule. |

  The rules live in the graph and are run by one generic engine. On the Janz test bed, rules held in the graph produced the same verdicts as rules in code (60,059 of 60,059).
- **Procedures.** `dx:Procedure` and `dx:ProcedureStep` hold reviewed alarm-clearing procedures. A new procedure is published only after human review of a candidate (doc 02 §6.3).
- **Completeness declarations** (`dx:CompletenessDeclaration`). `optx:traverses`, `optx:supportedBy` and `optx:hostsBoard` are complete, so absence means false. Cable sections and amplifier records are incomplete, so absence means *unknown*. The rule engine treats absence on incomplete relations as unknown and logs a data request.

## 5. What is stored in semantic memory: named graphs, bitemporal

Each lifecycle write is one named graph. The default graph is a catalogue that holds each graph's metadata: `dx:validFrom`, `dx:validTo`, `dx:recordedAt` and `dx:supersededAt`, `dx:writtenBy` (a `dx:LifecycleEvent`), and `prov:` provenance.

| Named graph | Lifecycle writer | Content | Example |
|---|---|---|---|
| `g:ontology-<ver>` | Release | L0–L2 TBox | `onm-*.ttl`, `optx-*.ttl`, `dx-*.ttl` |
| `g:kb-<release>` | DocumentationPublication | KB | `kb-fault-modes.ttl`, plus the loader output from `alarm_catalog_9800.json` (796 types), `alarm_suppression.json` (50 edges) and `alarms_detail.json` (possible-cause text, curated to `dx:possibleCause`) |
| `g:net-<topology_id>-v<n>` | Onboarding, InventorySync, TopologyChange | Lab-mesh resources | `example-lab-mesh.trig` |
| `g:incident-<no>` | Closure | RFC 9940 view of a closed incident: Events, Occurrence, Symptoms, Problems, Fault, Cause, Alert, Incident, and a tree reference | `example-lab-mesh.trig` |
| `g:stats-<date>` | BaseRateUpdate | Closed-ticket counts and base rates by (mode, resource class), from episodes with verification ≥ 0.7 | — |

**As-of queries.** Every `SemanticMemory` query takes `as_of = (valid_time, record_time)`. The store selects the graphs valid at `valid_time` and recorded by `record_time`. The diagnosis therefore sees the network *as it stood at onset*, even after a later topology change (a `kg_engine` `topology.*` event), and a replay sees only what the agent could have known.

## 6. How the agent uses the graph

| Use | Primitive | Example |
|---|---|---|
| Scope and closure | `sparql("scope_closure", anchor)` over `dx:dependsOn`, `optx:traverses`, `optx:supportedBy` | Services through `span_OMS3_BC` are ch2 and ch4 |
| Backward walk (abduction) | `walk(anchor, step)`, where a step is a location term, or a raw relation with direction | `upstream_span(C.FIU.line_from_B)` = `span_OMS3_BC` |
| Documented modes at grain | `documented_modes(resource)` (`dx:appliesTo` plus the `rdfs:subClassOf*` grain check) | `B.OA_C` → `AMP_FAIL`, `BOARD_POWER_LOSS` |
| Forward propagation | `effects(mode)` plus the `CascadeRule`s, which give predictions with strength, layer and delay | `FIBRE_CUT@span_OMS3_BC` → its prediction table (doc 04 §6.2) |
| Alarm-type knowledge | `alarm_type(name)` → Condition, layer, suppression children, possible causes | `OTS_LOS` → `LossOfSignal`, 5 possible causes |
| Rules | `rules()` → ASK templates, run over a per-hypothesis scratch graph (Predictions + FrameSymptoms + KB) | R1–R7 |
| Checks | `checks()` → catalogue for the advisor | `CHK_PPE_PROFILE`, 1 min |

## 7. Relationship to the simulator's existing KG

The simulator's property-style triples (`simulator/kg/schema.py`) are a different artefact: training features for the rule baseline and the GNN. `diag_agent` does not read them. Where they overlap, they map as follows:

| `kg/schema.py` predicate | Here |
|---|---|
| `Hosts` | `optx:hostsBoard`, `optx:hasPort` |
| `CarriedBy` / `OnPipe` | `optx:supportedBy` |
| `RidesOver`, `SupportingTunnel` | `optx:supportingTunnel` |
| `Carries` | `optx:carries` |
| `AtNE`, `AtBoard`, `AtPort` | `onm:alarmedResource` |
| `Container`, `AtSpan`, `AtChannel` | `optx:containerRef` |
| `MeasuredOn`, `MeasuresSpan`, `MeasuredBy`, `HasAnomaly`, `LocatedAt`, `OnSpan` | `optx:PowerProfile` / `optx:PpeAnomaly` properties |
| `Earlier`, `Close`, `WithinSix`, `OnSame*`, `Correlated`, `SameIncident` | Not stored. They are computed in working memory by the framer and the incident correlator. |
| `RootCause`, `Secondary`, `PpeRootCause` | **Never imported.** These are labels or rule-baseline outputs, and importing them would leak the answer. |

## 8. Documentation conformance test

The test bed is the "network". The KB must therefore describe how the test bed actually behaves, and this is checked rather than assumed. A conformance job runs as follows:

1. It triggers every simulator fault on every valid target, with the polling delay removed, in a scratch database.
2. It runs the propagator for the corresponding documented mode at the same location.
3. It compares the predicted Conditions with the observed ones.

Each mismatch is reported for a knowledge engineer to review. The job never writes the KB.

The job already has its first finding from reading the code:

- `board_power_off` on an **FIU** board emits `BD_OFFLINE` plus a peer `R_LOS`, but no OTU-layer cascade downstream of the dead board.
- The KB documents the physically expected `DOWNSTREAM_OTU_RX_OF_BOARD` LOS as Required.
- Either the simulator is fixed, or the effect is downgraded to Possible for this test bed. The decision is recorded as an open issue rather than silently tailored.

## 9. Validation and tooling

**Parse and consistency check** (already run on these files):

- All files parse with rdflib.
- No term is used without being declared.
- All seven `ASK` rules compile.
- The walks resolve on the example network:

  | Walk | Result |
  |---|---|
  | `upstream_span(S1)` | `span_OMS3_BC` |
  | `upstream_tx_unit(S1)` | `B.FIU_C` |
  | `upstream_amplifier(S1)` | `B.OA_C` |
  | `CR1` from `C.FIU.line_from_B` | `C.OTU.line_rx_B` |
  | `CR3` | `B.OTU.line_tx_C` |
  | `CR4` | `C.OTU.client_ch2`, `C.OTU.client_ch4` |

**SHACL shapes** (`semantic_memory/ontology/shapes.ttl`, to be written):

- Every `dx:FaultMode` has at least one Required effect and a `dx:appliesTo`.
- Every `dx:Effect` has a role, a layer and a strength.
- Every `optx:OmsSpan` has a length greater than 0 (PPE needs it, per Design/03 §2.3).
- Every `optx:AlarmType` indicates exactly one Condition.
- No node is typed both `onm:Cause` and `onm:Condition`.

**CI checks:**

- Load the full KB and a generated lab-mesh graph, validate the shapes, and run the conformance job on a 1-sample-per-target dataset.
