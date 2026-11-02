# Network Simulator — Design Document

Status: **v3 — current.** This document describes the simulator as shipped in v3 (lab-mesh topology + OSN 9800 alarm catalog). Older v1 / v2 worked-examples (4-NE diamond+crossbar, OSN 8800 catalog) are kept inline only where they are illustrative — the authoritative v3 inputs are noted alongside.
Scope: Phase-2 deliverable. Phase-1 (PDF analysis, alarm catalog) is complete; the v3 multi-agent runtime now lives in `fault_management_agent/`; ML training is still pending.

### What changed in v3 (vs. v2)

- **Topology**: replaced the 4-NE diamond+crossbar (`ne1`..`ne4`) with the **lab mesh**: 6 hubs **A–F**, 8 OMS line systems (one span each — `span_OMS1_AC` … `span_OMS8_FC`), 6 OCh tunnels (`ch1`..`ch6`), 6 ODU client services (`ch1`..`ch6`). See `Design/07_lab_mesh_topology.md` for the lab origin and `data/yang/{topology,tunnels,inventory}.json` for the encoded form.
- **Alarm catalog**: switched from OSN 8800 (`data/alarm_catalog.json`, 816 entries) to **OSN 9800** (`data/alarm_catalog_9800.json`, 796 entries) as the primary. 8800 is retained as a fallback for the few names 9800 doesn't ship (e.g. `FEC_BEF_COR_ER`). See `Design/05_fault_event_catalog.md` §8 for the per-alarm resolution table.
- **Fault catalog**: implemented the 10 ★ priority faults from `Design/05_fault_event_catalog.md` §7 — `fiber_cut`, `fiber_attenuation_high`, `b1_exc_high`, `b2_exc_high`, `fec_pre_cor_high`, `rx_power_drift`, `ne_power_off`, `board_power_off`, `fan_failure`, `config_mismatch`. The 8 unstarred faults from §4.2–§4.4 are the v4 backlog.
- **Cross-service correlation demo**: the R4 reference incident is now **`fiber_cut(span_OMS3_BC)`** — both `ch2` and `ch4` traverse `span_OMS3_BC`, so a single span cut fires correlated alarm cascades on two distinct services. (v2's equivalent was `fiber_cut(span_23)` taking down `svc_A` + `svc_B`.)

---

## 1. Purpose

Build a controllable, *visible* simulation of a Huawei OptiX OSN optical-transport network so that we can:

1. **Inject faults** (e.g. fiber cut, board power-off, fan failure) by clicking a button on a GUI.
2. **Observe the resulting alarm cascade** as it would appear on a real NMS — both the raw cascade and the suppressed view.
3. **Record every (root-cause event → resulting alarms) sample** into a database that becomes the **labelled training set** for the **alarm-propagation knowledge graph** described in §10.

The simulator is *not* an NMS or an emulator of the equipment firmware. It is a **rule-driven generator** of realistic alarm incidents, grounded in the alarm-suppression and signal-flow rules already extracted from the two PDFs in Phase 1.

The downstream consumer of the simulator's output is the RCA pipeline 「**基于告警传播关系和图搜索的根因推理**」 sketched in `Library/Picture1.png` and `Library/Root cause analysis of alarms.txt`: alarm-correlation group → propagation-graph reasoning → supplementary-rule reasoning → root-cause alarm. §10 describes that pipeline and how the simulator's records map onto it.

## 2. Functional Requirements

What a user can do via the GUI:

| # | Capability |
|---|---|
| F1 | View the current network topology (NEs, fiber spans, services). |
| F2 | Click a **fault button** to trigger a fault on a chosen target (NE / board / link / port). |
| F3 | See the **alarms** that the fault produced, with location, severity, and timestamp, in a live panel. |
| F4 | Toggle **suppression on / off** to see the operator-friendly view vs. the raw cascade. |
| F5 | Browse the **incident history** — every fault that has been triggered in the session. |
| F6 | **Clear** the current alarm state (return all NEs to healthy). |
| F7 | **Export** the accumulated dataset as JSON / CSV for ML training. |

What the user *cannot* do in v1 (out of scope, possibly later):
- Edit topology graphically (must edit `topology.json`).
- Compose multiple simultaneous faults.
- Replay a previous incident.
- Configure protection schemes (1+1, SNCP).

## 3. System Architecture

```
┌───────────────────────────────────────────────────────────────────────┐
│                            BROWSER  (frontend)                        │
│   ┌──────────────┐   ┌────────────────┐   ┌────────────────────┐      │
│   │ Topology     │   │ Fault          │   │ Alarm panel        │      │
│   │ canvas       │   │ buttons        │   │ + incident history │      │
│   │ (vis-network)│   │ (HTML/JS)      │   │ (HTML table)       │      │
│   └──────┬───────┘   └────────┬───────┘   └─────────┬──────────┘      │
│          │   GET /topology    │   POST /trigger     │ GET /alarms     │
│          │   GET /faults      │                     │ GET /incidents  │
└──────────┼────────────────────┼─────────────────────┼─────────────────┘
           ▼                    ▼                     ▼
┌───────────────────────────────────────────────────────────────────────┐
│                         FLASK HTTP SERVER                             │
│   src/app/server.py        — REST endpoints, serves static frontend   │
└──────────┬────────────────────────────────────────────────────────────┘
           │  Python imports
           ▼
┌──────────────────────────────┐  ┌──────────────────────────────────┐
│  SIMULATOR  (src/simulator)  │  │   DATA  (data/)                  │
│  ┌────────────────────────┐  │  │  yang/{topology,tunnels,         │
│  │ yang_loader.py loads   │◀─┼──│         inventory}.json (v3 lab) │
│  │   NEs, spans, tunnels  │  │  │  alarm_catalog_9800.json (v3)    │
│  └────────────┬───────────┘  │  │  alarm_catalog.json (8800 fallback)│
│               │              │  │  alarm_suppression.json (Phase 1)│
│               │              │  └──────────────────────────────────┘
│               │              │
│  ┌────────────▼───────────┐  │  ┌──────────────────────────────────┐
│  │ faults.py    fault     │  │  │   DATABASE  (SQLite)             │
│  │              catalog + │  │  │  data/incidents.db               │
│  │              rules     │  │  │   ┌────────────┐ ┌────────────┐  │
│  └────────────┬───────────┘  │  │   │ incidents  │ │ alarms     │  │
│               │              │  │   └────────────┘ └────────────┘  │
│  ┌────────────▼───────────┐  │  └──────────────────────────────────┘
│  │ engine.py   propagate, │──┼─────────────────────────────────▶ writes
│  │             suppress   │  │
│  └────────────┬───────────┘  │
│               │              │
│  ┌────────────▼───────────┐  │
│  │ db.py        record    │  │
│  └────────────────────────┘  │
└──────────────────────────────┘
```

**Data flow for one button click:**

1. User clicks "Fiber cut span_A_to_1" → browser sends `POST /api/trigger {fault: "fiber_cut", target: "span_A_to_1"}`.
2. Server calls `engine.trigger(fault, target)`.
3. Engine looks up the fault rule, walks the topology to figure out *where* alarms appear, applies the suppression filter (loaded from `alarm_suppression.json`), produces an `Alarm[]`.
4. Engine asks `db.record_incident(...)` to persist the (root-cause, alarms) row pair.
5. Server returns the alarm list to the browser.
6. Browser updates the alarm panel and highlights affected nodes/links on the topology canvas.

## 4. Technology Stack

| Layer | Choice | Why |
|---|---|---|
| Language | **Python 3.10+** | Already used for PDF parsing in Phase 1; one language across the project. |
| Web framework | **Flask** | Smallest server we can use that does REST + static-file serving cleanly. No build step. |
| Persistence | **SQLite (stdlib `sqlite3`)** | Zero-install. The dataset will be ≤ 100K rows — SQLite is more than enough. Easy to export to CSV / Pandas DataFrame for the future ML pipeline. |
| Topology viz | **vis-network** (via CDN) | A few lines of JS produces a draggable, click-aware graph — good fit for showing NEs and fiber spans. |
| Frontend code | **Vanilla HTML + CSS + JS** | No React, no build tools. The page is small enough that this stays simple and review-friendly. |
| Optional | **pandas** for export only | Used by the export endpoint to write CSV. Already a common dependency. |

Rejected alternatives:
- *FastAPI* — fine, but Flask's simpler stdlib-feel is enough here.
- *PyQt / Tkinter* — desktop-only, harder to demo, no cheap topology widget.
- *Streamlit* — too rigid for the topology + buttons + live alarms layout we want.
- *Postgres / MySQL* — overkill for a single-user demo.

## 5. Topology Model (YANG / data-driven)

The topology is encoded as YANG-conformant JSON instance documents per `Design/03_yang_topology_plan.md` — three files at `data/yang/{topology,tunnels,inventory}.json`. The simulator's `yang_loader.py` materialises them into in-memory `Topology` objects.

### 5.1 v3 — lab-mesh topology (current)

The v3 topology is captured from a Huawei OSN 9800 reference deployment — see `Design/07_lab_mesh_topology.md` for the source diagram and rationale. Six WSS hubs labelled **A–F**, eight OMS line systems (one span each), six OCh tunnels (`ch1`..`ch6`) carrying matching ODU client services with `nfms:client-service-name = "chN"`:

```
              A ──── span_OMS2_AB ───── B
              │                         │
        span_OMS1_AC               span_OMS5_BD
              │                         │
              C ──── span_OMS4_DC ───── D
              │ \                       │
              │  \                  span_OMS6_DE
   span_OMS8_FC   span_OMS3_BC          │
              │     (B↔C)               E
              │                         │
              F ──── span_OMS7_EF ───── E

   Service paths (each ODU rides a single OCh; per data/yang/tunnels.json):
       ch1 :  A ─OMS1→ C
       ch2 :  A ─OMS2→ B ─OMS3→ C       ─┐
       ch3 :  A ─OMS2→ B ─OMS5→ D        │ ch2 + ch4 share OMS3_BC
       ch4 :  D ─OMS4→ C ─OMS3→ B        │   → R4 cross-service demo
       ch5 :  D ─OMS6→ E                ─┘
       ch6 :  F ─OMS7→ E
```

R4 demo (cross-service alarm correlation, §10.1): cutting `span_OMS3_BC` brings down **both** `ch2` and `ch4`. Each service produces its own alarm cascade, but the cascades share the same physical root cause — the canonical input to the propagation-graph reasoning step.

Each NE carries the standard board set (`OTU`, `OA`, `FIU`, `SCC`, `FAN`); each board has named ports. Links are **bidirectional fiber-span pairs** at the optical layer, with an OTU-pipe link in the OTN-layer topology stacked over each via `supporting-link`. Services are realised as ODU tunnels with the `nfms:client-service-name` tag set to the corresponding channel id.

### 5.2 v2 — diamond+crossbar (historical)

The v2 simulator ran on a **4-NE diamond+crossbar** (`ne1`..`ne4`) with two SDH STM-64 services sharing `span_23`. The v2 YANG instance files are preserved at `data/yang/v2/` for regression testing. The R4 demo there cut `span_23` to take down `svc_A` + `svc_B`.

Swapping in a different topology means editing the three YANG JSON files (or pointing `FMA_YANG_DIR` at a different directory) — no code change.

## 6. Fault Catalog (v3 — 10 ★ priority faults)

Each fault is a "button" the user can click. Faults are grouped by what they target. The full catalogue (the 10 implemented + 8 v4-backlog faults) lives in `Design/05_fault_event_catalog.md` §4 and §7; the table below lists what `src/simulator/faults.py::CATALOG` actually exposes.

| ID | Display name | Target | Models which root cause |
|---|---|---|---|
| `fiber_cut` | Fiber cut | a link | Service interruption — fiber severed |
| `fiber_attenuation_high` | Fiber attenuation high | a link | Soft loss — dirty connector / aged fiber |
| `b1_exc_high` | B1 byte errors over threshold | a link | Regenerator-section bit errors |
| `b2_exc_high` | B2 byte errors over threshold | a link | Multiplex-section bit errors |
| `fec_pre_cor_high` | FEC pre-correction over threshold | a link | OTN FEC pre-correction degradation |
| `rx_power_drift` | RX optical power drift | an NE+board+port | Light-signal drift on a line port |
| `ne_power_off` | NE power off | an NE | Service interruption — NE-wide power supply |
| `board_power_off` | Board power off | an NE+board | Service interruption — single board power |
| `fan_failure` | Fan failure | an NE | Environment — fan stops |
| `config_mismatch` | Configuration mismatch | an NE+board+port | Wavelength / rate / trail-id misconfiguration |

Each fault is a Python rule of the shape:

```python
def rule(topology, *target_args) -> list[Alarm]:
    """Return the *seed* alarms for this fault, before L1/L2 expansion."""
```

The rule walks the topology to compute *where* (which NE / board / port) each alarm appears, then attaches severity from the alarm catalog. The web UI's description lookup (`src/app/server.py::_description_for`) queries `data/alarm_catalog_9800.json` first and falls back to `data/alarm_catalog.json` (8800) for any name 9800 doesn't ship.

The 8 v4-backlog faults (`wavelength_drift`, `oa_pump_failure`, `oa_gain_drift`, `tx_laser_die`, `board_hardware_fault`, `board_type_mismatch`, `laser_module_mismatch`, `ambient_temp_high`, `protection_switch_failure`, `ne_unreachable`) are documented in `Design/05_fault_event_catalog.md` §8.4.

## 7. Alarm Propagation Engine

A two-stage pipeline:

**Stage 1 — Generation.** The fault rule produces every alarm a real NMS would see if alarm suppression were *off*. For example, `fiber_cut(span_OMS3_BC)` cuts the shared span at the C-end (RX) and brings down **both** `ch2` and `ch4` (both ride OMS3 between B and C). The generated cascade includes (illustrative — names use the family-form `OTU_*` from the project notes; the description lookup resolves them to the rate-specific 9800 form like `OTU0_SSF` at display time):

```
At C (RX side of the cut span_OMS3_BC):
  OTS_LOS    at C.FIU.line_from_B          (optical-layer LOS from B toward C)
  MUT_LOS    at C.FIU.line_from_B          (fault-summary at the trunk RX port)

At C (downstream of the cut, RX of both ch2 and ch4):
  R_LOS      at C.OTU_ch2.line_rx          (OCh-layer LOS for ch2)
  OTU_SSF    at C.OTU_ch2.line_rx          (server-signal-fail for ch2's OTU)
  R_LOS      at C.OTU_ch4.line_rx          (OCh-layer LOS for ch4)
  OTU_SSF    at C.OTU_ch4.line_rx          (server-signal-fail for ch4's OTU)

At B (upstream of the cut, peer reports back-defect):
  OTU_BDI    at B.OTU_ch2.line_tx
  OTU_BDI    at B.OTU_ch4.line_tx

ODU-layer cascade at the destination client TPs of each service:
  ODU_PM_SSF at C.OTU_ch2.client           (ch2's destination client TP)
  ODU_PM_SSF at B.OTU_ch4.client           (ch4's destination client TP at B)
```

This raw cascade — alarms from **two different services** sharing a common root — is exactly the alarm-correlation-group input the RCA pipeline in §10 expects.

**Stage 2 — Suppression.** Apply the 50 edges from `data/alarm_suppression.json` (Section 1.7 of the reference). For every emitted alarm `B`, if any *upstream* alarm `A` (where `A → B` is a suppression edge) is also present, drop `B`. The operator-friendly view keeps only `OTS_LOS`, `MUT_LOS` and the `OTU_SSF` family (the `ODU_PM_SSF`, `OTU_BDI`, `R_LOS` cascades fold up into them).

**Both views are recorded in the database** (controlled by the `suppressed` column). The future ML model needs both: real operators sometimes have suppression on and sometimes off, and the propagation-graph mining in §10 wants the unsuppressed cascades as ground truth.

## 8. Frontend GUI Layout

A single page, divided into three regions:

```
┌──────────────────────────────────────────────────────────────────────┐
│  Network Fault Simulator                          [Clear] [Export ⇩] │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│   ┌──────────────────────────────────────────┐                       │
│   │             [ne1]                        │   ── Trigger fault ──│
│   │            /     \                       │                       │
│   │       [ne2]──────[ne4]                   │   Fault type:         │
│   │            \     /                       │   ┌────────────────┐  │
│   │             [ne3]                        │   │ Fiber cut      ▼│  │
│   │   topology canvas (vis-network)          │   └────────────────┘  │
│   │                                          │   Target:             │
│   │   - nodes turn red when alarmed          │   ┌────────────────┐  │
│   │   - links turn red when cut/degraded     │   │ span_23        ▼│  │
│   │   - click a node/link to fill "Target"   │   └────────────────┘  │
│   │                                          │                       │
│   │                                          │   [   Trigger   ]     │
│   │                                          │                       │
│   │                                          │   ☑ Apply suppression │
│   └──────────────────────────────────────────┘                       │
│                                                                      │
├──────────────────────────────────────────────────────────────────────┤
│   Alarms (current incident: #42  Fiber cut on span_23)               │
│   ┌─────┬──────────────┬─────────────────┬──────────┬─────────────┐  │
│   │  #  │ Alarm        │ Location        │ Severity │ Time offset │  │
│   ├─────┼──────────────┼─────────────────┼──────────┼─────────────┤  │
│   │  1  │ MUT_LOS      │ ne2.FIU.line_e  │ Critical │ +0 ms       │  │
│   │  2  │ OTS_LOS      │ ne2.FIU.line_e  │ Critical │ +0 ms       │  │
│   │  3  │ R_LOS        │ ne3.OTU.line_rx │ Critical │ +5 ms       │  │
│   │  4  │ OTU_SSF      │ ne3.OTU.line_rx │ Warning  │ +12 ms      │  │
│   │  5  │ OTU_BDI      │ ne2.OTU.line_tx │ Minor    │ +15 ms      │  │
│   │  6  │ ODU_PM_SSF   │ ne3.OTU.client  │ Minor    │ +20 ms      │  │
│   │  ...                                                          │  │
│   └─────┴──────────────┴─────────────────┴──────────┴─────────────┘  │
│                                                                      │
│   Incident history:  ▸ #41 Board power off ne1.OTU                   │
│                      ▸ #40 Fan failure ne2                           │
└──────────────────────────────────────────────────────────────────────┘
```

Behaviors:
- Clicking a node or link in the canvas **auto-fills the Target dropdown** so the user doesn't have to read fiddly IDs.
- Affected nodes turn red; alarmed links turn dashed-red.
- The alarm panel is sortable and filterable by severity.
- "Export" downloads the current `incidents.db` content as a single `.zip` containing `incidents.csv` + `alarms.csv` + `alarm_catalog.json` + `alarm_suppression.json`. This bundle is what the ML pipeline will consume in the next phase.

## 9. Database Schema (training-set output)

Two tables in SQLite, one row per incident, many rows per alarm.

### Table `incidents`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PRIMARY KEY | incident number |
| `created_at` | TEXT (ISO 8601) | when triggered |
| `fault_type` | TEXT | one of the 8 fault IDs in §6 — **this is the ML label** |
| `fault_target` | TEXT | which link / NE / board / port was targeted |
| `target_kind` | TEXT | `link` / `ne` / `board` / `port` |
| `suppressed` | INTEGER (0 / 1) | whether suppression was applied to the recorded view |
| `topology_id` | TEXT | hash of the topology JSON used (for reproducibility) |
| `note` | TEXT | optional — free text, e.g. "click from GUI" |

### Table `alarms`

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PRIMARY KEY | row id |
| `incident_id` | INTEGER FK → incidents.id | parent incident |
| `alarm_name` | TEXT | e.g. `R_LOS`, `OTU_SSF`, `ODU_PM_SSF` |
| `ne` | TEXT | NE id where the alarm fires |
| `board` | TEXT | board id (may be empty) |
| `port` | TEXT | port id (may be empty) |
| `severity` | TEXT | `Critical` / `Major` / `Minor` / `Warning` |
| `layer` | TEXT | `OTS` / `OMS` / `OCH` / `OTU` / `ODU` / `client` — needed for correlation rules R2/R4 |
| `container_id` | TEXT | tunnel or service id this alarm belongs to (e.g. `odu-A`, `och-B`, `svc_A`) — needed for R2/R3/R4 |
| `t_offset_ms` | INTEGER | milliseconds after the fault — needed for correlation rule R1 |
| `is_root` | INTEGER (0 / 1) | true for the alarm closest to the root cause (heuristic flag) |

### Example record (one fiber-cut incident, v3 lab mesh)

```
incidents:
  id=42  created_at=2026-05-05T14:30:01Z
  fault_type=fiber_cut  fault_target=span_OMS3_BC
  target_kind=link  suppressed=0  topology_id=6a59b54cbe35

alarms (raw cascade, suppressed=0):
  id=1 alarm=OTS_LOS    ne=C board=FIU port=line_from_B  sev=Critical layer=OTS container=span_OMS3_BC t=0  is_root=1
  id=2 alarm=MUT_LOS    ne=C board=FIU port=line_from_B  sev=Critical layer=OTS container=span_OMS3_BC t=2
  id=3 alarm=R_LOS      ne=C board=OTU_ch2 port=line_rx  sev=Critical layer=OCH container=otu_OMS3_BC  t=5
  id=4 alarm=OTU_SSF    ne=C board=OTU_ch2 port=line_rx  sev=Warning  layer=OTU container=otu_OMS3_BC  t=12
  id=5 alarm=OTU_BDI    ne=B board=OTU_ch2 port=line_tx  sev=Minor    layer=OTU container=otu_OMS3_CB  t=18
  id=6 alarm=ODU_PM_SSF ne=C board=OTU_ch2 port=client   sev=Minor    layer=ODU container=odu_ch2      t=25
  id=7 alarm=R_LOS      ne=C board=OTU_ch4 port=line_rx  sev=Critical layer=OCH container=otu_OMS3_BC  t=5
  id=8 alarm=OTU_SSF    ne=C board=OTU_ch4 port=line_rx  sev=Warning  layer=OTU container=otu_OMS3_BC  t=12
  id=9 alarm=ODU_PM_SSF ne=B board=OTU_ch4 port=client   sev=Minor    layer=ODU container=odu_ch4      t=25
```

The `layer` and `container_id` columns make the four correlation rules (§10) trivially expressible as SQL queries.

### How the RCA pipeline will consume this

- Each incident becomes one training sample with **two labels**: the simulator-known `fault_type` (the GUI-injected root) **and** the alarm name closest to the root in the propagation graph.
- The (alarm_a, alarm_b) co-occurrence pairs across many incidents become **propagation-edge candidates** — exactly the ~2000 algorithm-mined edges the project notes describe.
- The 50 expert-written suppression edges + the layered topology + the mined edges form the **alarm-propagation knowledge graph**.
- A baseline classifier (gradient boosting on the alarm vector + topology features) gives us the headline number; a GNN over the propagation-graph subgraph is the upgrade path.

## 10. RCA Pipeline & Correlation Rules — what the simulator's data feeds

The simulator's `incidents.db` is the **labelled training set** for the project's RCA pipeline 「**基于告警传播关系和图搜索的根因推理**」. The pipeline is published in `Library/Picture1.png` and `Library/Root cause analysis of alarms.txt`. It has three stages:

```
   ┌────────────────────┐    ┌──────────────────────┐    ┌──────────────────────┐
   │  alarm correlation │    │ propagation-relation │    │ supplementary-rule   │
   │  group             │ →  │ reasoning            │ →  │ reasoning            │ →  root-cause alarm
   │                    │    │  (over fault         │    │  (service up/down,   │
   │                    │    │   propagation graph) │    │   client-svc layer,  │
   │                    │    │                      │    │   concurrent faults) │
   └────────────────────┘    └──────────────────────┘    └──────────────────────┘
        告警关联组              传播关系推理                  补充规则推理            根因告警
```

Walk-through (the example in `Library/Picture1.png`):

1. **Input — alarm correlation group**: `{ MUT_LOS, R_LOS, OTU_SSF, OTU_BDI, ODU_PM_SSF }` arrive within a short time window on related layers/containers.
2. **Propagation-relation reasoning**: each edge of the **fault propagation graph** (3000+ expert-written + ~2000 GNN-mined, ~5000 total) is checked. Edges that fire on this group:
   ```
       MUT_LOS  →  R_LOS                  (expert)
       R_LOS    →  OTU_SSF                (expert)
       OTU_SSF  →  OTU_BDI                (expert)
       OTU_SSF  →  ODU_PM_SSF             (expert, layer-cascade)
       (and internal MUT_LOS → MUT_LOS self-loop for span-level summary)
   ```
   The graph collapses the alarm set toward the upstream-most node — `MUT_LOS`.
3. **Supplementary-rule reasoning** disambiguates when multiple candidates remain or when concurrent independent faults exist, using:
   - **service upstream/downstream** (是 `MUT_LOS` 真的上游？),
   - **client-service layer** (does the candidate sit at the lowest layer in any service's stack?),
   - **concurrent-fault reasoning** (split into multiple roots if candidates' propagation cones don't overlap).
4. **Output — root-cause alarm**: `MUT_LOS` (highlighted red in the picture).

### 10.1 The four alarm-correlation rules (with weights)

These rules — verbatim from `Library/Alarm correlation logical rules.txt` — decide which alarms belong to the same correlation group at stage 1. Each rule has a confidence weight; an alarm pair's total correlation score is the sum of weights of rules that fire on it.

| # | Rule | Weight | What we need from the data model |
|---|---|---|---|
| **R1** | If a set of alarms are generated within 6 minutes, and any two consecutive alarms are ≤ 5 minutes apart, they are correlated. | **0.1** | `t_offset_ms` (or absolute timestamp) on every alarm. |
| **R2** | An alarm on an OTU container and an alarm on an OCh tunnel are correlated if the OCh signal is carried in that OTU. | **0.2** | `container_id` on each alarm; the YANG layered topology (OTU link → `supporting-link` → OCh tunnel) walks the relationship. |
| **R3** | Two ODU alarms are correlated if their ODUs are underlay segments of the same client service. | **0.4** | `nfms:client-service-name` tag on each ODU tunnel; equality of tag value ⇒ rule fires. |
| **R4** | Two OCh-tunnel alarms are correlated if their paths share a common OTS link. | **0.3** | each WDM tunnel's explicit-route hop list in the optical topology; non-empty intersection ⇒ rule fires. |

In the v3 default incident "Fiber cut on `span_OMS3_BC`", **R4 fires** between `ch2`'s and `ch4`'s OCh-layer alarms because both `och_ch2` and `och_ch4` traverse `span_OMS3_BC` (per `data/yang/tunnels.json`). R3 does **not** fire between `odu_ch2` and `odu_ch4` because their `client-service-name` tags differ (`ch2` vs `ch4`). This is by construction — exactly the cross-service correlation pedagogical case the project's reference picture illustrates.

### 10.2 What the simulator must contribute

For the pipeline above to be trainable from the simulator's database, every alarm row needs:

- a **timestamp / t_offset_ms** (R1),
- a **layer** (`OTS` / `OMS` / `OCH` / `OTU` / `ODU` / `client`) (R2, R4),
- a **container_id** (tunnel / service identifier the alarm belongs to) (R2, R3, R4),
- a **link to the GUI-injected fault** (`incidents.fault_type`, `fault_target`) — the ground-truth root cause used to label the propagation-graph edges mined later.

§9's `alarms` table now carries all four. Nothing else is needed at simulator level.

## 11. Out of Scope (later phases)

- Training and serving the ML root-cause model itself; it consumes our DB but is built outside this repo.
- A real-time / streaming alarm interface.
- Multi-fault incidents, protection-switching simulation.
- Persistence of GUI session state across restarts.
- A reasoning runtime that *executes* §10's pipeline live; for now we only generate the labelled data it needs.

---

## 12. Decisions log (resolved during v1→v3)

The original draft listed five open questions; v3 resolved all of them:

1. **Scope of the fault catalog** → resolved in `Design/05_fault_event_catalog.md` §7. v3 ships the 10 ★ priority faults; the remaining 8 candidates are the v4 backlog.
2. **Suppression view** → record both. Every GUI click writes one `suppressed=0` and one `suppressed=1` incident; see `src/app/server.py::api_trigger`.
3. **Alarm name normalization** → store the family-form name in `alarms.alarm_name` (e.g. `OTU_SSF`); the rate-specific form (`OTU0_SSF` / `OTU1_SSF`) is recovered at display time via `_description_for()` walking the `_RATE_VARIANTS` list against the carrying tunnel's `rate-type`.
4. **GUI styling** → utility-grade CSS, no framework. Single-page layout per §8; vis-network for the topology canvas.
5. **Backend port / hosting** → local-only `127.0.0.1:5000` (default). The Flask server runs with `debug=True` so code edits hot-reload; YANG JSON edits require a server restart.

Build order (now historical, per the v3 implementation):
1. ✅ Hand-wrote `data/yang/v2/{topology,tunnels,inventory}.json` for the diamond+crossbar baseline.
2. ✅ Implemented `yang_loader.py` → `Topology` objects.
3. ✅ Implemented `faults.py` (10 ★ rules) + `engine.py` (L1 suppression-graph forward-walk + L2 cause-keyword expansion).
4. ✅ Implemented `db.py` SQLite recorder with the §9 schema.
5. ✅ Implemented `server.py` + frontend GUI per §8.
6. ✅ v3 pivot: replaced `data/yang/{topology,tunnels,inventory}.json` with the lab-mesh capture (preserved v2 at `data/yang/v2/`); switched alarm-description lookup to OSN 9800 with 8800 fallback.
7. ⏳ KG builder + ML training — see `Design/04_knowledge_graph_plan.md`.
