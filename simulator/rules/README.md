# src/rules — Rule-Based RCA Reasoner

The deterministic, explainable root-cause-analysis (RCA) reasoner over the simulator's alarm cascades. Given a incident's alarms + the YANG topology, it picks **one root-cause alarm**, names the **fault class**, and tags every other alarm as **Secondary** of some parent.

This module is the **T1 baseline** in the project's ML tier map (`Design/04 §6.3`). It implements the rule families defined in `Design/08_root_cause_rules.md` — the reasoner the GNN (`src/ml/`, planned in `Design/11`) has to beat.

- **Design spec**: `Design/08_root_cause_rules.md` (rule definitions); `docs/03_rules_catalog.md` (flat inventory + implementation status)
- **Current accuracy on the 5120-incident default dataset**:
  - Root alarm: **99.1%**
  - Fault class: **98.4%**
  - Per-class breakdown: 100% on `b1_exc_high`, `b2_exc_high`, `board_power_off`, `fan_failure`, `fiber_attenuation_high`; 96–100% on the rest. See `memory/rules_baseline.md`.

---

## Quick start

```powershell
# Score the reasoner against every incident in data/incidents.db.
& .\.venv\Scripts\python.exe -m src.rules evaluate

# Same, but raw incidents only (skip the L1-suppressed view).
& .\.venv\Scripts\python.exe -m src.rules evaluate --raw-only

# Score the first 200 incidents (smoke test).
& .\.venv\Scripts\python.exe -m src.rules evaluate --max-incidents 200

# Show the rule trace for one incident — useful when explaining a miss.
& .\.venv\Scripts\python.exe -m src.rules explain 42
```

`evaluate` prints:
- A per-fault-class accuracy table (raw + suppressed views, separately).
- A confusion matrix on `fault_type` for single-fault incidents.
- The first N (default 10) failed incidents with predicted vs. actual root.

`explain` prints:
- The ground-truth root(s) and the reasoner's pick.
- The fault class predicted, and which rule fired (`source`).
- The first 25 `Inferred` records (Secondary / RootCandidate / RootCause), in the order rules emitted them.

---

## What "rule-based" means here

The reasoner does **not** train. It takes a fixed set of expert-written predicates and runs them in priority order against the alarm list. Each predicate is a pure Python function with a clearly named source (e.g. `§8.1.fiber_cut`), so every prediction comes with a trace explaining *which rule fired and why*.

This buys two things a learned model doesn't:
- **Explainability** — every pick maps to one rule with a citation in `Design/08`.
- **No labelled data required** — the rules are derived from expert knowledge (the OSN 9800 alarm catalog + suppression graph), not from training incidents. They work even when there's no ground truth.

It loses one thing the GNN should eventually win: **generalisation to cases the rules miss**. See §"Known failure modes" below.

---

## Architecture

```
src/rules/
├── __init__.py            # exports infer_root, Inference
├── __main__.py            # CLI: evaluate | explain
├── schema.py              # Inferred, Inference dataclasses
├── reasoner.py            # orchestrator — picks root, applies all rule families
├── fingerprints.py        # §8 — 10 fault-class fingerprints (primary root pickers)
├── layer_cascade.py       # §3 — within-port layer ordering (OTS→OMS→OCh→...→client)
├── topology_cascade.py    # §4 — cross-port topology-grounded propagation
├── equipment.py           # §5 — NE / board / fan "trump cards"
└── evaluate.py            # scoring against incidents.db
```

### Data flow

```
incidents.db         data/yang/
   alarms table         topology.json
        │              tunnels.json
        ▼              inventory.json
  list[dict]            ▼
   (one per      Topology object
    alarm row)         │
        │              │
        └──────┬───────┘
               ▼
        reasoner.infer_root(incident_id, alarms, topology)
               │
               ▼
  ┌─────────── pipeline ────────────┐
  │                                  │
  │  fingerprints.apply()  ── §8    │  ← picks root + fault class
  │  layer_cascade.apply()  ── §3   │  ← labels Secondary (within-port)
  │  topology_cascade.apply() ── §4 │  ← labels Secondary (cross-port)
  │  equipment.apply()      ── §5   │  ← NE/board trump cards
  │                                  │
  │  if no fingerprint fired:        │
  │     fallback.lowest_layer        │  ← graceful degradation
  └──────────────────────────────────┘
               │
               ▼
   Inference(
     incident_id, root_alarm_id, root_alarm_name,
     fault_type, source,
     inferred=[Inferred(kind, alarm_id, parent_id, ...), ...],
   )
```

### Why this orchestration order?

Per `reasoner.py` docstring (§8 first, then §3 / §4 / §5):

1. **Fingerprints (§8) pick the root.** Each of the 10 ★ priority faults has a characteristic alarm constellation. When two fingerprints match, the highest-weighted (most specific) one wins. Ties broken by `FINGERPRINTS` list order — more specific rules listed first.
2. **§3 / §4 / §5 label secondaries.** These rules don't pick a root — they explain *why* the other alarms are not the root. The `Inferred(kind="Secondary")` records they produce are informational (for `explain` output and downstream `Secondary` triple emission in `src/kg/`).
3. **Fallback only if nothing fired.** Pick the lowest-layer alarm not marked Secondary. Graceful degradation — `Design/08 §6` cautions that "earliest timestamp = root" is unsafe under polling-delay jitter, so this fallback intentionally does NOT use time.

---

## Rule families

### §8 — Fault-class fingerprints (`fingerprints.py`)

Ten functions, one per ★ priority fault. Each returns zero or more `RootCandidate` records with `weight ∈ [1.6, 2.0]`.

| Fingerprint | Root alarm | Key premise | Weight |
|---|---|---|---|
| `fp_fiber_cut` | `OTS_LOS` on a span | `MUT_LOS` also at that span | 2.0 |
| `fp_ne_power_off` | `POWER_ABNORMAL` @ NE | ≥ 60% of NE's outgoing spans show `OTS_LOS` | 2.0 |
| `fp_board_power_off` | `BD_OFFLINE` @ board | peer-port `R_LOS` OR (no `OTS_LOS` at same NE) | 2.0 / 1.7 |
| `fp_fan_failure` | `FAN_FAIL` @ NE | `TEMP_OVER` at same NE, different board | 2.0 |
| `fp_config_mismatch` | `OPU_PLM` @ port | ≥ 3 of `{OPU_PLM, OTU_TIM, ODU_PM_TIM}` co-occur on same port; NO `OTS_LOS` at NE | 1.8 |
| `fp_fiber_attenuation_high` | `IN_PWR_LOW` on a span | `BEFFEC_EXC` on a topology container at same NE; NO `OTS_LOS` on span | 1.8 |
| `fp_rx_power_drift` | `IN_PWR_LOW` @ port | `IN_PWR_ABN` at same port; NO `OTS_LOS` on span; NO `BEFFEC_EXC` on topology container at NE | 1.7 |
| `fp_fec_pre_cor_high` | `BEFFEC_EXC` @ OTU port | `FEC_BEF_COR_ER` at same port; NO `IN_PWR_LOW` on same container | 1.7 |
| `fp_b2_exc_high` | `B2_EXC` @ SDH | NO `OTS_LOS` on supporting span; NO stronger root signature at NE | 1.6 |
| `fp_b1_exc_high` | `B1_EXC` @ SDH | as above | 1.6 |

**Why `NOT` clauses matter.** The simulator's L2 keyword expansion pulls alarms like `POWER_ABNORMAL`, `BEFFEC_EXC`, `IN_PWR_HIGH` into cascades they don't belong to (matching descriptive text like "input power" / "FEC"). A naive trump-by-name rule (`§5` as written in `Design/08`) produces massive false positives. The `NOT Exists ...` guards above are what make the fingerprints precise.

### §3 — Layer-cascade (`layer_cascade.py`)

Within a single port, the upstream layer suppresses the downstream layer:

```
OTS → OMS → OCH → OTU → SDH/FEC/ODU/OPU → client
```

Two refinements:
- `OTS_LOS` and `MUT_LOS` are **dominating** — they suppress every other alarm at the same port regardless of layer (the LOS means the bit stream is gone, so bit-error counts on overlying layers are meaningless).
- For every other layer pair, if `layer(a1) < layer(a2)` at the same port, then `Secondary(a2, a1)`.

### §4 — Topology-grounded cascade (`topology_cascade.py`)

Cross-port propagation that only fires because the topology says so:

| Rule | Conclusion |
|---|---|
| Peer-port `R_LOS` | `R_LOS` at the other end of a span is `Secondary` to its `OTS_LOS` |
| Supporting-link cascade | Every OCH/OTU alarm on a pipe whose `supporting-link` is the faulted span is `Secondary` to the `OTS_LOS` |
| Per-tunnel cascade | `ODU_PM_SSF` / `ODU_PM_BDI` on an ODU whose underlay OCH traverses the span is `Secondary` to the `OTS_LOS` |

### §5 — Equipment trump cards (`equipment.py`)

Hard rules: equipment-level events suppress everything at their location.

| Trump alarm | Scope of suppression |
|---|---|
| `POWER_ABNORMAL` | every alarm at the same NE |
| `BD_OFFLINE`     | every alarm at the same board |
| `HARD_BAD`       | every alarm at the same board |
| `FAN_FAIL`       | every `TEMP_OVER` at the same NE |

**Important:** `§5` rules emit `Secondary` records only. They do **not** pick a root by themselves — only the §8 fingerprints do. This is the load-bearing design call documented at the top of `reasoner.py`: trumping by name alone (as `Design/08 §5` is literally written) would mis-promote L2-expanded `POWER_ABNORMAL` noise to root status.

---

## Programmatic use

```python
from src.simulator import db as simdb, yang_loader
from src.rules import infer_root

topo = yang_loader.load()
conn = simdb.open_db("data/incidents.db")
alarms = conn.execute(
    "SELECT id, alarm_name, ne, board, port, severity, layer, "
    "       container_id, t_offset_ms, t_observed_ms, is_root, group_id "
    "FROM alarms WHERE incident_id=? ORDER BY id",
    (42,),
).fetchall()
# (in production: convert rows to dicts the reasoner expects)

inf = infer_root(incident_id=42, alarms=alarms, topo=topo)
print(f"root  = {inf.root_alarm_name} (id {inf.root_alarm_id})")
print(f"fault = {inf.fault_type}")
print(f"why   = {inf.source}")
for x in inf.inferred[:10]:
    print(f"  {x.kind} alarm={x.alarm_id} parent={x.parent_id} src={x.source}")
```

The reasoner is **stateless** — every call is independent, no setup, ~10 ms per incident at v3 scale (~50 alarms).

### Integration with the streaming KG engine

`src/kg_engine/subscribers/rule_engine.py` wraps `infer_root` and, at each `incident.boundary`, emits a `RootCause` triple tagged `source="rule:fingerprint"`. This is how live alarm cascades from the simulator GUI get their root pick in real time.

### Integration with the batch KG builder

`src/kg/correlation.py` consumes the §2 R1–R4 correlation logic separately (not via this module — R1–R4 are emitted as `Correlated` edges at KG-build time, not at RCA inference time).

---

## Inputs the reasoner expects

### Alarms — list of dicts, one per row of `alarms` table

Required fields (matching `src/simulator/db.py` schema):

| Field | Type | Used by |
|---|---|---|
| `id`            | int | every rule (alarm identity) |
| `alarm_name`    | str | every rule (e.g. `OTS_LOS`, `BEFFEC_EXC`) |
| `ne`            | str | §5, §8 (NE-level scope) |
| `board`         | str | §5, §8 (board-level scope) |
| `port`          | str | §3, §4, §8 (port-level scope) |
| `layer`         | str | §3 (layer ordering) |
| `container_id`  | str | §4, §8 (span / OTU pipe / channel) |
| `t_offset_ms`   | int | fallback only (lowest-layer tie-break) |
| `t_observed_ms` | int | **not used** — kept for symmetry with the ML pipeline |
| `is_root`       | int | `evaluate.py` ground-truth check only |
| `group_id`      | str | `evaluate.py` compound-incident splitting only |

The reasoner deliberately ignores `t_observed_ms`. Per `Design/08 §6` and the project's polling-delay caveat: ~80% of incidents have the root-cause alarm NOT as the chronologically first alarm, so any time-based rule would be wrong most of the time.

### Topology — `src.simulator.topology.Topology`

The whole YANG-loaded topology object (`data/yang/*.json`). Used by §4 and §8 for span / OTU-pipe / OCh-tunnel reasoning:

- `topo.optical_links` — span list (OMS spans)
- `topo.otn_links` — OTU pipes (with `supporting_link_id` referring back to a span)
- `topo.otn_tunnels` — ODU tunnels (with `ero` referring back to OTU pipe names)
- `topo.nes` — NE inventory (boards, ports, peer-port discovery)

---

## Known failure modes

From the 0.9% the reasoner currently misses (per `memory/rules_baseline.md`):

1. **`config_mismatch` 90% class accuracy.** The 10% that fall to `<none>` had a dropped `OPU_PLM` (drop_prob=5%) so the §8.8 three-alarm guard didn't fire. The fallback picks the lowest-layer non-secondary alarm correctly but with empty `fault_type`.
2. **`fiber_cut` 96% class accuracy.** The 4% miss is the same drop-prob symptom — `MUT_LOS` dropped, so the §8.1 fingerprint can't fire even though the `OTS_LOS` is the correct root.
3. **`fec_pre_cor_high` 97%, `rx_power_drift` 98%, `ne_power_off` 99%.** All long-tail drop-prob misses. The root alarm is usually picked correctly via fallback; only the fault class is missing.

These are exactly the cases a GNN can win on — the structural signal (topology + locality + temporal) survives a dropped alarm, but the rules' fingerprint premise doesn't. See `Design/11_gnn_training_plan.md` §10 risks.

---

## Related code

- `src/simulator/` — produces the alarm rows this module consumes.
- `src/kg_engine/subscribers/rule_engine.py` — wraps this module as a live subscriber.
- `src/kg/correlation.py` — implements §2 R1–R4 correlation edges (separate concern from RCA inference).
- `docs/03_rules_catalog.md` — implementation-status table cross-referencing every rule in `Design/08` with the code that realises it.
