# Domain Analysis — Huawei OptiX OSN 8800/6800/3800 Faults

Sources: the two PDFs in `Library/` (V100R011C10, 2017-03-31).

## 1. Equipment & Layer Model

The OptiX OSN is an optical transport platform. From an alarm-management standpoint the relevant hierarchy is:

```
NMS (U2000)
  └─ Subnet
      └─ NE (Network Element / station: OTM, OLA, ROADM, REG, ...)
          └─ Subrack
              └─ Board (line card / OTU / OA / FIU / SCC / clock / fan / power)
                  └─ Port (optical / electrical / management)
                      └─ Channel / Wavelength
                          └─ Service (SDH / OTN / Ethernet / GE)
```

A single physical fault (e.g. fiber cut, faulty pluggable optic, board reset) produces a cascade of alarms across multiple layers. The job of the RCA system is to walk that cascade back to the originating fault.

## 2. Alarm Taxonomy

The reference uses three orthogonal classifications:

- **Severity** — `Critical`, `Major`, `Minor`, `Warning`.
- **Type** — `Service alarm` (affects traffic), `Communication alarm`, `Equipment alarm`, `Processing-error alarm`, `Environment alarm`, `Quality of service`.
- **Service-Affecting** — boolean column in the product alarm list.

Each detailed alarm entry uses a fixed schema we can parse:

```
<NAME>
  Description     (free text)
  Attribute       Severity | Type
  Parameters      ordered parameter list (port id, path id, ...)
  Impact on the System
  Fault Symptom   (mostly trouble-shooting doc)
  Possible Causes (numbered: Cause 1, Cause 2, ...)
  Procedure       (Step 1: ... Step 2: ...)
  Related Information
```

There are ~680 distinct alarms in the reference (counted by occurrences of "Possible Causes").

## 3. Causal Knowledge — Where it Lives in the PDFs

### 3.1 Alarm Signal Flow (Section 1.5–1.6)

Diagrams that show how a degraded signal propagates through OTU/OCS hardware and what each functional block detects. These are the textbook propagation rules — useful as KG seed edges.

Examples:

- Non-Convergent OTU on SDH signal: `R_LOS` at the receive port → `OTUk_AIS` injected → downstream `RS_AIS`/`MS_AIS`/`AU_AIS`.
- Non-Convergent OTU on OTN signal: `OTUk_LOF` triggers `OTUk_AIS`, masks B1/B2 performance events.

### 3.2 Suppression Relations (Section 1.7–1.9) — KG Edges

This is the cleanest source of `causes / suppresses` edges. Format: "the alarm above the arrow suppresses the alarm below".

**OTN Optical Layer** (Fig. 1-20):

```
OTS_LOS → OTS_LOS_P, OTS_LOS_O
OTS_LOS → OMS_SSF → OMS_SSF_P, OMS_SSF_O
OMS_SSF → OMS_FDI → OMS_FDI_P, OMS_FDI_O
OMS_SSF → OCh_SSF → OCh_SSF_P, OCh_SSF_O
OCh_SSF → OCh_FDI → OCh_FDI_P, OCh_FDI_O
OCh_LOS_P / OCh_OCI → R_LOS in OTU boards → OTUk_SSF
OTS_TIM → OTS_BDI → OTS_BDI_O, OTS_BDI_P
OMS_PMI, OTS_PMI = upstream presence indicators
```

**OTN Electrical Layer** (Fig. 1-21):

```
OCH_LOS / LOL → OTUk_SSF → OTUk_LOF → OTUk_LOM → OTUk_TIM → OTUk_BDI / OTUk_DEG
                                                              ↓
                            ODUk_LOFLOM
OPUk_PLM, OPUk_MSIM, ODUk_PM_TIM → ODUk_PM_BDI / ODUk_PM_DEG
ODUk_TCMn_LTC, _LCK, _OCI, _TIM → ODUk_TCMn_BDI, _DEG
```

**SDH** (Fig. 1-23):

```
R_LOS → R_LOF → MS_AIS → AU_AIS → AU_LOP → HP_TIM, HP_SLM, HP_UNEQ, B3_EXC
                       → B2_EXC → B1_EXC
```

**Inter-node** (Sec. 1.7 narrative): when alarm-suppression and optical-layer monitoring are enabled, an OTS-level fault on an upstream OLA suppresses MUT_LOS, OSC_LOS, R_LOS at downstream OTM. The single root surfaced is `OTS_LOS`.

### 3.3 Performance-Event Suppression (Section 1.9)

`R_LOS` masks 13–14 performance counters (MSBBE, RSBBE, FEC_AFT_COR_ER, etc.). For the dataset generator: do not emit those PMs while R_LOS is present.

## 4. Fault Categories (from Trouble-Shooting Doc)

Section 4–12 of the trouble-shooting PDF enumerates the *root* fault incidents that we want our model to predict. Mapping to model labels:

| Category | Examples | Typical observed alarms |
|---|---|---|
| **Service interruption — fiber/cable** | fiber cut, dirty connector, pinched fiber | R_LOS, OTS_LOS, MUT_LOS, OCh_LOS_P |
| **Service interruption — board** | board reset, optical module fail, laser death | HARD_BAD, BD_STATUS, BUS_ERR, PUM_FAIL, R_LOS at one port |
| **Service interruption — power supply** | -48V loss, PEM failure | POWER_ABNORMAL, PWR_MAJ_ALM, BD_OFFLINE |
| **Service interruption — grounding** | bad ground | random bit errors, intermittent OTS alarms |
| **Service interruption — environment** | over-temp, dust, humidity | TEMP_OVER, FAN_FAIL, AMBIENT_TEMP_FLUCT |
| **Service interruption — config mismatch** | wavelength misconfig, port-rate mismatch | OPUk_PLM, OPUk_MSIM, OTUk_TIM, ODUk_PM_TIM |
| **Single-channel ETH interrupt** | tributary port fault | ETH_LOS, ALM_GFP_dCSF, LINK_ERR |
| **Optical-power abnormality** | aging fiber, span loss, OA gain | IN_PWR_ABN, OUT_PWR_ABN, MUT_LOS, ALC_ADJUST_FAIL |
| **Bit errors — environment** | EMI, vibration | B1_EXC, B2_EXC, FEC_BEF_COR_ER high |
| **Bit errors — device** | bad XFP, RX deteriorating | B1_EXC, B2_EXC, OTUk_DEG |
| **NE reachability** | gateway broken, ECC fault | NE_OFFLINE, COMMUN_FAIL, ECC_LOST |
| **Board offline** | comm with SCC lost | BD_STATUS, BD_OFFLINE, FW_BD_NOMATCH |
| **Protection switch fail** | OLP/SNCP/DLAG | PROTOCOL_MISMATCH, APS_INDI, TPS_ALARM |
| **IPA/ALC/APE issues** | EDFA gain runaway, Raman safety trip | IPA_TRIGGER, RAMAN_PUMP_FAIL, APE_ADJUST_FAIL |

These ~14 categories are a reasonable label space for the first ML model.

## 5. Implications for the System Design

1. **Knowledge graph schema** is well-defined by the source material:
   - Node types: `NE`, `Board`, `Port`, `Link`, `Channel`, `Service`, `AlarmType`.
   - Edge types: `hosts`, `connects`, `runs-over`, `suppresses` (alarm→alarm), `caused-by` (alarm→fault-category), `affects` (fault→service).

2. **Simulator must reproduce suppression behavior** — otherwise the labeled dataset will not match the alarms a real NMS would deliver. We will implement Section 1.7's suppression rules verbatim before randomizing incidents.

3. **Dataset record shape** (proposal):
   ```
   {
     incident_id, root_cause, fault_location {ne, board, port},
     observed_alarms: [{name, ne, board, port, severity, t_offset_ms}, ...],
     topology_snapshot: <id>
   }
   ```
   With suppression on, `observed_alarms` is small (the masked alarms removed); with suppression off, it's the full cascade. Train both — operators may get either.

4. **Multi-agent decomposition** (six agents):
   - `Collector` — ingests raw alarms from NEs.
   - `Topology` — supplies the snapshot graph context.
   - `Suppressor` — applies/inverts the published suppression rules.
   - `KG-Reasoner` — graph reachability / shortest-cause queries.
   - `ML-Predictor` — calls the trained model on the alarm vector.
   - `Coordinator` — combines KG + ML evidence, returns the ranked root cause.

The architecture document (next task) will fix the messages between agents.
