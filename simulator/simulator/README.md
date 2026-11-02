# `src/simulator` — Alarm + PPE Generator

This package generates **realistic alarm cascades** and, since v3, **Power Profile Estimation (PPE) traces** for a Huawei OptiX OSN 9800 optical-transport network. You inject a *fault* (fiber cut, board power-off, fan failure, …); the simulator walks the YANG topology, produces the alarms a real NMS would observe, and (since `ppe.py` landed) the matching per-channel longitudinal optical-power curves a coherent receiver would reconstruct from its DBP intermediate fields. Both modalities are persisted to `data/incidents.db` as a labelled training set for the downstream RCA pipeline.

It is **not** an emulator of the equipment firmware. The physical network (topology, services, inventory) lives in three YANG-conformant JSON files under `data/yang/`; the simulator treats those as operational state and runs Python rules against them.

For deeper reading:
- `docs/02_simulator_user_guide.md` — the **operational** user guide (UI walkthrough, SQL recipes, ML workflow).
- `Design/02_simulator_design.md` — the **architectural** design.
- `Design/05_fault_event_catalog.md` — the 11 ★ fault rules and the 21 alarm names the simulator emits.
- `Design/12_ppe_simulation.md` — the PPE generator design.

---

## 1. Quick start

```bash
# Install (works on both bash and PowerShell once the venv exists)
python -m venv .venv
.venv/Scripts/activate           # bash
.venv\Scripts\Activate.ps1       # PowerShell
pip install -r requirements.txt

# (A) Web UI — single-click triggers, live alarm/topology view
python -m src.app.server
# Open http://127.0.0.1:5000/

# (B) Batch CLI — 5120 incidents × ~100 k alarm rows in ~11 s
python -m src.simulator.generate_dataset --clear --samples-per-target 20

# (C) PPE-only plot — see a single fault's per-channel PPE traces
python ppe_demo.py fiber_cut span_OMS3_BC --position-km 25
```

---

## 2. What's in the package

```
src/simulator/
├── yang_loader.py          load data/yang/*.json → in-memory Topology
├── topology.py             Topology / NE / Board / Port / Link / Tunnel dataclasses
├── faults.py               11 fault rules → seed alarms
├── engine.py               L1 forward-walk + L2 keyword expansion
│                           + apply_polling_delay + apply_suppression
├── ppe.py                  11 paired PPE emitters + baseline composer + detector
├── format_alm.py           Huawei NMS dump renderer
├── db.py                   SQLite recorder (incidents + alarms tables)
├── generate_dataset.py     batch generator (CLI)
├── inspect_db.py           dataset summary stats CLI
└── run_demo.py             one-fault smoke test
```

**Inputs** (under `data/`):

| File | Role |
|---|---|
| `data/yang/{topology,tunnels,inventory}.json` | the network (6 hubs A–F, 8 OMS spans, 6 OCh tunnels) |
| `data/alarm_catalog_9800.json` (+ `alarm_catalog.json` fallback) | the 796 / 816 alarm definitions |
| `data/alarm_suppression.json` | 50 curated suppression edges (L1 input + Stage 5) |
| `data/alarms_detail.json` | parsed "Possible Causes" text (L2 keyword input) |

**Outputs** (under `data/`):

- `data/incidents.db` — the dataset (gitignored).
- `ppe_traces` / `ppe_anomalies` tables land in **P-2** (see `Design/12 §12`).

---

## 3. Architecture

```
┌──────────────────┐                       ┌──────────────────────────┐
│  YANG instance   │                       │  Alarm catalogs          │
│  files (data/    │  load topology + ►    │  alarm_catalog_9800.json │
│  yang/*.json)    │  tunnels + inventory  │  alarm_catalog.json (8800)│
└──────────────────┘                       │  alarm_suppression.json  │
        │                                  │  alarms_detail.json      │
        ▼                                  └──────────────────────────┘
┌─────────────────────────────────────────────────────────────────────┐
│ src/simulator/                                                      │
│                                                                     │
│   yang_loader → Topology  (NE / Board / Port / Link / Tunnel)       │
│                                                                     │
│   faults.CATALOG[fault_type] → seed alarms                          │
│                                ─────┬─────────────────────────────  │
│                                     │                               │
│         ┌───────────────────────────┴──────────────────────────┐    │
│         │                                                       │   │
│         ▼ engine.expand_cascade  → 30–60 alarms                 │   │
│           ▸ L1 suppression-graph forward-walk                   │   │
│           ▸ L2 cause-keyword expansion                          │   │
│           ▸ apply_polling_delay  → t_observed_ms (poll jitter)  │   │
│           ▸ apply_suppression    → raw + operator view          │   │
│                                                                 │   │
│         ▼ ppe.generate (paired with same fault_type + args) ────┘   │
│           ▸ baseline(channel, span) → P_0(z)                        │
│           ▸ per-fault δP shape (step / slope / null / rx_offset)    │
│           ▸ Gaussian noise σ(z) = σ_0 + σ_per_km · z_cumulative     │
│           ▸ apply_polling_delay → t_observed_ms (30 s cadence)      │
│                                                                     │
│   db.record  ──────────► data/incidents.db                          │
│                          (incidents, alarms; ppe_* tables in P-2)   │
└─────────────────────────────────────────────────────────────────────┘
                          │
                          ▼
            src/app/server.py (Flask web UI)
            ML / KG pipeline    (src/kg/, src/rules/, src/kg_engine/)
```

The alarm pipeline is documented stage-by-stage in `docs/02_simulator_user_guide.md` §2. The PPE pipeline is documented in `Design/12_ppe_simulation.md`.

---

## 4. The 11 fault rules (alarm + PPE pairings)

Every fault has a `(target_kind, alarm rule, PPE emitter)` triple in `faults.CATALOG` / `ppe.CATALOG`.

| Fault id                 | Target   | Primary alarms                                   | PPE signature                             |
|---|---|---|---|
| `fiber_cut`              | link     | OTS_LOS, MUT_LOS, R_LOS, OTU_SSF, OTU_BDI, ODU_PM_SSF | step −30 dB at `pos_km`, NaN beyond     |
| `fiber_attenuation_high` | link     | IN_PWR_LOW, BEFFEC_EXC, OTU_DEG                  | slope over segment `[a, b]`               |
| `connector_dirty`        | link_z   | none (sub-threshold), or Minor BEFFEC_EXC (≥ 1.5 dB) | localised step 0.5–3 dB at `z_km`  |
| `b1_exc_high`            | link     | B1_EXC, OTU_DEG                                  | clean (electrical — negative example)     |
| `b2_exc_high`            | link     | B2_EXC, OTU_DEG                                  | clean                                     |
| `fec_pre_cor_high`       | link     | BEFFEC_EXC, FEC_BEF_COR_ER, OTU_DEG              | clean                                     |
| `rx_power_drift`         | port     | IN_PWR_LOW or IN_PWR_HIGH, IN_PWR_ABN            | rx_offset ±2 dB on the last sample only  |
| `ne_power_off`           | ne       | POWER_ABNORMAL, OTS_LOS, MUT_LOS, R_LOS, …       | all-degraded sentinels                    |
| `board_power_off`        | board    | BD_OFFLINE, R_LOS                                | OTU → degraded; OA / FIU → clean          |
| `fan_failure`            | ne       | FAN_FAIL, TEMP_OVER                              | small ±0.5 dB drift on drop spans         |
| `config_mismatch`        | port     | OPU_PLM, OTU_TIM, ODU_PM_TIM                     | clean                                     |

`connector_dirty` is the **PPE-first demo fault** — a contaminated or loose fibre connector at a splice/amplifier site. Its magnitude (0.5–3 dB) is deliberately small: most cases produce **zero alarms** (the alarms-only system is blind), yet PPE shows a sharp step at the connector's `z_km`. Only stronger contamination (≥ 1.5 dB) raises a single Minor-severity `BEFFEC_EXC` that is **not** marked `is_root` — the diagnostic signal lives in PPE, not in alarms. This makes it the killer-demo fault for why PPE matters: alarms-only RCA misses every low-magnitude case; PPE-enabled RCA catches all of them, with z_km localisation.

The five "clean PPE" rows are deliberate: alarms fire but the longitudinal profile shows no anomaly — a discriminative basis for the GNN to tell electrical / receiver / config faults apart from optical-medium faults. Every PPE emitter also tags a ground-truth `PpeAnomaly` with `is_root=True` aligned with the alarm side's `is_root`.

---

## 5. How to drive the simulator

### 5.1 Web UI — single-click triggers

```bash
python -m src.app.server                 # http://127.0.0.1:5000/
```

In the browser: pick a fault from the dropdown, pick a target (link / NE / board / port — auto-populated from the loaded topology), optionally toggle **Suppress** (operator vs. raw view), click **Trigger**. One click writes two incident rows (raw + suppressed) to `data/incidents.db` and renders the cascade in the live alarm panel.

For compound (two-root) incidents tick **"Add a second root cause"**, configure R2's fault, optionally set a time offset between the two roots, click Trigger. The alarm panel shows the merged timeline with R1 / R2 chips. Full walk-through: `docs/02_simulator_user_guide.md` §3.1–§3.2.

### 5.2 Batch CLI — ML training scale

```bash
# Default: 5120 incidents × ~100 k alarm rows in ~11 s
python -m src.simulator.generate_dataset --clear --samples-per-target 20

# With knobs
python -m src.simulator.generate_dataset --clear \
    --samples-per-target 100 \   # samples per (fault, target)
    --jitter-ms 15 \             # ±15 ms physical-time jitter per alarm
    --drop-prob 0.05 \           # 5 % chance of dropping any non-root alarm
    --seed 42                    # reproducibility seed
```

The generator walks every `(fault_type × valid target)` pair, takes `samples-per-target` jittered copies, persists both raw + suppressed views per sample. PPE wiring lands in **P-2** (`--with-ppe` flag).

### 5.3 Programmatic — call engine + PPE directly

```python
from src.simulator import yang_loader, faults, engine, ppe

t = yang_loader.load()

# Alarm side
seeds = faults.fiber_cut(t, "span_OMS3_BC")
cascade = engine.expand_cascade(seeds, faults.keywords_for("fiber_cut"))
cascade = engine.apply_polling_delay(cascade)
suppressed = engine.apply_suppression(cascade)

for a in cascade:
    print(f"{a.name:18s}  ne={a.ne:2s}  t_phys={a.t_offset_ms:>4d}ms  "
          f"t_obs={a.t_observed_ms:>5d}ms  root={a.is_root}")

# PPE side (paired with the same fault)
traces = ppe.generate(t, "fiber_cut", "span_OMS3_BC", position_km=25.0)
traces = ppe.apply_polling_delay(traces)
```

The two sides share ground truth — every PPE trace's `group_id` matches the alarms' `group_id`, and `is_root=True` is tagged consistently across both modalities.

### 5.4 PPE-only plotting — no DB write

```bash
python ppe_demo.py <fault_type> <target> [extra-args]
```

Writes `ppe_demo.png` showing per-(channel, span) traces — baseline in grey dashed, observed samples in red, anomalies marked. CLI is deterministic given `--seed`. See §6 for worked examples.

---

## 6. PPE demo gallery

Three representative signatures. All are reproducible with `--seed 7`.

### 6.1 `fiber_cut(span_OMS3_BC, position_km=25)` — the canonical R4 fault

```bash
python ppe_demo.py fiber_cut span_OMS3_BC --position-km 25
```

**One fault, two channels affected:**

| Subplot | Behavior |
|---|---|
| ch2 on OMS2_AB (clean upstream) | Linear loss 0 → −20 dBm at α=0.20 dB/km |
| ch2 on OMS3_BC (the cut span) | Tracks baseline to z=25 km, hard step, NaN beyond ("no light" shaded region) |
| ch4 on OMS5_DB (clean upstream) | Linear loss 0 → −16 dBm across 80 km |
| ch4 on OMS3_BC (same cut) | Identical step at 25 km — same physical fault, second receiver |

The two right-hand subplots are nearly identical because the same cut is observed by two different OCh drop receivers. This is exactly the cross-channel correlation R4 was designed to exploit.

### 6.2 `fiber_attenuation_high(span_OMS3_BC, seg=[15,45], extra=0.15 dB/km)`

```bash
python ppe_demo.py fiber_attenuation_high span_OMS3_BC --seg 15,45 --extra 0.15
```

**The soft-fault slope signature:**

- Red shaded region `[15, 45]` km marks the affected segment.
- Inside it, the observed trace bends **downward** relative to baseline (extra slope of 0.15 dB/km).
- Past 45 km, the trace stays parallel to but **below** baseline — cumulative loss carries forward (~4.5 dB total over the 30 km segment).
- Upstream spans (OMS2_AB, OMS5_DB) are clean — slope is localized to OMS3.

### 6.3 `rx_power_drift(C, direction=low)`

```bash
python ppe_demo.py rx_power_drift C --direction low
```

**The diagnostic distinguisher:**

6 traces, 4 channels (ch1–ch4 all drop at C). Every span profile is clean along its length — but on each channel's **last span** (where it drops at C) the final sample has a sharp −2 dB notch.

This is the signal the rules baseline can't currently produce: `IN_PWR_LOW` fires on the alarm side but the PPE evidence **rules out** a span fault, leaving "receiver-side drift" as the only explanation. P-5's R7 rule encodes exactly this.

### Other fault types worth running

```bash
python ppe_demo.py connector_dirty span_OMS3_BC --z-km 25 --magnitude 1.2  # PPE-only: zero alarms, sharp step
python ppe_demo.py connector_dirty span_OMS3_BC --z-km 40 --magnitude 2.5  # above threshold: 1 Minor alarm + PPE step
python ppe_demo.py ne_power_off C                       # all 5 OChs at C → degraded
python ppe_demo.py b1_exc_high span_OMS3_BC             # alarms fire, PPE is clean
python ppe_demo.py fan_failure C                        # ±0.5 dB drift on each receiver
python ppe_demo.py config_mismatch C                    # clean PPE (config-only fault)
python ppe_demo.py board_power_off C C.OTU              # like ne_power_off but board-scoped
```

---

## 7. The dataset (output of `generate_dataset.py`)

### 7.1 SQLite schema — current (P-1)

`data/incidents.db` has two tables:

- **`incidents`** — one row per (fault, target, sample, view). Columns: `id`, `created_at`, `fault_type`, `fault_target`, `target_kind`, `suppressed`, `topology_id`, `note`.
- **`alarms`** — many rows per incident. Columns: `id`, `incident_id`, `alarm_name`, `ne`, `board`, `port`, `severity`, `layer`, `container_id`, `t_offset_ms`, `t_observed_ms`, `is_root`, `group_id`.

PPE storage (`ppe_traces` + `ppe_anomalies` tables) lands in **P-2** per `Design/12_ppe_simulation.md` §5.

### 7.2 Working with the dataset

Full walkthrough — SQL recipes, choice of training time column, KG construction, compound-incident generation, training tips: `docs/02_simulator_user_guide.md` §§4–6.

---

## 8. Common tasks

### Wipe and regenerate
```bash
python -m src.simulator.generate_dataset --clear --samples-per-target 30
```

### Switch topology (point to a different YANG directory)
```bash
export FMA_YANG_DIR=/path/to/your/yang        # bash
$env:FMA_YANG_DIR = "C:\path\to\your\yang"    # PowerShell
python -m src.simulator.generate_dataset --clear
```

### Inspect a single incident
```bash
python -m src.simulator.format_alm 42       # Huawei NMS dump format
python -m src.simulator.inspect_db          # dataset-wide summary stats
```

### Render PPE for one fault
```bash
python ppe_demo.py fiber_cut span_OMS3_BC --position-km 25 --out my_cut.png
```

### Run the test suite
```bash
python -m pytest tests/
# Or just the PPE generator tests:
python -m pytest tests/test_ppe.py -v
```

---

## 9. PPE generator — quick API reference

```python
from src.simulator import ppe

# Catalog mirrors faults.CATALOG keys exactly.
ppe.CATALOG.keys()
# dict_keys(['fiber_cut', 'fiber_attenuation_high', 'b1_exc_high', ...])

# Dispatch — args mirror faults.<fault_type> exactly.
traces = ppe.generate(t, "fiber_cut", "span_OMS3_BC",
                      position_km=25.0, rng=random.Random(7))

# Baseline composer (used internally, but you can call it directly).
z_km, p0_dbm = ppe.baseline(t, t.wdm_tunnels["och_ch2"], "span_OMS3_BC")

# Anomaly detector — runs on noisy samples; outputs have is_root=False.
detector_view = ppe.detect_anomalies(traces[1])

# Polling-delay jitter (Option A: uniform jitter in [0, 30 s]).
delayed = ppe.apply_polling_delay(traces, rng=random.Random(99))
```

The dataclasses (`PpeTrace`, `PpeAnomaly`) are defined at the top of `ppe.py`. Each trace's `samples` is a `float32[N]` numpy array (N = `length_km / Δz_m`), with NaN values marking "no light" regions. `baseline` is the noiseless reference. `anomalies` carries the emitter-planted ground truth (`is_root=True`).

---

## 10. Where to learn more

| Topic | Document |
|---|---|
| Operational guide (UI walkthrough, SQL recipes, ML workflow) | `docs/02_simulator_user_guide.md` |
| Simulator architecture (the alarm pipeline in depth) | `Design/02_simulator_design.md` |
| YANG schema (incl. PPE-related `nfms:*` augmentations) | `Design/03_yang_topology_plan.md` |
| KG schema + ML plan | `Design/04_knowledge_graph_plan.md` |
| Fault catalog & alarm-name inventory | `Design/05_fault_event_catalog.md` (esp. §8) |
| OSN 9800 vs 8800 alarm differences | `Design/06_osn9800_supplement.md` |
| Lab-mesh topology (v3 6-hub mesh) | `Design/07_lab_mesh_topology.md` |
| Rules baseline (R1–R4 + the planned R5–R7 PPE rules) | `Design/08_root_cause_rules.md` |
| Real-time streaming KG engine | `Design/10_realtime_kg_engine.md` |
| GNN training plan | `Design/11_gnn_training_plan.md` |
| **PPE design + integration plan** | `Design/12_ppe_simulation.md` |
