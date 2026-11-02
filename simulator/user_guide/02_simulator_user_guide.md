# Network Fault Simulator — User Guide

This document is the **operational guide** for the simulator: what it is, how
to drive it, where the alarm data comes from, and how to extract a dataset
for ML / KG training.

For the *architectural* design, see `Design/02_simulator_design.md`.
For the alarm-name inventory and the rationale behind which alarms the
simulator emits, see `Design/05_fault_event_catalog.md`.

---

## 1. What the simulator is

The simulator is a **rule-driven generator** of realistic alarm cascades for
a Huawei OptiX OSN 9800 optical-transport network. You inject a *fault*
(fiber cut, board power-off, fan failure, …); it produces the **alarm
cascade** a real NMS would observe, and it persists every (root cause →
derived alarms) sample to a SQLite database that becomes the labelled
training set for a root-cause-analysis (RCA) ML pipeline.

It is **not** an emulator of the equipment firmware. The physical network
(topology, services, inventory) lives in three YANG-conformant JSON files
under `data/yang/`; the simulator treats those as the operational state and
runs Python rules against them.

```
┌──────────────────┐                       ┌──────────────────────────┐
│  YANG instance   │                       │  Alarm catalogs          │
│  files (data/    │  load topology + ►    │  data/alarm_catalog_     │
│  yang/*.json)    │  tunnels + inventory  │       9800.json (primary)│
└──────────────────┘                       │  data/alarm_catalog.json │
                                           │       (8800 fallback)    │
        │                                  │  data/alarm_suppression  │
        ▼                                  │       .json              │
┌──────────────────────────────────────────┤  data/alarms_detail.json │
│  src/simulator/                          └──────────────────────────┘
│   ├── yang_loader.py     load JSON → in-memory Topology
│   ├── topology.py        Topology / NE / Board / Port / Link / Tunnel
│   ├── faults.py          10 fault rules → seed alarms
│   ├── engine.py          L1 forward-walk + L2 keyword expansion
│   │                      + apply_polling_delay + apply_suppression
│   ├── format_alm.py      Huawei NMS dump renderer
│   ├── db.py              SQLite recorder (incidents + alarms tables)
│   └── generate_dataset.py batch generator (CLI)
└──────────────────────────────────────────┘
        │
        │  writes
        ▼
┌──────────────────────────┐         ┌─────────────────────────────────┐
│  data/incidents.db       │  ◄──┐   │  src/app/server.py (Flask)      │
│  (the dataset)           │     │   │  + src/app/static/* (web UI)    │
└──────────────────────────┘     │   │  + GUI trigger / dump endpoints │
                                 └───┴─────────────────────────────────┘
```

---

## 2. How alarms are generated — the pipeline

For one fault click, the simulator walks five stages in order:

### Stage 1 — Fault rule produces *seed* alarms

`src/simulator/faults.py` exports `CATALOG` mapping each fault id to a
Python rule. The rule walks the topology to figure out *where* on the
network the fault manifests and emits a small list of seed alarms — the
"things a real NMS would see at the physical fault location":

| Fault id                | Seeds emitted                                 |
|---|---|
| `fiber_cut`             | OTS_LOS, MUT_LOS, R_LOS, OTU_SSF, OTU_BDI, ODU_PM_SSF |
| `fiber_attenuation_high`| IN_PWR_LOW, BEFFEC_EXC, OTU_DEG               |
| `b1_exc_high`           | B1_EXC, OTU_DEG                               |
| `b2_exc_high`           | B2_EXC, OTU_DEG                               |
| `fec_pre_cor_high`      | BEFFEC_EXC, FEC_BEF_COR_ER, OTU_DEG           |
| `rx_power_drift`        | IN_PWR_LOW or IN_PWR_HIGH, IN_PWR_ABN          |
| `ne_power_off`          | POWER_ABNORMAL, OTS_LOS, MUT_LOS, R_LOS, OTU_SSF, ODU_PM_SSF |
| `board_power_off`       | BD_OFFLINE, R_LOS                             |
| `fan_failure`           | FAN_FAIL, TEMP_OVER                           |
| `config_mismatch`       | OPU_PLM, OTU_TIM, ODU_PM_TIM                  |

Exactly one seed per fault is flagged `is_root=True` — the **root-cause
alarm**. Everything else (in this stage and the next two) is a *derived*
alarm.

### Stage 2 — L1: suppression-graph forward-walk

`engine.expand_l1()` reads `data/alarm_suppression.json` (50 expert-curated
edges from the OSN reference manual) and, for every seed, transitively adds
every alarm that the suppression graph reaches forward — at the same NE /
board / port / container as the seed. Each L1 child gets its own
`t_offset_ms` (5 ms increments per hop) and inherits the seed's location.

### Stage 3 — L2: cause-keyword expansion

`engine.expand_l2()` looks up every alarm whose "Possible Causes" text in
`data/alarms_detail.json` matches the fault's declared keyword set
(e.g. `fiber_cut` declares `{"fiber", "los"}`). Matches are emitted at the
first seed's location with a small `t_offset_ms` and `is_root=False`.

After Stages 1–3, a typical cascade has **30–60 alarms** — vs the rule's 6
seeds. That richness is what the ML model sees.

### Stage 4 — Polling-delay model (NMS observation time)

Real NMSes don't see an alarm the instant the underlying physical event
happens — they poll each layer / board on its own timer. We model this by
adding, per alarm, a uniform `[0, cadence)` ms delay to the physical
`t_offset_ms` to derive a separate `t_observed_ms`:

| Layer       | Poll cadence | Notes                          |
|---|---:|---|
| OTS, OMS    |  5 s         | optical-section: fast          |
| OCH         | 10 s         |                                 |
| OTU, SDH    | 30 s         |                                 |
| ODU, FEC, client, protection, auto-power, clock, management | 60 s | |
| equipment (BD, POWER, HARD, …) | 120 s | every ~2 min |
| environment (FAN, TEMP, AMBIENT, …) | 300 s | every ~5 min |

**Effect on the dataset** (against the current 5120-incident run):

- **80.4 %** of incidents — the root-cause alarm is *not* the
  chronologically first observed alarm in the cascade.
- **26.0 %** — the root sits in the *last quartile* of the observed
  timeline.

Both `t_offset_ms` (physical, ground truth) and `t_observed_ms`
(observed-by-NMS, post-jitter) are persisted on every alarm row, so the ML
pipeline gets to choose which to train on, and you can debug against the
clean physical order whenever needed.

### Stage 5 — Suppression filter

`engine.apply_suppression()` produces the operator-friendly view by
dropping every alarm whose any-upstream in the suppression graph is also
present in the cascade. Both views (`raw` = unsuppressed, `suppressed` =
operator) are recorded; `incidents.suppressed` is a boolean column.

### What about compound incidents?

When you trigger two faults near-simultaneously, the engine runs Stages 1–3
**independently per fault** with a per-fault `group_id` (`"R1"`, `"R2"`,
…), then concatenates the cascades, then runs Stages 4–5 once on the
combined set. Every alarm row carries its `group_id` so derived alarms can
be traced back to which root they descend from.

---

## 3. Three ways to generate alarms

### 3.1 Web UI — single click

```bash
# From D:\NetworkFaultManagement\
python -m src.app.server
# Open http://127.0.0.1:5000/
```

In the UI you can:

1. **Pick a fault** from the dropdown (10 faults, see §2)
2. **Pick a target** (link / NE / board / port — auto-populated from the
   loaded topology)
3. Optionally toggle **Suppress** (operator vs. raw view)
4. Click **Trigger**

One click produces:
- Two incident rows in `data/incidents.db` (raw + suppressed)
- The alarm cascade rendered in the live alarm panel
- The affected NEs / spans highlighted on the topology canvas
- A `.txt` link in the incident-history sidebar that downloads the same
  cascade in **Huawei NMS dump format** (`ALM-CUR-DATA-ALL`).

### 3.2 Web UI — compound incident (two roots at once)

In the trigger panel:

1. Configure root cause **R1** as above
2. Tick **"Add a second root cause (compound incident)"**
3. The R2 fault block appears — pick its fault type + target
4. Optionally set a **time offset** (default `0` = simultaneous; set
   `30000` for a 30-second gap between R1's and R2's physical times)
5. Click **Trigger**

The alarm panel now shows:
- A two-tag legend: `R1 fiber_cut on span_OMS3_BC | R2 ne_power_off on E`
- Each row tinted blue (R1) or orange (R2)
- A **Group** column with the chip
- The two root rows (`is_root=★`) highlighted in their group's darker tint

Use this to inspect how derived alarms from independent root causes mix in
the timeline once polling jitter is applied — it's the realistic
"two-things-broke-at-once" case the RCA pipeline has to handle.

### 3.3 Batch generator — for ML training scale

```bash
# Generate ~5000 incidents with default knobs
python -m src.simulator.generate_dataset --clear --samples-per-target 20

# Common knobs:
python -m src.simulator.generate_dataset \
    --clear \
    --samples-per-target 100 \   # 100 jittered samples per (fault, target)
    --jitter-ms 15 \             # ±15 ms physical-time jitter per alarm
    --drop-prob 0.05 \           # 5 % chance of dropping any non-root alarm
    --seed 42                    # reproducibility seed
```

The generator iterates **every fault × every valid target** and, for each
combo, writes `samples-per-target` jittered samples × {raw, suppressed} =
`samples-per-target × 2` rows. With `--samples-per-target 20` against the
v3 lab mesh you get **5120 incidents**, **~100 000 alarm rows**,
**95 distinct alarm names** in ~11 seconds.

The generator does not currently emit compound (multi-fault) incidents in
batch — it walks one fault at a time. For compound training data, drive
`/api/trigger_multi` from a Python loop (see §6.3).

### 3.4 Programmatic — call the engine directly

```python
from src.simulator import yang_loader, faults, engine

t = yang_loader.load()
seeds = faults.fiber_cut(t, "span_OMS3_BC")
raw = engine.expand_cascade(seeds, faults.keywords_for("fiber_cut"))
raw = engine.apply_polling_delay(raw)         # adds t_observed_ms
suppressed = engine.apply_suppression(raw)

for a in raw:
    print(f"{a.name:18s}  ne={a.ne:2s}  t_phys={a.t_offset_ms:>4d}ms  "
          f"t_obs={a.t_observed_ms:>5d}ms  root={a.is_root}")
```

---

## 4. The dataset — schema, columns, location

### 4.1 Where it lives

| Path                          | What it is                                 |
|---|---|
| `data/incidents.db`           | SQLite DB — the actual training set       |
| `data/yang/topology.json`     | network topology (6 hubs, 8 OMS spans)    |
| `data/yang/tunnels.json`      | OCh + ODU tunnels (`ch1`..`ch6`)          |
| `data/yang/inventory.json`    | NEs, boards, ports                        |
| `data/alarm_catalog_9800.json`| **Primary** alarm catalog (796 entries)   |
| `data/alarm_catalog.json`     | OSN 8800 fallback (816 entries)           |
| `data/alarm_suppression.json` | 50 suppression edges (Stage 5 + L1 input) |
| `data/alarms_detail.json`     | "Possible Causes" text (L2 input)         |

`incidents.db` is **gitignored** — every clone re-generates it locally
with `python -m src.simulator.generate_dataset --clear`. The three YANG
files and four catalogs *are* committed.

### 4.2 Schema

Two tables. One row per incident; many rows per alarm.

#### `incidents`

| Column         | Type        | Meaning                                       |
|---|---|---|
| `id`           | INTEGER PK  | incident id (autoincrement)                   |
| `created_at`   | TEXT (ISO)  | when the incident was triggered                |
| `fault_type`   | TEXT        | one of the 10 fault ids — **the ML label**    |
| `fault_target` | TEXT        | which link/NE/board/port was targeted         |
| `target_kind`  | TEXT        | `link` / `ne` / `board` / `port` / `multi`    |
| `suppressed`   | INTEGER 0/1 | view: 1 = operator-friendly, 0 = raw cascade  |
| `topology_id`  | TEXT        | hash of the YANG (reproducibility check)      |
| `note`         | TEXT        | free text (e.g. "GUI trigger", "batch seed=…")|

For compound incidents `fault_type` is the `+`-joined ids
(`fiber_cut+ne_power_off`) and `fault_target` packs both
(`R1:span_OMS3_BC | R2:E`).

#### `alarms`

| Column          | Type    | Meaning                                          |
|---|---|---|
| `id`            | INTEGER PK | alarm row id                                  |
| `incident_id`   | INTEGER FK | parent `incidents.id`                         |
| `alarm_name`    | TEXT    | e.g. `OTS_LOS`, `OTU_SSF`, `ODU_PM_SSF`         |
| `ne`            | TEXT    | NE id where the alarm fires (`A`..`F`)          |
| `board`         | TEXT    | board uuid (may be empty for NE-level alarms)   |
| `port`          | TEXT    | port uuid (may be empty)                        |
| `severity`      | TEXT    | `Critical` / `Major` / `Minor` / `Warning`      |
| `layer`         | TEXT    | `OTS` / `OMS` / `OCH` / `OTU` / `ODU` / …       |
| `container_id`  | TEXT    | span id / tunnel id this alarm is bound to     |
| `t_offset_ms`   | INTEGER | **physical** propagation time (ground truth)    |
| `t_observed_ms` | INTEGER | **observed-by-NMS** time (after poll jitter)    |
| `is_root`       | INTEGER 0/1 | `1` for the root-cause alarm of its group   |
| `group_id`      | TEXT    | `"R1"` / `"R2"` / … — which root it descends from |

The `layer`, `container_id`, `t_offset_ms`, `t_observed_ms`, `group_id` and
`is_root` columns are exactly what the four R1–R4 alarm-correlation rules
plus the propagation-graph mining need (see
`Design/02_simulator_design.md` §10).

---

## 5. Where to get the dataset for ML training

Five paths, ordered from "I want it now" to "I want a custom build".

### 5.1 The SQLite file directly

```bash
# After running the generator — it lives at:
D:\NetworkFaultManagement\data\incidents.db
```

Read it with `sqlite3` CLI, DBeaver, Python's `sqlite3` module, or pandas:

```python
import sqlite3, pandas as pd
conn = sqlite3.connect("data/incidents.db")
incidents = pd.read_sql("SELECT * FROM incidents", conn)
alarms    = pd.read_sql("SELECT * FROM alarms",    conn)
# 5120 incidents, ~100k alarm rows for the default --samples-per-target 20
```

### 5.2 ZIP export from the web UI

Click **Export ↓** in the header. The browser downloads
`incidents_export.zip` containing:

- `incidents.csv`
- `alarms.csv`
- `alarm_catalog.json` (OSN 8800)
- `alarm_suppression.json`

### 5.3 Per-incident Huawei NMS dump (`.txt`)

In the incident-history sidebar each entry has a `.txt` chip that
downloads the cascade rendered as a Huawei `ALM-CUR-DATA-ALL` table —
matching the format of `Library/Alm_format.txt`.

```bash
# Same render from the CLI:
python -m src.simulator.format_alm <incident_id> > incident_42.txt
```

This format is useful for:
- comparing simulator output against a real lab dump byte-for-byte
- feeding tools that already parse Huawei NMS dumps
- inspecting one cascade in isolation

### 5.4 Direct SQL queries — the recommended path for KG / ML

The schema is designed so the propagation-graph and correlation-rule
queries are one-liners. The five most useful queries:

```sql
-- (a) propagation-graph edge candidates: (root, derived, weight)
-- This is the ~5000-edge propagation graph the project notes describe.
SELECT root.alarm_name AS root_alarm,
       der.alarm_name  AS derived_alarm,
       count(*)        AS n_incidents
FROM alarms root
JOIN alarms der ON der.incident_id = root.incident_id AND der.id != root.id
JOIN incidents s ON s.id = root.incident_id
WHERE root.is_root = 1
  AND root.group_id = der.group_id    -- only co-occurrence within same root
  AND s.suppressed = 0
GROUP BY root.alarm_name, der.alarm_name;

-- (b) ML class -> root-alarm mapping (the label table)
SELECT s.fault_type, a.alarm_name, count(*)
FROM incidents s JOIN alarms a ON a.incident_id = s.id
WHERE a.is_root = 1 AND s.suppressed = 0
GROUP BY s.fault_type, a.alarm_name;

-- (c) topology-grounded edges (span → OTU layer cascade)
SELECT root.container_id AS root_cont,
       der.container_id  AS der_cont,
       count(*)
FROM alarms root
JOIN alarms der ON der.incident_id=root.incident_id AND der.id!=root.id
WHERE root.is_root=1 AND root.container_id != der.container_id
GROUP BY root.container_id, der.container_id;

-- (d) one cascade as an ML feature row
SELECT alarm_name, layer, ne, port, container_id,
       t_offset_ms, t_observed_ms, is_root, group_id
FROM alarms
WHERE incident_id = ?
ORDER BY t_observed_ms;     -- or t_offset_ms for ground-truth order

-- (e) suppression-effect query: how many alarms a fault collapses
SELECT s.fault_type,
       sum(CASE WHEN s.suppressed=0 THEN 1 ELSE 0 END) AS raw_alarms,
       sum(CASE WHEN s.suppressed=1 THEN 1 ELSE 0 END) AS sup_alarms
FROM incidents s JOIN alarms a ON a.incident_id=s.id
GROUP BY s.fault_type;
```

### 5.5 Joining with the YANG topology

Every column in `alarms` is a foreign key into the YANG entity space.

| `alarms` column         | YANG file        | YANG path                                       |
|---|---|---|
| `ne`                    | `inventory.json` | `network-element/uuid`                          |
| `board`                 | `inventory.json` | `component/uuid` (class `module`)               |
| `port`                  | `inventory.json` | `component/uuid` (class `port`)                 |
| `container_id = span_*` | `topology.json`  | `network[optical-topo]/link/link-id`            |
| `container_id = otu_*`  | `topology.json`  | `network[otn-topo]/link/link-id`                |
| `container_id = och_*`  | `tunnels.json`   | `tunnel[type=wdm-tunnel]/name`                  |
| `container_id = odu_*`  | `tunnels.json`   | `tunnel[type=otn-tunnel]/name`                  |

Load the YANG with `src.simulator.yang_loader.load()` and you get a
`Topology` object whose `.optical_links / .otn_links / .otn_tunnels /
.wdm_tunnels / .nes / .ports` dicts are keyed by exactly these strings —
so a join is one Python dict lookup.

---

## 6. Recommended ML / KG workflow

### 6.1 Reset and generate at scale

```bash
python -m src.simulator.generate_dataset --clear --samples-per-target 100
# Wait ~1 minute. You now have ~25 000 incidents, ~500 000 alarm rows.
```

### 6.2 Decide which time column to train on

| Use this column | When                                                |
|---|---|
| `t_observed_ms` | **Default for ML training.** This is what the operator/NMS sees; reflects realistic polling jitter. |
| `t_offset_ms`   | When you want clean physical propagation order — e.g. validating that a learned model behaves consistently with the propagation graph; or for debugging. |

Note: if you train on `t_offset_ms` the model will pick up the
(artificial) "earliest alarm = root cause" shortcut and won't generalise.
Train on `t_observed_ms` and use `t_offset_ms` only for validation.

### 6.3 Generate compound incidents for multi-root RCA

The batch generator only does single-fault. For compound, drive the API:

```python
import requests, random

faults = ["fiber_cut", "ne_power_off", "fan_failure",
          "config_mismatch", "rx_power_drift"]
spans  = ["span_OMS1_AC", "span_OMS3_BC", "span_OMS6_DE",
          "span_OMS7_EF", "span_OMS8_FC"]
nes    = ["A", "B", "C", "D", "E", "F"]

for _ in range(500):
    f1 = {"fault_type": "fiber_cut", "args": [random.choice(spans)]}
    f2 = {"fault_type": random.choice(["ne_power_off", "fan_failure"]),
          "args":       [random.choice(nes)]}
    requests.post("http://127.0.0.1:5000/api/trigger_multi",
                  json={"faults": [f1, f2],
                        "delay_ms": random.randint(0, 10_000)})
```

The Flask server stays up; every request adds two incidents (raw + sup)
to `data/incidents.db`.

### 6.4 Build the knowledge graph

Combine `incidents.db` with the three YANG files:

- **structural triples** (from YANG): `(span_OMS3_BC, between, B)`,
  `(otu_OMS3_BC, supportedBy, span_OMS3_BC)`,
  `(och_ch2, traverses, otu_OMS3_BC)`,
  `(odu_ch2, ridesOn, och_ch2)`, …
- **behavioural triples** (from `incidents.db` query (a) above):
  `(OTS_LOS, propagatesTo, R_LOS, weight=0.95)`, …
- **grounding triples** (joining the two):
  `(alarm:OTS_LOS@span_OMS3_BC, locatedAt, span_OMS3_BC)`, …

`fault_management_agent/knowledge-engine/knowledge_gateway.py` already
implements the **structural** half — it walks the YANG and emits ~231
triples. The behavioural half (querying `incidents.db` for propagation
edges) is the next step (#5 in the project task list).

### 6.5 Train a baseline classifier

A first-pass model:

- **Input features** per incident:
  - bag-of-alarm-names (95-dim one-hot vector)
  - layer counts (13 layers)
  - `(ne, container_id)` co-occurrence vector
  - earliest / median / latest `t_observed_ms`
- **Label**: `incidents.fault_type` (one of 10)
- **Model**: gradient boosting (`xgboost` or `lightgbm`)
- **Split**: by `(fault_type, fault_target)` so the test set sees
  fault-target combos the train set never saw — mimics the real-world
  use-case of an unseen fiber span being cut.

A GNN over the propagation graph + topology subgraph is the upgrade path.

---

## 7. Common operational tasks

### 7.1 Wipe the dataset and start over

```bash
rm data/incidents.db
python -m src.simulator.generate_dataset --clear --samples-per-target 30
```

Or from the web UI: click **Clear DB** in the header.

### 7.2 Switch to a different topology

Drop your `topology.json` / `tunnels.json` / `inventory.json` into a
directory and:

```bash
export FMA_YANG_DIR=/path/to/your/yang   # Linux/macOS
# or PowerShell:    $env:FMA_YANG_DIR = "C:\path\to\your\yang"
```

Restart the server / re-run the generator.

### 7.3 Inspect one incident quickly

Web UI: click the incident row in the history sidebar — it loads the
cascade into the alarm panel.

CLI:

```bash
python -m src.simulator.format_alm 42 | head -40
```

### 7.4 Dataset stats at a glance

```bash
python -m src.simulator.inspect_db
# (or just the generator's tail-summary; --quiet to skip per-fault lines)
```

---

## 8. Quick cheat-sheet

```bash
# Launch web UI on :5000
python -m src.app.server

# Batch-generate for ML training
python -m src.simulator.generate_dataset --clear --samples-per-target 100

# Render incident 42 in Huawei NMS dump form
python -m src.simulator.format_alm 42

# Query (root, derived, count) edges
sqlite3 data/incidents.db <<'SQL'
SELECT r.alarm_name, d.alarm_name, count(*)
FROM alarms r JOIN alarms d
  ON d.incident_id=r.incident_id AND d.id!=r.id AND d.group_id=r.group_id
JOIN incidents s ON s.id=r.incident_id
WHERE r.is_root=1 AND s.suppressed=0
GROUP BY r.alarm_name, d.alarm_name
ORDER BY count(*) DESC LIMIT 20;
SQL
```

---

## 9. Where to learn more

| Topic                                | Document                                        |
|---|---|
| Simulator architecture               | `Design/02_simulator_design.md`                 |
| YANG schema                          | `Design/03_yang_topology_plan.md`               |
| KG schema + ML plan                  | `Design/04_knowledge_graph_plan.md`             |
| Fault catalog & alarm-name inventory | `Design/05_fault_event_catalog.md` (esp. §8)    |
| OSN 9800 vs 8800 alarm differences   | `Design/06_osn9800_supplement.md`               |
| Lab mesh topology (real-equipment origin) | `Design/07_lab_mesh_topology.md`           |
| 6-agent RCA framework on top         | `fault_management_agent/fault_management_agent_framework/README.md` |
