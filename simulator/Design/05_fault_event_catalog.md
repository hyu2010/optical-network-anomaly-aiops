# Fault-Event Catalog — v3

Status: **v3 — current.** v2 was the bucket-and-list audit; v3 is the catalogue the v3 simulator actually runs against the lab-mesh topology + OSN 9800 alarm set.
Purpose: a curated catalogue of *physical fault events* the simulator can inject. Each fault becomes a label in `incidents.fault_type`; the alarms it produces become the input features for the ML / RCA pipeline. v1 covered 4 fault types; v2 enumerated 18 candidates; v3 implements the 10 ★ priority faults from §4 against the **OSN 9800 alarm catalog** (`data/alarm_catalog_9800.json`, 796 entries) with the 8800 catalog kept as a fallback for the few names 9800 does not ship.

This catalogue is the answer to the question "we have ~800 alarms — why do we only inject a handful of fault types?" — see `Design/02_simulator_design.md` §6 for the original v1 list and the audit motivation. **For the v3 alarm-name inventory the simulator actually emits (and why those 21 names), see §8.**

---

## 1. Why this list

A fault event is **the thing that goes wrong in the network**; an alarm is a **symptom**. The simulator's job is to turn one fault event into a realistic alarm cascade. So we need fault ids that are:

1. **Physically distinct** — a fiber cut and a dirty connector both involve the fiber, but the operator response is different (replace fiber vs. clean connector), so they get different ids.
2. **Cascade-distinguishable** — each fault must produce an alarm set that an ML model can learn to tell apart. Identical cascades collapse into one fault id.
3. **Coverage-driven** — the union of all faults' cascades should exercise every meaningful alarm family in the catalog.

v2 had 816 alarms in `data/alarm_catalog.json` (the OSN 8800 set); v3 runs against `data/alarm_catalog_9800.json` (796 alarms, the OSN 9800 set). Both cluster into roughly the same **20 prefix families**, of which **15 are physically meaningful**. The per-family counts shift slightly between the two catalogs — most visibly ODU drops from 127 → 99 because 9800 ships fewer rate-suffix variants per family, and 9800 introduces a new `SWDL_*` software-download family (14 entries) that 8800 lacks. The remaining 5 families are vendor-specific control-plane codes (`AD_CHECK_FAIL`, `ARP_FAIL`, etc.) that fire as side effects, not as roots.

## 2. Alarm-family audit (v2 baseline; v3 deltas noted in §8.1)

| Family | Count | Example alarm names |
|---|---:|---|
| Other (vendor-internal control-plane) | 429 | `AD_CHECK_FAIL`, `ALM_HANGUP`, `ARP_FAIL` |
| OTN ODU | 127 | `ODU2_PM_SSF`, `ODU2_PM_BDI`, `ODU2_TCMn_TIM`, `ODU0_LOFLOM` |
| Ethernet / client | 48 | `ETH_LOS`, `ETH_8B10B_ERR`, `ALM_GFP_dCSF` |
| OTN OTU | 42 | `OTU2_LOF`, `OTU2_LOM`, `OTU2_TIM`, `OTU2_BDI`, `OTU2_DEG`, `OTU2_SSF` |
| SDH | 28 | `MS_AIS`, `AU_AIS`, `AU_LOP`, `B1_EXC`, `B2_EXC`, `HP_TIM`, `HP_SLM` |
| Laser / ALS | 19 | `ALS_TRIGGERED`, `ALS_ACTIVE`, `LSR_WILL_DIE`, `LASER_SHUT`, `ALM_ALS` |
| OTN OPU | 19 | `OPU2_PLM`, `OPU2_MSIM`, `OPU2_CSF` |
| Optical OMS | 15 | `OMS_SSF`, `OMS_FDI`, `OMS_BDI` (each ×3 with `_O`/`_P` variants) |
| FEC | 13 | `BEFFEC_EXC`, `FEC_BEF_COR_ER`, `FEC_AFT_COR_ER` |
| Board state | 12 | `BD_OFFLINE`, `BD_STATUS`, `HARD_BAD`, `BD_BPXC_MISMATCH`, `WRG_BD_TYPE`, `FW_BD_NOMATCH` |
| RX / optical-power | 11 | `MUT_LOS`, `IN_PWR_LOW`, `IN_PWR_HIGH`, `IN_PWR_ABN`, `CHAN_LOS` |
| Auto-power-mgmt | 8 | `ALC_ADJUST_FAIL`, `APE_ADJUST_FAIL`, `IPA_TRIGGER`, `GAINDATA_MIS` |
| Clock / sync | 8 | `CLK_LOCK_FAIL`, `SYNC_FAIL`, `SYNC_C_LOS` |
| Optical OCh | 8 | `OCh_SSF`, `OCh_FDI`, `OCh_LOS_P`, `OCh_OCI` |
| Optical OTS | 8 | `OTS_LOS`, `OTS_BDI`, `OTS_LOS_O`, `OTS_LOS_P` |
| Protection-switch | 7 | `APS_FAIL`, `APS_INDI`, `DLAG_PROTECT_FAIL`, `OLP_PS` |
| Environment | 5 | `FAN_FAIL`, `TEMP_OVER`, `AMBIENT_TEMP_FLUCT`, `PUMP_COOL_EXC` |
| Power | 5 | `POWER_ABNORMAL`, `POWER_FAIL`, `PWR_UNBALANCED` |
| NMS / management | 3 | `COMMUN_FAIL`, `DCNLINK_OVER`, `DCNSIZE_OVER` |
| Loopback | 1 | `LOOP_ALM` |

## 3. Mapping: physical fault → alarm families it activates

| Physical category | Alarm families fired | What's actually broken |
|---|---|---|
| Fiber medium | OTS, OMS, OCh, RX/optical-power, Laser/ALS | the glass between two NEs |
| OTN electrical | OTU, ODU, OPU, FEC, SDH | a board's framer / mapper / FEC engine |
| Optical amplifier | RX/optical-power, Auto-power-mgmt, Laser/ALS, Environment (`PUMP_COOL_EXC`) | EDFA / Raman amplifier |
| Equipment | Board-state, Power, Laser/ALS, Loopback | a chassis / board / power / pluggable optic |
| Environment | Environment, Power | room AC / fan / dust / humidity |
| Configuration | OPU (`PLM`/`MSIM`), OTU (`TIM`), ODU (`PM_TIM`, `TCMn_TIM`) | wrong wavelength / rate / trail-id provisioned |
| Protection | Protection-switch | OLP / SNCP / DLAG cannot do its job |
| Management | NMS/management, Clock/sync | gateway, ECC, SCC, clock reference |

## 4. The v2 catalog — 18 fault events

Faults marked **★** are the **priority-10 set** I recommend implementing first. They cover the 4 v1 faults plus the 6 most common operator-visible failure modes; together they exercise every one of the 15 physically-meaningful alarm families and all four physical buckets.

### 4.1 Bucket A — Fiber-medium faults
Target: a directed `span_xx` link in the optical-topo network.

| # | Fault id | Status | What's broken | Primary alarms |
|---|---|---|---|---|
| 1 | **★ `fiber_cut`** | v1 | fiber severed | `OTS_LOS` → OMS / OCh cascade → `R_LOS` → OTU / ODU cascade |
| 2 | **★ `fiber_attenuation_high`** | new | dirty connector, aged fiber, partial loss | `IN_PWR_LOW`, `BEFFEC_EXC`, `OTU_DEG` (no full LOS) |
| 3a | **★ `b1_exc_high`** | new | regenerator-section B1 byte errors over threshold | `B1_EXC`, `OTU_DEG` |
| 3b | **★ `b2_exc_high`** | new | multiplex-section B2 byte errors over threshold | `B2_EXC`, `B1_EXC` (suppressed downstream), `OTU_DEG` |
| 3c | **★ `fec_pre_cor_high`** | new | FEC pre-correction error rate over threshold (OTN) | `BEFFEC_EXC`, `FEC_BEF_COR_ER`, `OTU_DEG` |

### 4.2 Bucket B — Optical-power & laser faults
Target: an NE+board+port (line-side optical TP), or an NE+OA-board.

| # | Fault id | Status | What's broken | Primary alarms |
|---|---|---|---|---|
| 4 | **★ `rx_power_drift`** | new | input optical power out of spec ("light signal drift") | `IN_PWR_ABN`, `IN_PWR_LOW` or `IN_PWR_HIGH`, `MUT_LOS` if low enough |
| 5 | `wavelength_drift` | new | laser carrier frequency wandered off the ITU grid | `CHAN_LOS` at the receive de-mux, `OPU_PLM` if the wrong λ is selected |
| 6 | `oa_pump_failure` | new | EDFA pump laser dies | `OUT_PWR_ABN` zero, `PUMP_COOL_EXC` (Raman), then full downstream cascade like `fiber_cut` |
| 7 | `oa_gain_drift` | new | EDFA / Raman gain drifted, ALC cannot correct | `ALC_ADJUST_FAIL`, `APE_ADJUST_FAIL`, `IN_PWR_HIGH/LOW` downstream |
| 8 | `tx_laser_die` | new | pluggable optic's transmit laser fails | local: `LSR_WILL_DIE`, `ALS_TRIGGERED`; peer: `R_LOS` |

### 4.3 Bucket C — Equipment faults
Target: an NE, or NE+board.

| # | Fault id | Status | What's broken | Primary alarms |
|---|---|---|---|---|
| 9 | **★ `ne_power_off`** | v1 | whole NE loses -48V or PEM | `POWER_ABNORMAL`, every peer NE sees `R_LOS` |
| 10 | **★ `board_power_off`** | v1 | one board lost power | `BD_OFFLINE`, peer-port `R_LOS` |
| 11 | `board_hardware_fault` | new | board CPU / bus / control plane fails | `HARD_BAD`, `BUS_ERR`, `BD_STATUS`, `BD_SESSION_FAIL` |
| 12 | `board_type_mismatch` | new | wrong board inserted in slot | `BD_BPXC_MISMATCH`, `WRG_BD_TYPE`, `FW_BD_NOMATCH` |
| 13 | `laser_module_mismatch` | new | wrong pluggable optic inserted | `LASER_MODULE_MISMATCH` |

### 4.4 Bucket D — Environmental, configuration, protection, management
Target: an NE, or NE+board+port.

| # | Fault id | Status | What's broken | Primary alarms |
|---|---|---|---|---|
| 14 | **★ `fan_failure`** | v1 | fan stops moving | `FAN_FAIL` → eventually `TEMP_OVER` on every board |
| 15 | `ambient_temp_high` | new | room AC failure (fan still spinning) | `TEMP_OVER` without `FAN_FAIL`, `AMBIENT_TEMP_FLUCT` |
| 16 | **★ `config_mismatch`** | new | wavelength / rate / trail-id mismatch | `OPU_PLM`, `OPU_MSIM`, `OTU_TIM`, `ODU_PM_TIM` |
| 17 | `protection_switch_failure` | new | APS / SNCP / DLAG cannot switch | `APS_FAIL`, `APS_INDI`, `DLAG_PROTECT_FAIL`, `PROTOCOL_MISMATCH` |
| 18 | `ne_unreachable` | new | gateway / ECC fails — NMS loses contact | `COMMUN_FAIL`, `DCNLINK_OVER`, `NE_OFFLINE` |

## 5. Key design observations

### 5.1 We do not implement one fault per primary alarm

A fault id is the **label** the ML model predicts; the cascade is the **input feature**. Two faults that produce identical cascades collapse into one id, even if their physical names differ. Conversely, two faults that involve the same component but different repair actions (`fiber_cut` vs `fiber_attenuation_high` vs `bit_error_high` — all three involve the fiber) get different ids whenever their cascades are distinguishable.

### 5.2 Soft faults vs hard faults

Several faults differ only by *how complete* the loss is — `fiber_cut` (full LOS) vs `rx_power_drift` (partial degradation) vs `bit_error_high` (no LOS at all, just FEC pre-correction errors). All three need to be in the catalogue because operators must respond differently. The cascade signature is what tells them apart:

| Signature element | `fiber_cut` | `rx_power_drift` | `bit_error_high` |
|---|---|---|---|
| `R_LOS` / `OTS_LOS` fires | yes | sometimes | no |
| `IN_PWR_LOW` / `IN_PWR_HIGH` fires | yes | yes | no |
| `BEFFEC_EXC` / `B2_EXC` fires | yes (after AIS unlocks) | sometimes | yes (this is the only signature) |
| `OTU_DEG` fires | yes | yes | yes |

### 5.3 The `Other` bucket of 429 alarms

Vendor-specific board-internal codes (`AD_CHECK_FAIL`, `ARP_FAIL`, `ALM_HANGUP`, `MAN_AGENT_*`). They fire as side effects of the faults above, not as roots in their own right. **No fault event in this catalogue uses them as primaries.** They will appear in cascades only via L1 suppression-graph propagation or L2 cause-text mining — never as a fault id.

### 5.4 The `OTN ODU` family (~99 alarms in 9800, 127 in 8800)

This is the largest physically-meaningful family in either catalog. Most entries are `(rate × layer × position)` combinatorics — `ODU0_PM_SSF`, `ODU2_PM_SSF`, `ODU4_PM_SSF`, etc. The simulator only needs to fire the **family-form** name (`ODU_PM_SSF`); the rate suffix is filled in at lookup time from the carrying tunnel's `rate-type` (`ietf-otn-tunnel:otn/rate-type`). This keeps the rule logic clean and matches `Library/Root cause analysis of alarms.txt`. The web UI's description lookup (`src/app/server.py::_description_for`) tries the 9800 catalog first and falls back to 8800 for any name 9800 does not ship — see §8.

## 6. Implementation order

This catalogue is data, not code. The simulator's `faults.py` will be extended in two passes:

### 6.1 Pass 1 — priority-10 set (the ★ faults) plus engine upgrades

Add the 6 new ★ faults (`fiber_attenuation_high`, `bit_error_high`, `rx_power_drift`, `config_mismatch` — the ones not in v1) plus L1 suppression-graph forward-walk and L2 cause-keyword index in `engine.py`. After this pass:

- v2 catalogue covers ~10 fault types (vs v1's 4).
- Cascades grow from 6–10 alarms each to 30–60.
- Distinct alarm-name coverage grows from 11 to ~80.

### 6.2 Pass 2 — remaining 8 fault types

Add the 8 unstarred faults as we need more class diversity in the ML training set:
`wavelength_drift`, `oa_pump_failure`, `oa_gain_drift`, `tx_laser_die`, `board_hardware_fault`, `board_type_mismatch`, `laser_module_mismatch`, `ambient_temp_high`, `protection_switch_failure`, `ne_unreachable`.

(That's 10 — including #5 `wavelength_drift`. Pass 2 finishes the catalogue to 18.)

### 6.3 Adding a fault later

Each fault is one Python function in `faults.py` that takes `(topology, *args)` and returns `list[Alarm]`. With L1 + L2 in place each new function is ~20 lines: name the primary alarm + a few cause-keywords + the location-walk for the target. The rest is generated automatically.

## 7. Decisions (recorded 2026-04-29)

1. **Naming scheme** — keep the descriptive form (`fiber_attenuation_high`, `rx_power_drift`, etc.). No shortening.
2. **`bit_error_high` granularity** — **split by byte / FEC layer.** One id per signature: `b1_exc_high` (regenerator-section), `b2_exc_high` (multiplex-section), `fec_pre_cor_high` (OTN FEC). Reflected in §4.1 above.
3. **`config_mismatch` granularity** — keep as a single id. Wavelength / rate / trail-id mismatches all collapse here.
4. **Additions outside the list** — none for now.

The priority-10 set (★) therefore becomes:
`fiber_cut`, `fiber_attenuation_high`, `b1_exc_high`, `b2_exc_high`, `fec_pre_cor_high`, `rx_power_drift`, `ne_power_off`, `board_power_off`, `fan_failure`, `config_mismatch`.

Implementation proceeds per §6.1 (priority-10 + L1 + L2).

## 8. v3 — alarms the simulator actually emits

The 10 ★ priority faults implemented in `src/simulator/faults.py` together emit **21 distinct alarm names**. Each name uses the **family form** (no rate suffix); at lookup time the description is resolved against the OSN 9800 catalog first, with the OSN 8800 catalog as fallback.

### 8.1 Why we run on OSN 9800 (and what changed from v2)

The v3 lab-mesh topology (`data/yang/topology.json` — 6 hubs A–F, 8 OMS spans, 6 OCh services) is captured from a reference deployment built on the **Huawei OptiX OSN 9800** platform. That is the equipment whose alarm semantics matter for the operators we are targeting, so the alarm authority for v3 is `data/alarm_catalog_9800.json`.

Concrete deltas vs the v2 (8800) catalog:

- **9800 is slightly smaller (796 vs 816)** — mostly because 9800 collapses some `(rate × tail)` combinations 8800 enumerates separately. Most visibly: ODU 127 → 99, OTU 42 → 43.
- **A handful of 8800 names are missing from 9800** — notably `FEC_BEF_COR_ER` and the literal `BD_OFFLINE` token. The simulator still emits them; the description lookup falls back to the 8800 catalog (or, for `BD_OFFLINE`, to a project-defined sentinel — see §8.2).
- **9800 introduces a `SWDL_*` software-download family** (14 entries) that 8800 lacks — none of those are simulator roots.

We did not delete the 8800 catalog. The alarm-description lookup in `src/app/server.py::_description_for` queries 9800 first and falls back to 8800 for any name 9800 does not ship. This keeps every name the simulator emits resolvable while making 9800 the system of record.

### 8.2 The 21 alarm names

| Family | Alarm name (family form) | Where it fires | Resolves in 9800 / 8800 |
|---|---|---|---|
| OTS | `OTS_LOS` | RX side of a cut span | `OTS_LOS` / `OTS_LOS` |
| OTS | `MUT_LOS` | same RX port, milliseconds later | `MUT_LOS` / `MUT_LOS` |
| OCh | `R_LOS` | downstream OTU RX port | `R_LOS` / `R_LOS` |
| OTU | `OTU_SSF` | downstream OTU RX port | `OTU0_SSF` / `OTU1_SSF` |
| OTU | `OTU_BDI` | upstream TX port (return path) | `OTU0_BDI` / `OTU1_BDI` |
| OTU | `OTU_DEG` | downstream OTU RX port (soft fault) | `OTU0_DEG` / `OTU1_DEG` |
| OTU | `OTU_TIM` | misconfigured OTU RX port | `OTU0_TIM` / `OTU1_TIM` |
| ODU | `ODU_PM_SSF` | destination client TP of an affected ODU tunnel | `ODU0_PM_SSF` / `ODU0_PM_SSF` |
| ODU | `ODU_PM_TIM` | misconfigured ODU RX port | `ODU0_PM_TIM` / `ODU0_PM_TIM` |
| OPU | `OPU_PLM` | misconfigured OPU RX port | `OPU0_PLM` / `OPU0_PLM` |
| Equipment | `POWER_ABNORMAL` | NE-level power loss | `POWER_ABNORMAL` / `POWER_ABNORMAL` |
| Equipment | `BD_OFFLINE` | board lost power | — / — (project sentinel; description left blank) |
| Environment | `FAN_FAIL` | fan board stops | `FAN_FAIL` / `FAN_FAIL` |
| Environment | `TEMP_OVER` | every other board, ~120 s after `FAN_FAIL` | `TEMP_OVER` / `TEMP_OVER` |
| RX power | `IN_PWR_LOW` | OTS port input level below threshold | `IN_PWR_LOW` / `IN_PWR_LOW` |
| RX power | `IN_PWR_HIGH` | OTS port input level above threshold | `IN_PWR_HIGH` / `IN_PWR_HIGH` |
| RX power | `IN_PWR_ABN` | RX-power-out-of-range follow-up | `IN_PWR_ABN` / `IN_PWR_ABN` |
| FEC | `BEFFEC_EXC` | OTU RX port FEC pre-correction over threshold | `BEFFEC_EXC` / `BEFFEC_EXC` |
| FEC | `FEC_BEF_COR_ER` | follow-up to `BEFFEC_EXC` | — / `FEC_BEF_COR_ER` (8800 fallback) |
| SDH | `B1_EXC` | regenerator-section B1 byte errors | `B1_EXC` / `B1_EXC` |
| SDH | `B2_EXC` | multiplex-section B2 byte errors | `B2_EXC` / `B2_EXC` |

### 8.3 Why this subset (and not the other ~775 alarms)

Three filters narrow the OSN 9800 catalog down to these 21 names:

1. **The fault must be a root, not a side effect.** Of the 20 prefix families, 5 are vendor-internal control-plane codes (`Other` bucket) that only ever appear as side effects — `AD_CHECK_FAIL`, `ARP_FAIL`, `MAN_AGENT_*`, `SWDL_*`. None of them are valid root labels for an ML model that has to predict *what to repair*.
2. **The fault must be physically distinguishable.** Two faults that produce identical cascades collapse into one fault id. We only emit alarms that contribute to a distinguishable signature; anything in the catalog that fires in the *same* circumstances as something we already emit is redundant for ML purposes.
3. **The fault must be reproducible from the topology + inventory we model.** v3's lab mesh has 6 hubs, 8 OMS spans, OTU/ODU/OA/FIU/FAN/SCC boards. Faults that need hardware we do not model (Raman pumps, OLP cards, GFP framers, dedicated APS controllers) are out of scope until v4.

Applying all three filters to the 796-entry 9800 catalog leaves the 10 ★ faults in §4. Those 10 faults emit the 21 names above. The remaining ~775 alarms in 9800 are either (a) rate-variant siblings already covered by the family-form name (e.g. `ODU2_PM_SSF` collapses to `ODU_PM_SSF`), (b) downstream cascade alarms that L1 / L2 expansion in `engine.py` adds automatically, or (c) out-of-scope categories per filter 3.

### 8.4 What is *not* in v3 yet (planned for v4)

The 8 unstarred faults from §4.2–§4.4 — `wavelength_drift`, `oa_pump_failure`, `oa_gain_drift`, `tx_laser_die`, `board_hardware_fault`, `board_type_mismatch`, `laser_module_mismatch`, `ambient_temp_high`, `protection_switch_failure`, `ne_unreachable` — are the v4 backlog. Each will pull in 2–6 additional 9800 alarm names (notably `LSR_WILL_DIE`, `ALS_TRIGGERED`, `PUMP_COOL_EXC`, `ALC_ADJUST_FAIL`, `APS_FAIL`, `COMMUN_FAIL`).
