# 01 — Overview and Requirements

## 1. Purpose

`diag_agent` receives the alarms and optical measurements of the 6-node test bed. It determines the root cause of an incident through an explicit, displayable **decision procedure** (the diagnosis tree), and it reports the incident and its probable cause as an **Incident Server**, emitting `ietf-incident` payloads.

It is built as a CoALA-style language agent (doc 02) on LangGraph (doc 05). It uses an RFC 9940-grounded knowledge graph as semantic memory (doc 03), past incidents as episodic memory, and an LLM held in procedural memory. The LLM is guardrailed by the knowledge graph and never has the final word on a probability.

## 2. Requirements and where they are met

| Req. | Requirement (from the task) | Where |
|---|---|---|
| R-1 | Follow the CoALA Figure-4 framework: working memory plus procedural, semantic and episodic memory as **decoupled modules** | 02 §2–§5 |
| R-2 | Familiarity with the 6-node simulator and its anomaly injection | 01 §5, 03 §5, 07 |
| R-3 | A KG schema layer in RDF/OWL/TTL using the **RFC 9940** terms and relationships; populated instances in **semantic memory** | 03, `ontology/*.ttl`, `ontology/example-lab-mesh.trig` |
| R-4 | A decision procedure (diagnosis tree) built through these steps: <br>• an agent-led KG walk that guardrails the LLM's hypotheses <br>• windowed evaluation <br>• analogy with episodic memory <br>• Bayesian synthesis <br>The tree must be displayable. | 04 |
| R-5 | LangGraph; DeepSeek or local LLMs, switched through a config file (as in `.env.example`) | 05 |
| R-6 | The capabilities of the ietf-incident Incident Server, including generation of **ietf-incident JSON** | 06, `yang/`, `examples/` |
| R-7 | A demonstrable test case, such as a fibre cut, that shows the reasoning steps, tree generation and the JSON payload | 07 |

## 3. Design principles

These principles carry over from the Janz test-bed findings (`anomaly-diagnosis-agent/docs`), adapted to this project. Each one constrains a later design choice.

1. **The LLM is interface, proposer, planner and narrator, never judge.**
   - Only the deterministic ledger (doc 04 §7) holds probabilities and commits.
   - Measured on the Janz test bed, an LLM deciding alone stated an average confidence of 0.94 and was right in 57% of its commitments. Its proposals, by contrast, were nearly calibrated.
2. **Declarative knowledge lives in the graph; computation lives in tools.**
   - In the graph: fault modes, effects, alarm-to-cause links, consistency rules, location terms and the check catalogue.
   - In tools: propagation, fitting, search and ledger arithmetic.
   - Rules expressed in the graph gave the same verdicts as rules in code (60,059 of 60,059), at about 5% extra cost.
3. **Guardrail by grounding.**
   - The LLM may name only *documented* fault modes, at locations reached by the KG walk, which it states as location terms anchored on symptom references.
   - Anything it proposes that cannot be grounded goes to "none of these", with the reason recorded.
4. **Quiet instruments are evidence.**
   - The case frame records which observable resources saw nothing. Many hypotheses are eliminated this way.
   - The frame is built deterministically and never summarised by an LLM. When an LLM built the frame on the Janz test bed, top-1 accuracy fell by 22–24 points.
5. **Absence counts only after its observation window closes.**
   - The simulator's NMS polling delays are 5 s for OTS up to 300 s for environment alarms. An expected alarm that has not *yet* arrived is not evidence against a hypothesis (doc 04 §6).
6. **"I don't know" is an outcome.**
   - Every diagnosis carries "none of these" (NOTA).
   - The agent commits only under explicit conditions. Otherwise it reports the 90% set and says whether the cause was probably not considered.
7. **Independence: the agent never reads ground truth or simulator internals.**
   - Observations come through a substrate with a column allowlist (§5.3).
   - The substrate never returns data recorded after the agent's current clock.
8. **Evolution through lifecycle events.**
   - Semantic memory changes only through named lifecycle writers: onboarding, inventory sync, documentation publication and closure.
   - Episodic memory is written only at closure and revised only by dated follow-ups.
   - Procedural memory, which includes the prompts and the decision procedure, is never written by the agent at run time (CoALA: writing to procedural memory is risky).
9. **Bitemporal facts.**
   - Every graph fact carries a valid time (when it was true of the network) and a recorded time (when the agent learned it).
   - Diagnosis uses the network as it stood at onset.

## 4. System context

```
                                           ┌───────────────────────────────┐
  Operator / OSS (Incident Client) ◄──────►│ Incident Server API           │  RESTCONF-style JSON + SSE
        incident-acknowledge/diagnose/     │ (diag_agent.incident_server)  │  (doc 06)
        resolve RPCs, notifications        └──────────────┬────────────────┘
                                                          │
                                           ┌──────────────▼────────────────┐
                                           │ diag_agent (LangGraph)        │
                                           │  working memory  ◄─► LLM      │  doc 02, 04, 05
                                           │  procedural / semantic /      │
                                           │  episodic memory              │
                                           └──────────────┬────────────────┘
                                                          │ external grounding actions (read-only)
                       ┌──────────────────────────────────┼─────────────────────────────────┐
                       ▼                                  ▼                                 ▼
          data/yang/*.json (RFC 8345 topology,  data/incidents.db (alarms,       kg_engine SSE (:5001)
          TE tunnels, inventory) + kg_lib       ppe_traces, ppe_anomalies)       alarm.raise/clear,
          alarm catalogues                      or simulator API (:5000)         topology.*, ppe.update
                       ▲                                  ▲
                       └──────── simulator (fault injection: /api/trigger, /api/trigger_multi) ────┘
```

`diag_agent` can run in two modes:

| Mode | Input | Clock | Use |
|---|---|---|---|
| **Replay** | One recorded incident from `incidents.db` | Simulated clock driven by `created_at + t_observed_ms`, advanced by the agent's own wait and check actions | Deterministic tests and the demo (doc 07) |
| **Live** | The `kg_engine` event stream, or polling `incidents.db` | Wall clock | Interactive use with the simulator UI |

## 5. The test bed as diag_agent sees it

### 5.1 Network

The lab mesh (v3) has:

- **Hubs and spans:** six hubs A–F and eight OMS sections, each modelled as two directed spans `span_OMS{n}_{xy}`.
- **OTN topology:** one OTU pipe per directed span, `otu_OMS{n}_{xy}`, whose `supporting-link` is the span.
- **Channels:** six OCh tunnels and six matching ODU tunnels (`odu_chN`, client service `chN`). The routes below are those of `Design/03` and `Design/12`. `Design/02 §3` shows an older route set and is superseded.

| Service | OCh route | Spans | Drop NE |
|---|---|---|---|
| ch1 | A→C | span_OMS1_AC | C |
| ch2 | A→B→C | span_OMS2_AB, span_OMS3_BC | C |
| ch3 | D→C | span_OMS4_DC | C |
| ch4 | D→B→C | span_OMS5_DB, span_OMS3_BC | C |
| ch5 | D→E→F | span_OMS6_DE, span_OMS7_EF | F |
| ch6 | C→F | span_OMS8_CF | F |

- **Span lengths:** OMS6, OMS7 and OMS8 are 40, 60 and 80 km. OMS1–OMS5 carry `nfms:length-km` in `topology.json`. `diag_agent` always reads lengths from YANG and never hard-codes them.
- **Inventory:** NEs carry OTU, OA, FIU, SCC and FAN boards. OTU boards carry the PPE settings (`nfms:ppe-*`).

### 5.2 Faults and observables

The simulator injects 11 faults:

| Fault | Target kind |
|---|---|
| `fiber_cut` | link |
| `fiber_attenuation_high` | link |
| `connector_dirty` | link, plus position z |
| `b1_exc_high` | link |
| `b2_exc_high` | link |
| `fec_pre_cor_high` | link |
| `rx_power_drift` | port |
| `config_mismatch` | port |
| `ne_power_off` | NE |
| `board_power_off` | board |
| `fan_failure` | NE |

Compound incidents come from `/api/trigger_multi`, which adds R1, R2, … with an optional delay.

Each fault produces two kinds of observable:

1. **Alarm cascade:**
   - The fault rule emits seed alarms.
   - L1 adds the suppression-graph children and L2 the cause-keyword matches, giving typically 30–60 alarms.
   - A per-layer polling delay is then applied:

     | Layer | Polling delay |
     |---|---|
     | OTS, OMS | 5 s |
     | OCh | 10 s |
     | OTU, SDH | 30 s |
     | ODU, FEC, client | 60 s |
     | Equipment | 120 s |
     | Environment | 300 s |

   - Each cascade is recorded twice, as a raw view and as a suppressed view. In 80% of incidents the root alarm is *not* the first alarm observed.
2. **PPE traces:**
   - One power-profile trace per affected dropping OCh, over the whole route, with a 30 s polling delay.
   - Each trace carries the detector's anomalies (`step`, `slope`, `null`, `rx_offset`), reverse-mapped to `(span_id, offset_in_span_km)`.

### 5.3 Ground-truth hygiene

The test bed stores labels beside observations. The `diag_agent` substrate enforces an allowlist:

| Source | Readable by diag_agent | Forbidden (harness only) |
|---|---|---|
| `incidents` | `id`, `created_at`, `suppressed`, `topology_id` | `fault_type`, `fault_target`, `target_kind`, `note` |
| `alarms` | every column except those on the right | `t_offset_ms` (the physical time), plus `is_root` and `group_id` if a legacy database still has them |
| `ppe_traces` | every column except `group_id` | `group_id` |
| `ppe_anomalies` | rows with `is_root = 0` (the detector's view) | rows with `is_root = 1` (the emitter's planted truth) |
| simulator code | `yang_loader` (as a YANG parser only) | `faults`, `engine`, `ppe` generators, `generate_dataset`, `rules/`, which is the rule baseline |

- **Enforcement:** an `import-linter` contract and a substrate unit test.
- **Harness-only data:** the forbidden labels are read only by the scoring harness (doc 07 §8) and by the *field-check oracle*, which plays the field engineer in replay tests (doc 04 §8.3).

### 5.4 Prerequisites and gaps in the current repository

- `data/yang/*.json` is not in the repository, although the user guide says it is committed. Replay and live modes need it (`FMA_YANG_DIR`).
- The simulator reads its catalogues from `data/alarm_*.json`, which is git-ignored. The same files are present under `simulator/kg_lib/`. The setup step links or copies them.
- `requirements.txt` needs `langgraph`, `langgraph-checkpoint-sqlite`, `langchain-openai`, `rdflib`, `pyoxigraph` (optional), `pydantic-settings`, `pyyaml`, `rich`, `yangson` (for payload validation) and `import-linter`.

## 6. Out of scope for this design iteration

- Writing to the network: resolution is advised and verified, never executed (doc 06 §6).
- GNN specialists. The `ml/` work in the simulator could later join as tempered likelihood terms (doc 04 §7.6), but it is not required.
- Learning prompts or procedures automatically. Candidate procedures are queued for human review (doc 02 §6.3).
