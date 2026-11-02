# PPE Simulation — Fiber-Longitudinal Power Profile in the Alarm Simulator

Status: **Draft for review.**
Scope: Phase-7 deliverable. The simulator (`src/simulator/`) today emits **alarm cascades** only. This document specifies how to extend it so that every triggered fault also produces a synthetic **Power Profile Estimation (PPE)** trace — the per-distance optical-power curve a coherent receiver would reconstruct from its DSP — and how that trace flows into `incidents.db`, the KG (`data/kg/`), the streaming engine (`src/kg_engine/`), the rule reasoner (`src/rules/`), and eventually the GNN (`src/ml/`).
Related: `Design/02_simulator_design.md` (alarm pipeline), `Design/05_fault_event_catalog.md` (the 10 ★ faults this extends), `Design/08_root_cause_rules.md` (rules consumed at inference), `Design/10_realtime_kg_engine.md` (the event stream PPE updates ride on), `Design/11_gnn_training_plan.md` (the GNN that will consume PPE features).

---

## 1. What PPE is

**Power Profile Estimation** (PPE), also called **Fiber-Longitudinal Power Profile Estimation (LPPE / FLPPE)**, is a software-based monitoring technique that reconstructs the *optical signal power as a function of distance* `P(z)` along a fiber span, using **only receiver-side measurements** — no inline OTDR, no per-amplifier power monitor required.

### 1.1 How it works (one paragraph)

A coherent transponder already digitizes the received complex optical field and runs **Digital Back-Propagation (DBP)** to compensate for chromatic dispersion and Kerr nonlinearity. DBP is a discretised numerical inversion of the nonlinear Schrödinger equation: it steps the recovered field backward through the fiber in `Δz` segments. At every step `z_k`, an *intermediate* field estimate is available. The mean optical power of that intermediate field is `P̂(z_k)` — the receiver's best guess at what the power *was* at distance `z_k` from the transmitter. Concatenating those estimates over the whole span gives the longitudinal power profile.

### 1.2 Why it is "cutting-edge"

| Property | Conventional OTDR / OPM | PPE |
|---|---|---|
| Hardware | Dedicated injection laser + photodiode per fiber, or per-amplifier OPM tap | None — reuses the existing coherent receiver |
| Per-channel | No (probes the fiber as a whole) | Yes (one profile per WDM channel, since each receiver sees its own channel) |
| Update cadence | Minutes to hours (and disruptive) | Seconds (non-intrusive, runs continuously alongside data demodulation) |
| Spatial resolution | < 1 m for OTDR | 0.5–2 km, set by symbol rate and DBP step size |
| Range | Limited by injected probe power | Limited by accumulated DBP noise at far end |
| Cost | Expensive add-on equipment | Software upgrade on already-deployed coherent transponders |

### 1.3 What anomalies look like in `P(z)`

| Fault | Signature in `P(z) − P_baseline(z)` |
|---|---|
| Fiber cut at distance `d` | Near-vertical step **down by ≥25 dB** at `z ≈ d`; values beyond `d` are noise floor / NaN |
| High attenuation over segment `[a, b]` | Extra **negative slope** of 0.05–0.3 dB/km over `[a, b]`; profile elsewhere unchanged |
| Dirty connector at amp site | Localized **step down by 0.5–3 dB** at the amplifier z-coordinate |
| EDFA pump failure | Negative step at the amplifier site (gain → 0) plus elevated noise downstream |
| Raman gain drift | Slow change in the slope of `P(z)` in the Raman pump's reach |
| Rx-side optical drift | **No anomaly in the span profile** — the change is at the receiver, not along the fiber. PPE rules out a span fault. |
| Pure electrical fault (B1/B2/FEC) | No PPE anomaly. PPE rules out an optical-layer cause. |

### 1.4 Key references

- T. Tanimura et al., **"Fiber-Longitudinal Anomaly Position Identification Over Multi-Span Transmission Link Out of Receiver-end Signals,"** JLT 2020.
- T. Sasai et al., **"Digital Backpropagation for Optical Path Monitoring: Loss Profile and Passband Narrowing Estimation,"** ECOC 2020 / JLT 2021.
- F. Hahn et al., **"Localization of Reflection-Induced Anomalies via Longitudinal Power Profile Estimation,"** JLT 2023.
- NEC, Huawei, NTT have all demonstrated production prototypes at OFC 2022–2024.

These papers also bound the resolution / accuracy numbers used in §3.

---

## 2. Why integrate PPE into our simulator

The alarm cascade alone has three blind spots that PPE measurably reduces:

1. **Sub-threshold soft faults.** `fiber_attenuation_high(span)` today only fires `IN_PWR_LOW + BEFFEC_EXC + OTU_DEG`. If the attenuation grows slowly, these don't trip until *after* the operator could have intervened. PPE detects the slope change before any alarm threshold is crossed.
2. **Spatial localisation.** Alarms tell the operator *which span* is broken; PPE tells them *where along that span* (e.g. "9.4 km in"). For an 80 km long-haul this changes a 4-hour OTDR truck-roll into a 20-minute targeted repair.
3. **Disambiguation.** `rx_power_drift(port)` and `fiber_attenuation_high(span)` both raise `IN_PWR_LOW` at the same RX port. The alarm cascade alone cannot tell them apart with 100 % accuracy. PPE *does*: a Rx drift shows a flat span profile, a span fault shows a sloped or stepped profile. This is exactly the disambiguation the rules-baseline gets wrong on the 10 % long-tail `config_mismatch` and 4 % `fiber_cut` cases (`memory/rules_baseline.md`).

Beyond that — the planned T2 GNN (`Design/11`) needs **multi-modal input** to credibly beat the 99.1 % rules baseline. Alarms alone leave little room to learn; alarms + PPE is the natural extension that mirrors what a real Huawei OSN 9800 with PPE-enabled OTU boards exposes to its NMS.

---

## 3. The PPE generator — physics-based parametric model

The simulator stays **physics-based and parametric**, matching the style of `src/simulator/faults.py`. No measurement corpus is loaded; profiles are synthesised from a small set of per-span / per-amplifier parameters declared in YANG, plus a fault-specific perturbation.

### 3.1 Discretisation

For each `(channel, span)` pair we emit a profile sampled at fixed spatial resolution `Δz`:

```
Δz       = 200 m            # default; configurable per-span
N(span)  = ceil(L_km / Δz)  # number of bins on this span
z[k]     = k · Δz           # k = 0..N-1
```

For an 80 km span, `N = 400`. A profile is stored as a `float32[N]` blob (1.6 KB per channel per span per snapshot).

### 3.2 Baseline profile `P_0(z)`

Composed at simulator startup from YANG-declared fiber + amplifier parameters:

```
P_0(z) = P_launch_dBm
         − α_dB_per_km · z                      # fiber attenuation
         − Σ_i  L_connector_i · 1[z ≥ z_conn_i] # discrete connector / splice losses
         + Σ_j  G_edfa_j     · 1[z ≥ z_amp_j]   # EDFA gains at amplifier sites
```

Typical defaults for the v3 lab mesh:

| Parameter | Default | Source |
|---|---|---|
| `P_launch_dBm` | 0 dBm | per-channel launch power; declared on the line-side OTU port |
| `α_dB_per_km` | 0.20 dB/km (1550 nm SMF) | constant; overridable per-fibre-type |
| `L_connector` | 0.3 dB | at every join (each `Connected` edge in topology) |
| `G_edfa` | matches per-span loss | one amp per OMS by default; multi-amp spans v4 |

These parameters are added to `data/yang/inventory.json` (per-board for transponders / amplifiers) and `data/yang/topology.json` (per-span for fiber type and length). See §6.1.

### 3.3 Fault perturbation `δP(z)`

For a fault triggered on `(span, position_km)`, the observed profile is:

```
P̂(z) = P_0(z) + δP(z; fault, position, params) + ε(z)
```

Where `ε(z)` is per-sample Gaussian noise whose standard deviation grows linearly with `z` to mimic the cumulative DBP-noise floor:

```
σ(z) = σ_0 + σ_per_km · z
σ_0 = 0.05 dB,  σ_per_km = 0.005 dB/km   # tunable
```

The per-fault `δP` shapes are defined in §4.

### 3.4 Multi-span propagation

For a fault on `span_X` and a channel that traverses spans `[span_A, span_B, span_X, span_Y]` in that order, the PPE generator emits one profile per receiver — the OCh's drop receiver only sees the cumulative effect on its own line. Specifically:

- Spans `A`, `B` upstream of the fault are reported by *their* receivers with their own clean baselines (the fault has no upstream effect — light propagates forward).
- Span `X` (the faulted one): its receiver sees `P_0 + δP`.
- Span `Y` downstream of the fault: its receiver sees `P_0` shifted down by `δP(L_X)` (the cumulative loss at the end of the faulted span), and beyond a hard fiber-cut its receiver sees only noise → it raises its own `OTS_LOS` alarm, in which case **no PPE is meaningfully produced** for that span (we emit a sentinel `degraded=true` row).

The simulator does *not* model nonlinear interaction between PPE and DSP noise — the noise term `ε(z)` is treated as additive Gaussian regardless of fault state. This is acceptable for ML training; real-world cumulative non-Gaussian behaviour can be added in a later phase.

### 3.5 Coherent-receiver visibility

Only **OTU receivers** produce PPE. The simulator's PPE generator therefore enumerates `(och_tunnel, span)` pairs along each tunnel's ERO, and emits one profile per pair, attributed to the **drop NE / OTU board** that terminates that OCh. Spans not carried by any OCh tunnel (e.g. a freshly-provisioned dark span) get no PPE row.

---

## 4. Per-fault PPE signatures

One physical fault produces both an alarm cascade *and* a PPE delta. The PPE delta is generated by a paired emitter alongside the alarm rule. The two are kept aligned at the data-model level: same `incident_id`, same `group_id`, same `is_root` ground truth attribution.

| Fault id | PPE `δP(z)` shape | Localisation parameter | Notes |
|---|---|---|---|
| `fiber_cut(span, pos_km)` | step down `−30 dB` at `z = pos_km`, noise floor beyond | `pos_km` (currently random uniform on `[1, L-1]`; can be pinned via `--position`) | Beyond `pos_km` we emit `NaN`. |
| `fiber_attenuation_high(span, seg=(a,b), extra_db_per_km)` | extra `−extra_db_per_km · (z − a)` for `z ∈ [a, b]`, then flat extra `−extra_db_per_km · (b − a)` beyond | `(a, b, extra)` default `(0.3·L, 0.7·L, 0.10)` dB/km | Soft fault — alarm tripping needs `extra · (b − a) ≥ 3 dB`. |
| `b1_exc_high(span)` | **no PPE change** | — | Electrical-layer fault; useful negative example. |
| `b2_exc_high(span)` | **no PPE change** | — | Same. |
| `fec_pre_cor_high(span)` | **no PPE change** | — | Same. |
| `rx_power_drift(port)` | **no PPE change on span profile**; instead RX-side power offset `±2 dB` added to the final sample only | — | The diagnostic distinguisher from a true span fault. |
| `ne_power_off(ne)` | every OCh that drops at this NE → degraded sentinel (no usable profile); every OCh sourced at this NE → far-end receivers see clean baseline up to the dead NE, then noise floor | — | Effectively a fiber cut at the dead NE's egress, multiplied across all its egress spans. |
| `board_power_off(ne, board)` | OCh tunnels riding through this board → degraded sentinel on the affected receivers | — | Like `ne_power_off` but scoped to the board's egress ports. |
| `fan_failure(ne)` | **no PPE change** initially; after `temp_alarm_delay_ms` (default 120 s) → small per-channel power drift `±0.5 dB` per receiver on the affected NE (thermal-induced laser drift) | — | Slow PPE change, distinct from a span fault. |
| `config_mismatch(port)` | **no PPE change** | — | Configuration-only fault; PPE clean. |

The PPE-vs-alarm signature matrix above is the **single most useful artefact** for the downstream rules and the GNN: it forms a discriminative basis that the alarm cascade alone does not provide.

---

## 5. Data model — schema additions

### 5.1 SQLite (`data/incidents.db`)

Two new tables. The `alarms` table is unchanged.

```sql
CREATE TABLE IF NOT EXISTS ppe_traces (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id     INTEGER NOT NULL,
    channel         TEXT NOT NULL,         -- e.g. "ch2"
    span_id         TEXT NOT NULL,         -- e.g. "span_OMS3_BC"
    drop_ne         TEXT NOT NULL,         -- the NE whose OTU receiver produced this
    drop_board      TEXT,
    t_observed_ms   INTEGER NOT NULL,      -- time the profile was reported (NMS-observed)
    delta_z_m       INTEGER NOT NULL,      -- spatial resolution
    n_samples       INTEGER NOT NULL,
    samples_blob    BLOB NOT NULL,         -- float32[n_samples]  observed P̂(z) in dBm
    baseline_blob   BLOB NOT NULL,         -- float32[n_samples]  P_0(z) in dBm
    degraded        INTEGER NOT NULL DEFAULT 0,  -- 1 if no usable profile (LOS at receiver)
    FOREIGN KEY (incident_id) REFERENCES incidents(id)
);
CREATE INDEX IF NOT EXISTS idx_ppe_incident ON ppe_traces(incident_id);

CREATE TABLE IF NOT EXISTS ppe_anomalies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id        INTEGER NOT NULL,
    kind            TEXT NOT NULL,         -- 'step' | 'slope' | 'null' | 'amp_step' | 'rx_offset'
    z_km            REAL,                  -- localisation; NULL for non-spatial kinds
    z_end_km        REAL,                  -- for slope kind, end of the affected segment
    magnitude_db    REAL,                  -- depth of step, or per-km slope
    confidence      REAL NOT NULL,         -- detector confidence in [0,1]
    is_root         INTEGER NOT NULL DEFAULT 0,  -- ground-truth: is this the anomaly that caused the fault?
    FOREIGN KEY (trace_id) REFERENCES ppe_traces(id)
);
CREATE INDEX IF NOT EXISTS idx_ppe_anom_trace ON ppe_anomalies(trace_id);
```

`samples_blob` and `baseline_blob` are `numpy.ndarray.tobytes()` on a `float32[N]`. At `Δz = 200 m` and `L = 80 km` that's 1.6 KB / blob; a typical incident emits ~6 profiles × 2 blobs = ~20 KB. The default 5120-incident dataset adds **~100 MB** to `incidents.db` — manageable. Compressed (gzip blob option) it drops to ~30 MB.

#### 5.1.1 Column reference — `ppe_traces`

The schema block above is the canonical DDL; in the as-shipped code (`src/simulator/db.py:58-88`) the table also carries `group_id` and `t_offset_ms` columns, included here so the reference matches reality:

```sql
CREATE TABLE ppe_traces (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id     INTEGER NOT NULL,         -- FK → incidents.id
    channel         TEXT NOT NULL,            -- e.g. 'och_ch2'
    span_id         TEXT NOT NULL,            -- e.g. 'span_OMS3_BC'
    drop_ne         TEXT NOT NULL,            -- which NE's OTU reconstructed the trace
    drop_board      TEXT,                     -- e.g. 'C.OTU'
    group_id        TEXT NOT NULL DEFAULT '', -- aligned with alarms.group_id
    t_offset_ms     INTEGER NOT NULL,         -- physical-causal time
    t_observed_ms   INTEGER,                  -- arrival time with polling jitter
    delta_z_m       INTEGER NOT NULL,         -- spatial resolution, default 200 m
    n_samples       INTEGER NOT NULL,         -- = 1 + length_km × (1000 / delta_z_m)
    samples_blob    BLOB NOT NULL,            -- float32[n_samples] little-endian, dBm
    baseline_blob   BLOB NOT NULL,            -- float32[n_samples] little-endian, dBm
    degraded        INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (incident_id) REFERENCES incidents(id)
);
```

| Column | What it means |
|---|---|
| `channel` / `span_id` | The pair the trace describes. One incident produces multiple rows: every channel whose ERO crosses the faulted span emits one trace per span on its route. |
| `drop_ne` / `drop_board` | Provenance — which receiver reconstructed this trace. PPE is *receiver-side*, so the drop OTU "sees" the upstream span's profile. Only used for display and rule-engine attribution. |
| `group_id` | Aligned with `alarms.group_id` so a compound incident's R1/R2/… groups stitch alarms and traces together for downstream consumers. |
| `t_offset_ms` / `t_observed_ms` | Same dual-time convention as the `alarms` table. PPE typically arrives *before* the alarm thresholds trip (sub-second DSP cycle vs 5–30 s alarm polling), so traces often have `t_observed_ms < min(alarms.t_observed_ms)` for the same incident. **The ML pipeline should join on `t_observed_ms`.** |
| `delta_z_m` | Sampling step in metres along the fibre. Default 200 m → 5 samples/km. Fixed per trace; varies by transponder DSP capability in real hardware. |
| `n_samples` | Computed from `delta_z_m` and `nfms:length-km` so the receiver gets the *whole* span in one trace. |
| `samples_blob` | The actual P(z) curve — the disturbed power, what a real receiver would publish. Stored as little-endian float32 (`<f4`) so blobs are byte-identical across OS/CPU and `numpy.frombuffer(blob, dtype='<f4')` reconstructs the array verbatim. |
| `baseline_blob` | The corresponding *clean* curve from the parametric composer (P_launch − α·z − connector losses). Stored alongside so consumers don't have to recompute it; `samples − baseline` is the anomaly signal. |
| `degraded` | Sentinel for the post-cut region of a hard fibre cut: `1` means samples downstream of the cut are noise-floor / NaN-equivalent and should not be treated as valid power readings. |

#### 5.1.2 `kind` values — `ppe_anomalies`

```sql
CREATE TABLE ppe_anomalies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id        INTEGER NOT NULL,         -- FK → ppe_traces.id
    kind            TEXT NOT NULL,            -- step | slope | null | rx_offset
    z_km            REAL,                     -- localisation (None if non-spatial)
    z_end_km        REAL,                     -- only for kind='slope'
    magnitude_db    REAL,                     -- step depth, slope dB/km, or rx offset dB
    confidence      REAL NOT NULL,            -- in [0,1]; emitter-planted = 1.0
    is_root         INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (trace_id) REFERENCES ppe_traces(id)
);
```

| `kind` value | Trace signature | Originating fault |
|---|---|---|
| `step`      | sharp z-localised step ≥ 0.5 dB | dirty connector, EDFA pump failure, hard fibre cut (~25 dB) |
| `slope`     | extra negative slope over `[z_km, z_end_km]` | `fiber_attenuation_high` |
| `null`      | flat / noise floor, no usable curve | total signal loss (downstream of cut) |
| `rx_offset` | whole-curve vertical offset, no span feature | `rx_power_drift` — the change is at the receiver, not along the fibre. **Critical disambiguator** between `rx_power_drift` and `fiber_attenuation_high`, which the alarm cascade alone gets wrong on the long-tail 1 %. |

`is_root = 1` is set on the anomaly the emitter *planted* (ground truth); a detection pass on the trace can later add `is_root = 0` rows representing the detector's own view. Keeping both views in the same table lets downstream consumers train on either or both. See §5.5.1 for the cross-table relationship diagram and the canonical join queries.

### 5.2 Migration

`src/simulator/db.py::_migrate` already idempotently adds columns. Extend it to:

1. `CREATE TABLE IF NOT EXISTS ppe_traces` and `ppe_anomalies` on every open.
2. No backfill — existing incidents stay PPE-less; the rule and KG layers tolerate absence.

### 5.3 Why blob, not normalised rows

A normalised `ppe_samples(trace_id, z_km, p_dbm)` table would inflate to ~250 M rows for 5120 incidents — SQLite struggles past that. The blob is opaque to SQL but fully transparent to `numpy.frombuffer`, and downstream consumers (rules, KG builder, GNN) load whole profiles anyway.

### 5.4 YANG augmentations — `nfms:` extensions read by the PPE generator

The SQLite tables above describe PPE *output*. The *inputs* needed to compose a baseline curve live in the YANG instance files (`data/yang/`), augmented through the project module `network-fault-mgmt-state` (short prefix **`nfms`**). All PPE-relevant YANG extensions are concentrated in two files; `tunnels.json` is touched only to *reuse* an existing standard leaf.

#### 5.4.1 `inventory.json` — the OTU board capability flag

A single boolean leaf on every coherent transponder gates whether the simulator emits PPE traces for channels terminating on that board.

```jsonc
{ "uuid": "A.OTU", "name": "A.OTU", "class": "iana-hardware:module",
  "mfg-name": "Huawei", "part-number": "TN56NS3",
  "nfms:ppe-enabled": true,                                  // ← PPE flag
  "contained-child-component": [ … ports … ],
  "network-fault-mgmt-state:state": { "power-state": "on" } }
```

| Leaf | Type | Cardinality | Meaning |
|---|---|---|---|
| `nfms:ppe-enabled` | boolean | one per OTU module | Marks the OTU firmware as new enough to expose DSP-internal back-propagation results to the NMS. **The PPE generator only emits a trace when the affected channel terminates on a `ppe-enabled: true` OTU.** Set `false` (or omit) on legacy boards to simulate a mixed-vintage network. |

In v3 all six OTU boards (A.OTU…F.OTU) carry `true`, so every OCh channel can produce traces.

#### 5.4.2 `topology.json` — per-span optical parameters needed for the baseline curve

These augment the `te-link-attributes` of every `optical-topo` link (one entry per OMS span). They are the parameters the baseline composer needs to draw a *clean* reference curve before the fault deforms it: `P(z) = P_launch − α·z − connector losses + EDFA gains`.

```jsonc
{ "link-id": "span_OMS3_BC",
  "source": { "source-node": "B", "source-tp": "fiu-to-C" },
  "destination": { "dest-node": "C", "dest-tp": "fiu-from-B" },
  "ietf-te-topology:te": { "te-link-attributes": {
      "name": "OMS3 (B->C, 3 spans)", "admin-status": "up",
      "nfms:length-km": 60.0,                                // ← span physical length
      "nfms:fibre-type": "SMF-G.652",                        // ← fibre kind
      "nfms:alpha-db-per-km": 0.20,                          // ← attenuation coeff
      "nfms:connector-losses": [                             // ← discrete loss points
          { "z-km":  0.0, "loss-db": 0.3 },
          { "z-km": 60.0, "loss-db": 0.3 }
      ]
  } } }
```

| Leaf | Type | Required? | What the baseline composer does with it |
|---|---|---|---|
| `nfms:length-km` | decimal (km) | **required on every optical link** | Sets the `z`-axis end-point; with `delta_z_m = 200` (default) this fixes the sample count `n_samples ≈ 1 + length_km × 5`. For `span_OMS3_BC` (60 km) → 301 samples. |
| `nfms:fibre-type` | string | optional | Documentary; reserved for future fibre-aware physics (PMD, nonlinearity coefficient). Currently informational. |
| `nfms:alpha-db-per-km` | decimal (dB/km) | optional, defaults to `0.22` | The fibre's per-km attenuation. The baseline curve has slope `−α`. A `fiber_attenuation_high` fault perturbs this on the faulted span by adding an extra 0.05–0.3 dB/km. |
| `nfms:connector-losses` | list of `{z-km, loss-db}` | optional, defaults to `[]` | Discrete loss events along the span (splice points, patch panels at amp sites). The composer subtracts each `loss-db` at its `z-km`, producing a small step-down in the baseline. A future `connector_dirty` fault amplifies one of these. |

**Why these are required, not derived from elsewhere.** The IETF YANG topology model only describes the *logical* span (it's a link between two TPs); it has no notion of physical fibre length. PPE physics is parametric in `z`, so the YANG must carry `length-km` — without it the baseline composer can't decide the z-grid. The other three (`fibre-type`, `alpha-db-per-km`, `connector-losses`) are sensible defaults if omitted; `length-km` has no default.

Reading the comment block at `data/yang/topology.json` (line ~112):

> `nfms:length-km added per Design/03 §2.3 / Design/12 §6.1 — required for Power Profile Estimation. Values: OMS6/7/8 from Design/07 (40/60/80 km, calibrated); OMS1-5 concretized from the earlier 'abstract distance' (Design/07 §6 table 165) so the PPE baseline composer has a deterministic z-grid.`

The v3 OMS values are: OMS1 = 80 km, OMS2 = 100 km, OMS3 = 60 km, OMS4 = 40 km, OMS5 = 80 km, OMS6 = 40 km, OMS7 = 60 km, OMS8 = 80 km. Both directions of each OMS share the same `length-km` value (a link in `optical-topo` is unidirectional, so we get 16 link entries × one `length-km` each).

#### 5.4.3 `tunnels.json` — fields PPE reuses (no new schema)

PPE does **not** add new fields to `tunnels.json`. It piggybacks on standard `ietf-wdm-tunnel` leaves that were already there:

| Existing leaf | Type | What PPE uses it for |
|---|---|---|
| `wdm-constraint/tx-channel-power` | string (dBm) | The `P_launch` input to the baseline composer. v3 has `"0.0"` dBm on every OCh tunnel, so each channel's baseline starts at 0 dBm at `z=0`. |
| `wdm-constraint/otsi-carrier-frequency` | uint64 (×0.1 THz) | Identifies the wavelength — used in trace metadata (`channel` column) so the operator can disambiguate parallel channels on the same span. |

If you ever need per-wavelength launch-power tuning (a real-world incident), `tunnels.json` is where it would land — no schema change required, no new `nfms:` leaf.

### 5.5 End-to-end — how a fault becomes PPE rows

For a `fiber_attenuation_high(span_OMS3_BC)` incident the generator walks:

1. **Look up span** in `topology.json`: `length-km = 60`, `alpha-db-per-km = 0.20`, `connector-losses` list.
2. **Find channels** whose OCh ERO crosses `span_OMS3_BC`: `och_ch2`, `och_ch4` (the canonical R4 demo pair).
3. **For each channel**, find the drop NE and its OTU board; check `inventory.json` → `nfms:ppe-enabled = true`. If false, skip that channel.
4. **Compose baseline** per span on the ERO: `P_launch` (from the tunnel's `tx-channel-power`) − `α·z` − connector losses + EDFA gains, sampled every `delta_z_m = 200` m → 301 floats for the 60 km span.
5. **Apply the fault perturbation** on the faulted span only: extra negative slope of `Uniform(0.05, 0.3)` dB/km added to the baseline. Upstream spans get the clean baseline; downstream spans get the (now-depressed) launch power but their own clean profile shape.
6. **Add noise**: σ(z) = σ₀ + σ_per_km · z_cumulative.
7. **Plant the `is_root` anomaly**: `kind='slope', z_km=0.0, z_end_km=60.0, magnitude_db=<slope>, confidence=1.0`.
8. **Apply polling jitter** (Option A): `t_observed_ms = t_offset_ms + Uniform(0, 30000)` — same convention as alarms, but the PPE polling cycle is sub-second, so trace `t_observed_ms` typically lands earlier than the matching `IN_PWR_LOW` / `BEFFEC_EXC` alarm rows for the same incident.
9. **Persist**: one `ppe_traces` row + one `ppe_anomalies` row per `(channel, span)` pair, alongside the normal `alarms` rows under the same `incident_id`.

The resulting `incident_id` is interpretable two ways: the alarm cascade view (`alarms` table) tells you *which span* is faulted; the PPE view (`ppe_traces` + `ppe_anomalies`) tells you *where along the span* and *what kind of fault* — which is the disambiguation the rules baseline gets wrong on the long-tail 1 % (`config_mismatch`, `rx_power_drift` vs `fiber_attenuation_high`).

#### 5.5.1 Cross-table relationships

```
incidents (one row per fault)
   │
   ├──► alarms        (existing: ~20 rows / incident)
   │
   └──► ppe_traces    (new: 0..N rows; one per (channel,span) affected)
            │
            └──► ppe_anomalies  (new: 0..M rows / trace;
                                 usually 1 ground-truth root + 0..few detector picks)
```

Common downstream joins:

```sql
-- All PPE traces for one incident, with their planted root anomaly
SELECT t.channel, t.span_id, t.t_observed_ms,
       a.kind, a.z_km, a.magnitude_db
FROM   ppe_traces t
JOIN   ppe_anomalies a ON a.trace_id = t.id AND a.is_root = 1
WHERE  t.incident_id = ?;

-- Reconstruct the curves for plotting (use src.simulator.db.load_trace_samples)
SELECT id FROM ppe_traces WHERE incident_id = ? AND span_id = ?;
```

`src/simulator/db.py::load_trace_samples(conn, trace_id) → (samples, baseline)` is the canonical reader — it decodes the `<f4` little-endian blobs into a pair of numpy arrays and is what `src/simulator/ppe_plot.py` and the Web UI's PPE viewer call.

---

## 6. Integration into the batch simulator (`src/simulator/`)

### 6.1 YANG extensions — what's actually needed

Inspection of the current `data/yang/*.json` (v3 lab mesh) shows the situation is better than §3.2 might suggest: most of the inputs the baseline composer needs are already present under standard IETF names. The one **structural gap** is that span length is currently held in free-text comments only (`"OMS6 (D->E, 40 km)"`), not as a structured leaf. Everything else can be defaulted in code with optional YANG overrides.

#### 6.1.1 What's already there — reuse, don't reinvent

| Field | Lives at | Used as |
|---|---|---|
| `tx-channel-power` | `tunnels.json` → per OCh `ietf-wdm-tunnel:wdm-constraint` | `P_launch_dBm` for that channel |
| `otsi-carrier-frequency` | same place | wavelength, for any future per-λ `α` lookup |
| `supporting-link-ref` chain in `primary-path` | same place | ordered span list per OCh (multi-span propagation in §3.4) |

§3.2's "per-board launch power" was a design misread — launch power lives on the OCh tunnel, not the OTU board. Keep it on the tunnel; the PPE generator reads `tx-channel-power` directly from `tunnels.json`.

#### 6.1.2 What must be added — minimum viable set

Two fields, no defaults possible / sensible:

1. **`nfms:length-km`** on every span link in `topology.json` — required, structurally absent today.
2. **`nfms:ppe-enabled`** on every OTU module in `inventory.json` — feature gate; absence = PPE off for that receiver.

With just these two, the §3 generator runs with code defaults for α, connector loss, EDFA gain, Δz, σ, and poll interval.

#### 6.1.3 Optional refinements — sensible defaults exist

Add later, and only on the spans / boards that diverge from defaults.

**`topology.json` — per span link**, nested inside the existing `ietf-te-topology:te/te-link-attributes`:

```jsonc
{
  "link-id": "span_OMS3_BC",
  "source":      { … },
  "destination": { … },
  "ietf-te-topology:te": {
    "te-link-attributes": {
      "name": "OMS3 (B->C, 60km)",
      "admin-status": "up",
      "nfms:length-km":         60.0,        // ← REQUIRED
      "nfms:fibre-type":        "SMF-G.652", // optional — selects α default
      "nfms:alpha-db-per-km":   0.20,        // optional — overrides α
      "nfms:connector-losses": [             // optional — discrete losses along z
        { "z-km": 0.0,  "loss-db": 0.3 },
        { "z-km": 60.0, "loss-db": 0.3 }
      ]
    }
  }
}
```

**`inventory.json` — per OTU module**, alongside `mfg-name` / `part-number`:

```jsonc
{
  "uuid": "A.OTU",
  "name": "A.OTU",
  "class": "iana-hardware:module",
  "mfg-name": "Huawei",
  "part-number": "TN56NS3",
  "nfms:ppe-enabled":               true,    // ← REQUIRED gate
  "nfms:ppe-delta-z-m":             200,     // optional — spatial resolution
  "nfms:ppe-poll-interval-ms":      30000,   // optional — 30 s default
  "nfms:dbp-noise-sigma-db":        0.05,    // optional — σ₀
  "nfms:dbp-noise-sigma-per-km-db": 0.005    // optional — σ_per_km
}
```

**`inventory.json` — per OA (EDFA) module:**

```jsonc
{
  "uuid": "B.OA",
  "name": "B.OA",
  "class": "iana-hardware:module",
  "mfg-name": "Huawei",
  "part-number": "TN12OAU25S",
  "nfms:edfa-gain-db":         12.0,         // optional — defaults to span-loss compensation
  "nfms:edfa-noise-figure-db": 5.0,          // optional — reserved for σ(z) extension
  "nfms:edfa-z-km":            0.0           // optional — position on span; 0 = at NE egress
}
```

`nfms:edfa-z-km` only matters once a span has more than one amplifier; v3 has one per OMS at egress, so `0.0` is fine and the field can be omitted entirely.

**`tunnels.json` — no structural change required.** The existing `wdm-constraint` already carries everything needed on the source side. Only addition (optional) is a per-OCh override for experiments:

```jsonc
"ietf-wdm-tunnel:wdm-constraint": {
  …existing fields…
  "nfms:ppe-override": { "enabled": false }  // optional — disable PPE for this OCh only
}
```

#### 6.1.4 Naming convention

All new fields carry the **`nfms:` prefix** to match the existing `nfms:client-service-name` and `network-fault-mgmt-state:state` convention in `inventory.json` / `tunnels.json`. The formal augmentation is described in `Design/03_yang_topology_plan.md` §2.2 (alongside the v1 fields) and §2.3 (PPE-specific worked example).

`yang_loader.py` carries the new fields through to `Topology`. Defaults apply if a field is absent, so existing YANG files keep working until `length-km` and `ppe-enabled` are populated for spans / OTUs that should produce PPE.

### 6.2 New module: `src/simulator/ppe.py`

Self-contained generator. Public API:

```python
from dataclasses import dataclass

@dataclass
class PpeTrace:
    channel: str
    span_id: str
    drop_ne: str
    drop_board: str
    t_offset_ms: int
    t_observed_ms: int | None
    delta_z_m: int
    samples: np.ndarray         # float32[N]  observed P̂(z) in dBm
    baseline: np.ndarray        # float32[N]  P_0(z)
    degraded: bool = False
    anomalies: list["PpeAnomaly"] = field(default_factory=list)

@dataclass
class PpeAnomaly:
    kind: str                   # 'step' | 'slope' | 'null' | 'amp_step' | 'rx_offset'
    z_km: float | None
    z_end_km: float | None
    magnitude_db: float
    confidence: float
    is_root: bool = False

def baseline(t: Topology, channel: str, span_id: str) -> np.ndarray: ...

def generate(t: Topology, fault_type: str, *args, **kwargs) -> list[PpeTrace]: ...

def detect_anomalies(trace: PpeTrace, *, threshold_db: float = 1.0) -> list[PpeAnomaly]: ...
```

`generate` dispatches on `fault_type` to a per-fault PPE emitter — the **paired** emitter alongside the alarm rule. Each pairing is a small function that returns a `δP(z)` shape and an `(anomaly_kind, z_km, magnitude_db)` tuple flagged with `is_root=True` for the ground-truth-causing anomaly. The 10 emitters mirror §4 exactly.

`detect_anomalies` is the deterministic anomaly detector (peak-finder + slope-segmentation) that downstream rule R5–R7 consume. It runs *after* noise is added, so it returns the noisy detector's view — not the cheat of reading the planted `δP` directly.

### 6.3 Engine wiring

`src/simulator/engine.py::expand_cascade` already returns alarms. Add a sibling:

```python
def expand_ppe(t: Topology, fault_type: str, *args, **kwargs) -> list[PpeTrace]:
    traces = ppe.generate(t, fault_type, *args, **kwargs)
    for tr in traces:
        tr.anomalies = ppe.detect_anomalies(tr)
    return traces
```

`apply_polling_delay` gains a new layer cadence `PPE = 30_000 ms` (default 30 s per snapshot) so that `t_observed_ms` is set consistently with the existing alarm-polling model. A fault therefore typically produces:

- alarms at `t_observed_ms ∈ [0 s, ~5 min]` per their existing layer cadences,
- 1 PPE snapshot per affected receiver at `t_observed_ms ∈ [0, 30 s]`.

### 6.4 Recorder

`src/simulator/db.py::record` gains:

```python
def record(conn, *, fault_type, fault_target, target_kind,
           suppressed, topology_id, alarms, ppe_traces=(), note=""):
    # ... existing incident + alarm inserts ...
    for tr in ppe_traces:
        trace_id = _insert_ppe_trace(conn, sid, tr)
        for a in tr.anomalies:
            _insert_ppe_anomaly(conn, trace_id, a)
    return sid
```

Backwards-compatible: callers that don't pass `ppe_traces` keep working.

### 6.5 Batch generator

`src/simulator/generate_dataset.py` gains a `--with-ppe` flag (default `True` once the schema lands). With it on, the per-incident inner loop is:

```python
seeds   = trigger(t, fault_type, *args)
alarms  = expand_cascade(t, seeds, fault_type)
ppes    = expand_ppe(t, fault_type, *args)
apply_polling_delay(alarms)
apply_polling_delay_ppe(ppes)
sid = record(conn, ..., alarms=alarms, ppe_traces=ppes)
```

On the v3 lab mesh, this adds < 1 s per 1024 incidents (PPE generation is O(N_channels · N_samples) per incident; ~12 channel-spans × 400 samples = 5k float ops per incident).

### 6.6 Compound incidents

For multi-fault incidents (`R1+R2` groups), each fault generates its own PPE traces. Where two faults affect the same `(channel, span)` pair (rare: two faults hitting the same span), the second fault's `δP` is **added** to the first's — physically correct for additive-in-dB attenuation. The `is_root` flag on each anomaly is set per fault, preserving multi-root ground truth like the alarm side.

---

## 7. Integration into the streaming engine (`src/kg_engine/`)

The streaming engine is the production-shaped path; batch is for training data only. PPE updates flow as their own event kind.

### 7.1 New event kind

`src/kg_engine/events.py`:

```python
class EventKind(StrEnum):
    # existing: ALARM_RAISE, ALARM_CLEAR, INCIDENT_BOUNDARY, TOPOLOGY_*
    PPE_UPDATE = "ppe.update"
```

Payload:

```python
{
  "channel":    "ch2",
  "span_id":    "span_OMS3_BC",
  "drop_ne":    "C",
  "drop_board": "C.OTU",
  "delta_z_m":  200,
  "samples":    [...],      # float32 list, dBm
  "baseline":   [...],
  "degraded":   false,
  "anomalies":  [
    {"kind": "step", "z_km": 9.4, "magnitude_db": -28.7, "confidence": 0.98}
  ]
}
```

### 7.2 New handler

`src/kg_engine/handlers/ppe.py`:

```python
def handle_ppe_update(event: Event, state: StateStore) -> Delta:
    # 1. Resolve URIs (channel, span, anomaly).
    # 2. Generate add-triples for the trace + each anomaly.
    # 3. Garbage-collect superseded traces for the same (channel, span, drop_ne):
    #    emit remove-triples for the previous snapshot's URIs.
    # 4. Return Delta(add=..., remove=...).
```

Snapshot-and-replace semantics — we keep only the latest profile per `(channel, span, drop_ne)` in the live graph. Historical profiles live in the event log (`data/kg_engine/events.ndjson.*`) for replay and training.

### 7.3 Adapters

- **Simulator adapter** (`src/kg_engine/adapters/simulator.py`): extend the existing tail-of-`incidents.db` adapter to also tail `ppe_traces` and emit `ppe.update` events.
- **NETCONF adapter**: production-only. Subscribe to the Huawei NMS PPE notification stream (vendor-specific; documented in `Library/` once we get access).
- **File adapter**: read `data/ppe_replay/*.ndjson` for unit tests.

### 7.4 CSV mirror

`CsvMirrorSubscriber` (Design/10 §8) gains a fourth output: `data/kg/ppe_triples.csv` with the same flat-triple shape as `alarm_triples.csv`. Latest-snapshot-only — the file is rewritten, not appended, per snapshot (small enough that this is fine: ~6 traces × ~20 triples = ~120 rows per active incident).

---

## 8. KG schema extensions

`src/kg/schema.py` gains three new entity types and seven new predicates. URIs follow the existing `{type}:{id}` convention.

### 8.1 New entities

| Entity | URI pattern | Example | Source |
|---|---|---|---|
| `PpeTrace` | `ppe:{incident_id}:{channel}:{span}` | `ppe:42:ch2:span_OMS3_BC` | `ppe_traces.id` keyed by (scn, ch, span) |
| `PpeAnomaly` | `ppe_anom:{incident_id}:{trace_uri}:{idx}` | `ppe_anom:42:ppe:42:ch2:span_OMS3_BC:0` | `ppe_anomalies.id` |
| `FibrePosition` | `fpos:{span_id}:{z_km*10}` | `fpos:span_OMS3_BC:94` (= 9.4 km) | derived from anomaly `z_km` |

`FibrePosition` is a quantised spatial node: integer `z_km*10` (decimetre granularity at the URI level) bins anomalies onto a sparse grid so multiple snapshots over time at the same physical location share one URI. This is the entity the GNN learns position embeddings for.

### 8.2 New predicates

| Predicate | Subject → Object | Source tag | Weight |
|---|---|---|---|
| `MeasuredOn` | PpeTrace → OChTunnel | `ppe` | 1.0 |
| `MeasuresSpan` | PpeTrace → Span | `ppe` | 1.0 |
| `MeasuredBy` | PpeTrace → Board (the OTU drop board) | `ppe` | 1.0 |
| `HasAnomaly` | PpeTrace → PpeAnomaly | `ppe` | 1.0 |
| `LocatedAt` | PpeAnomaly → FibrePosition | `ppe` | 1.0 |
| `OnSpan` | FibrePosition → Span | `topology` | 1.0 |
| `PpeIndicates` | PpeAnomaly → Alarm | `derived` | 1.0 |
| `PpeRootCause` | PpeAnomaly → "true" | `ppe` | 1.0 |

`PpeIndicates` is the fused-signal edge — emitted by the KG builder (batch) or the rule subscriber (streaming) when a PPE anomaly is spatially / temporally co-located with an alarm, providing a cross-modal correlation hint to the GNN.

`PpeRootCause` is the ground-truth label on the planted anomaly (`is_root=1` in `ppe_anomalies`), parallel to the existing `RootCause` self-loop on alarms.

### 8.3 Batch builder updates

`src/kg/builder.py` gains:

- `build_ppe_triples(incident_id) -> list[Triple]` — emits the per-incident triples above.
- Hooks into the existing `build_kg()` pipeline so that `alarm_triples.csv` continues to hold alarms only, while a new `ppe_triples.csv` mirrors the PPE data (same schema columns).

`data/kg/topology_triples.csv` gains static `OnSpan` rows for every `FibrePosition` known at build time (typically empty — positions are incident-derived).

---

## 9. Rules-engine extensions (`src/rules/`)

Add three PPE-aware correlation rules that fuse with the existing alarm rules R1–R4 and the layer / fingerprint rules.

### 9.1 R5 — PPE step confirms fiber cut

```
∃ ppe_anomaly A of kind=step, magnitude_db ≤ -25, located_at fpos on span S
∧ ∃ alarm Y of name OTS_LOS, at_span S
⇒ confirm fault_type = fiber_cut(S, z_km = A.z_km),
  weight = 1.0, root = Y
```

Replaces the fallback heuristic that gets `fiber_cut` to 96 % class accuracy today; with PPE this should reach 100 % since the step localises the cut deterministically.

### 9.2 R6 — PPE slope confirms attenuation

```
∃ ppe_anomaly A of kind=slope, magnitude_db ∈ [0.05, 0.30], length ≥ 1 km on span S
∧ ∃ alarm Y of name IN_PWR_LOW or BEFFEC_EXC, at_span S
⇒ confirm fault_type = fiber_attenuation_high(S),
  weight = 0.8, root = Y
```

Earlier and more confident detection of soft attenuation, including some cases where the alarm hasn't yet tripped (the rule fires on PPE alone with weight 0.5).

### 9.3 R7 — Clean PPE rules out span-side cause

```
∃ alarm Y of name IN_PWR_LOW, at_port P (on span S)
∧ ¬∃ ppe_anomaly on span S above noise floor
⇒ confirm fault_type = rx_power_drift(port P),
  weight = 0.9, root = Y
```

This is the **negative-evidence** rule — the rules baseline misclassifies some `rx_power_drift` cases as `fiber_attenuation_high` because both raise `IN_PWR_LOW`. PPE adds the missing signal that the *span* is healthy, so the only remaining explanation is a receiver-side drift.

### 9.4 Rule predicates

`src/rules/schema.py` gains the new PPE-side predicates as first-class facts the reasoner can pattern-match, parallel to how it consumes alarm attributes today.

---

## 10. GNN / ML consumption (`src/ml/`)

Reads directly from `data/kg/*.csv` per Design/11. PPE adds two things to the inputs:

### 10.1 New node types

- `PpeTrace` and `PpeAnomaly` are added to the PyG HeteroData node typing.
- `FibrePosition` is added as a quantised positional node; its initial embedding is a sinusoidal positional encoding of `z_km` (mirroring transformer practice) so the GNN gets ordering of positions for free.

### 10.2 New edge types

The §8.2 predicates become edge types. `PpeIndicates` is the cross-modal fusion edge that links a `PpeAnomaly` to one or more candidate `Alarm` nodes; this is the edge most useful at inference.

### 10.3 Per-node features

- `PpeTrace`: `[mean_db, std_db, n_samples, delta_z_m]` and a small 1-D CNN over the residual `samples − baseline` (fixed length, 400 by default) producing a 16-dim embedding consumed by the R-GCN as a static feature.
- `PpeAnomaly`: `one-hot(kind), magnitude_db, confidence, length_km`.

### 10.4 Targets

T2 keeps the two existing targets — per-alarm `is_root` and per-pair `Correlated`. PPE is a feature, not a target. A T3 deliverable can add **fiber-position regression** (predict the `z_km` of the anomaly) as a separate head; we leave that to Design/11 §8 follow-up.

### 10.5 Why we expect PPE to help

The rule baseline's residual error (0.9 % root, 1.6 % class) comes mostly from the long-tail `fiber_cut` / `config_mismatch` cases where `drop_prob=0.05` removed a load-bearing alarm. PPE adds a feature that is **invariant** to alarm-drop noise (it's a continuous signal, not a discrete event), so the GNN should regain those cases.

---

## 11. Web UI changes (`src/app/`)

Two additions to `src/app/server.py` and the vanilla-JS frontend:

1. **`/api/incident/<sid>/ppe`** returns `{traces: [{channel, span, samples, baseline, anomalies}, …]}`.
2. **PPE panel** rendered alongside the alarm timeline. For each affected receiver, plot `P_0(z)` in grey and `P̂(z)` in red on a shared axis; mark anomalies with vertical lines and annotate `z_km` + `magnitude_db`. Click an anomaly to highlight the matching `Alarm` row in the timeline (and vice versa).
3. **Topology canvas overlay**: a coloured glyph at the anomaly's `z_km` along the affected span edge, so the operator sees the spatial localisation in topology-space, not just plot-space.

No new dependency — we already use vis-network for topology; a tiny dependency-free SVG renderer is sufficient for the profile plot.

---

## 12. Implementation roadmap

Mirrors the R-1..R-4 milestone style used in `Design/10`.

| Milestone | Scope | Verifies |
|---|---|---|
| **P-1** | `ppe.py` generator + `expand_ppe` + the 10 paired fault emitters + unit tests on shapes / signs | The per-fault signature table in §4 is reproducible from code, deterministically given a seed |
| **P-2** | `ppe_traces` / `ppe_anomalies` schema in `db.py` + recorder integration + `--with-ppe` on `generate_dataset.py` | 5120-incident default dataset regenerates in < 15 s with `--with-ppe`, adds ~100 MB to `incidents.db` |
| **P-3** | KG schema additions + `build_ppe_triples` + `ppe_triples.csv` mirror + batch builder reruns | `data/kg/` gains a clean `ppe_triples.csv`; round-trip via `to_networkx` works |
| **P-4** | `kg_engine` PPE handler + `CsvMirrorSubscriber` PPE row + adapter tails `ppe_traces` | Streaming engine emits PPE deltas in real time; parity test against batch builder |
| **P-5** | Rules R5/R6/R7 + reasoner extension + evaluate full dataset | New rules baseline: aim ≥ 99.7 % root accuracy on 5120 default + early detection on attenuation soft faults |
| **P-6** | Web UI panel + topology overlay | Visual inspection on at least 5 fault types; demo screenshot in `docs/` |
| **P-7** | GNN feature additions per §10 + T2 retraining | T2 GNN beats new rules baseline by a measurable margin on the residual long-tail |

P-1 through P-3 are independent of the streaming and ML deliverables and can ship first; the KG-schema additions (P-3) are the blocker for P-5 onward.

---

## 13. Open questions / risks

- **Vendor-specific PPE format.** Huawei has not (yet) published a canonical PPE notification schema; the `Library/` PDFs document the *concept* but not the wire format. P-4 will need to be re-shaped once we have access. Our internal event schema (§7.1) is deliberately generic so the NETCONF adapter can normalise into it.
- **Spatial resolution mismatch.** Real PPE has resolution that varies with symbol rate; we use a single global `Δz` for simplicity. If the GNN ends up sensitive to this, we'll need per-channel resolution and a resampling step in the loader.
- **Multi-amp spans.** v3 lab mesh has one amp per OMS; v4 will have multiple. The baseline-profile composer in §3.2 supports it but only the simplest case is tested in P-1.
- **PPE under L1/L2 cascade expansion.** The engine's L2 cause-keyword expansion (`Design/02 §6`) only operates on alarms. PPE traces are *not* expanded by L2 — they are direct ground-truth measurements per receiver, not heuristics. This is deliberate; documenting it here so a future contributor doesn't try to wire them in.
- **Real-time vs. polling cadence.** Production receivers can stream PPE at sub-second rates but typical NMS pipelines aggregate to 30–60 s. We default to 30 s; the cadence is configurable per-receiver via inventory YANG so a per-experiment over-ride is one config knob, not a code change.
- **Noise calibration.** `σ_per_km = 0.005 dB/km` is a placeholder. Once we have a reference PPE trace from a Huawei lab unit we can calibrate against it; until then the absolute SNR is approximate, but the *shape* of each fault's signature is correct.

---

## 14. References

- Tanimura et al., **"Fiber-Longitudinal Anomaly Position Identification Over Multi-Span Transmission Link Out of Receiver-end Signals,"** JLT 2020 — the foundational PPE paper.
- Sasai et al., **"Digital Backpropagation for Optical Path Monitoring,"** JLT 2021 — extends PPE to passband-narrowing diagnosis.
- Hahn et al., **"Localization of Reflection-Induced Anomalies via Longitudinal Power Profile Estimation,"** JLT 2023 — reflection signatures, useful for the connector-loss section of §4.
- `Design/02_simulator_design.md` §6, §9 — the alarm pipeline this extends.
- `Design/05_fault_event_catalog.md` §4, §8 — the 10 ★ faults and 21 alarm names this design is paired against.
- `Design/08_root_cause_rules.md` — the R1–R4 rule baseline R5–R7 extend.
- `Design/10_realtime_kg_engine.md` §3, §4 — the event-stream architecture `ppe.update` joins.
- `Design/11_gnn_training_plan.md` §2, §5 — the GNN this PPE data will feed.
- `src/simulator/faults.py` — the 10 fault emitters whose PPE-paired siblings P-1 will land.
- `src/kg/schema.py` — the authoritative URI / predicate registry that §8 extends.
