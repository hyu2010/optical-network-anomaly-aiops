# OptiX OSN 9800 Product Documentation — Supplement

Status: **Reference analysis — adopted in v3.** When written, this was framed as supplementary background to the 8800/6800/3800 reference. v3 has since promoted OSN 9800 to be the **primary alarm authority** for the simulator: the alarm-description lookup queries `data/alarm_catalog_9800.json` first (796 entries) and falls back to `data/alarm_catalog.json` (8800, 816 entries) only for the few names 9800 does not ship. The simulator topology is also taken from a 9800-platform lab — see `Design/07_lab_mesh_topology.md`. For the per-name resolution table and which 9800 alarms the v3 simulator actually emits, see `Design/05_fault_event_catalog.md` §8.

This document supplements `docs/01_domain_analysis.md` (which covered the 8800/6800/3800 V100R011C10 reference). On 2026-05-04 you uploaded three OSN 9800 PDFs totalling 1876 pages plus a real-world alarm-table dump (`Library/Alm_format.txt`) and the source URL (`Library/OptiX_OSN_9800_Product_Documentation_link.txt`). The 9800 docs are the **newer-platform sibling** of the existing 8800 reference; their structure and alarm-entry format are identical, the differences are in scope and the management plane.

---

## 1. Document inventory

| File | Pages | Source-page range | Size |
|---|---|---|---|
| `OptiX OSN 9800 Product Documentation_…_1_1000.pdf` | 1000 | 1–1000 | 6.9 MB |
| `…_1001_1876_1_800.pdf` | 800 | 1001–1800 | 9.1 MB |
| `…_1001_1876_801_876.pdf` | 76 | 1801–1876 | 3.6 MB |
| **Total** | **1876** | — | 19.6 MB |

All three were extracted to `Library/extracted/osn9800.txt` (3.0 MB plain text) by `Library/extracted/extract_9800.py`. Source: `https://support.huawei.com/hedex/hdx.do?docid=EDOC1100555965&id=EN-US_TOPIC_0000001072976983`.

## 2. How the 9800 doc is organised

The chapter we care about is **1.2 Alarms and Performance Events**. Inside it the relevant sub-sections (with TOC pages) are:

```
1.2.2.4  Alarm Signal Flow (OTN)                ←  same as 8800
1.2.2.5  Alarm Signal Flow (SDH)                ←  same as 8800
1.2.2.6  Alarm Signal Flow (Packet)             ←  *new* in 9800 — Ethernet / MPLS / PW
1.2.2.8  Alarm Signal Flow of a Tributary or Line Board   ← *new* in 9800
1.2.2.9  Suppression Relationship Between Alarms          ← same as 8800 §1.7
1.2.3    Product Alarm List                                ← same as 8800 §2
1.2.4    Clearing Common Alarms                            ← same as 8800 §3 — your focus
```

## 3. Structural comparison with 8800

| | **8800/6800/3800 V100R011C10** | **OSN 9800** |
|---|---|---|
| Total pages | 1492 (Alarms ref) + 273 (Trouble Shoot) | 1876 (combined) |
| Alarm entries with "Possible Causes" | **680** | **884**  (+30 %) |
| NMS referenced in Procedure text | U2000 | NCE |
| Alarm-entry layout (heading → Description → Attribute → Parameters → Impact → Fault Symptom → Possible Causes → Procedure → Related Information) | identical | identical |
| Bullet character used in cause/step lists | `l` (lowercase L) | `•` (Unicode bullet) |
| OTN signal-flow section | yes | yes |
| SDH signal-flow section | yes | yes |
| **Packet signal-flow section** (Ethernet/MPLS/PW) | no | **yes — new** |
| Per-board signal-flow section | no | **yes — new** |
| Suppression-relationship diagrams | yes (§1.7) | yes (§1.2.2.9) |
| Real-world ALM-CUR-DATA-ALL format | not shown | shown (`Library/Alm_format.txt`) |

**The single most important practical difference** is the bullet-character switch (`l` → `•`). Our existing parser `src/parser/parse_alarms.py` keys its cause-list extraction off `l Cause N:`; it would currently miss the 9800's `• Cause N:` cases. One-line fix when we choose to ingest 9800 too.

## 4. Section 1.2.4 Clearing Common Alarms — deep dive

This is the section you asked me to focus on. It lists **61 alarms** for which the 9800 reference provides full handling procedures (`Description`, `Attribute`, `Parameters`, `Impact on the System`, `Fault Symptom`, `Possible Causes`, `Procedure`, `Related Information`). Each is documented just as in 8800 §3.

### 4.1 The 61 Common Alarms

```
B1_EXC               BD_STATUS            BEFFEC_EXC           BIOS_STATUS
BOOTROM_BAD          BP_ODU_LOFLOM*       BUS_ERR              CHAN_LOS
CLIENT_PORT_PS       COMMUN_FAIL          DBMS_ERROR           FAN_FAIL
FAN_FAULT            HARD_BAD             HARD_ERR             HARD_NONSUPPORT
INNER_COMMUN_MISCON  INSUFFCNT_FLSH_SPACE IN_PWR_HIGH          IN_PWR_LOW
LAN_LOC              LASER_MODULE_MISMATCH LASER_MOD_ERR       LINK_ERR
LOCAL_FAULT          LPT_ACTIVE           LSR_WILL_DIE         MEM_OVER*
MOD_COM_FAIL         MS_AIS               MS_RDI               MUT_LOS
NEBD_XC_DIF          NO_ELABEL            OA_LOW_GAIN          ODU_SNCP_PS
ODU_SNCP_WEAK        ODUk_LOFLOM†         ODUk_PM_EXC†         OPA_FAIL_INDI
OTUk_LOF†            OUT_PWR_HIGH         PORT_MODULE_OFFLINE  POWER_DIFF_OVER
POWER_FAIL           PUMP_COOL_EXC        REMOTE_FAULT         REM_SF
R_LOF                R_LOS                R_SLIP               SECU_ALM
SUBRACK_ID_CONFLICT  SUM_INPWR_LOW        SWDL_CHGMNG_NOMATCH  SWDL_NEPKGCHECK
SYNC_C_LOS           TEMP_OVER            TF                   WRG_BD_TYPE
XCP_DEGRADE*
```

`*` = new alarm name not in the 8800 catalog at all.
`†` = placeholder-form name (`OTUk_LOF`, `ODUk_LOFLOM`, `ODUk_PM_EXC`) that 8800 listed only under rate-specific variants (`OTU2_LOF`, `ODU2_LOFLOM`, etc.). Same alarm, different naming convention in the doc.

### 4.2 Overlap with the 8800 catalog

| Bucket | Count | What it means |
|---|---|---|
| Name appears verbatim in `data/alarm_catalog.json` (8800) | **55** | Identical alarm. No catalog change needed — handling text in 9800 may differ in wording (NCE vs U2000) but the alarm is the same. |
| Placeholder-form name; 8800 has rate-specific variants | **3** | `OTUk_LOF` ↔ {`OTU1_LOF`,`OTU2_LOF`,`OTU3_LOF`,`OTU4_LOF`,`OTU5G_LOF`}. Already covered by our rate-suffix normalisation in `engine.py`. No catalog change. |
| Truly new in 9800 | **3** | `BP_ODU_LOFLOM`, `MEM_OVER`, `XCP_DEGRADE`. These should be added to our catalog when we extend coverage. |

### 4.3 The three truly-new alarms

#### 4.3.1 `BP_ODU_LOFLOM` (§1.2.4.6)

> "BP_ODU_LOFLOM indicates the loss of ODUk frames and multiframes in the **backplane direction** of an OTN line board. This alarm is reported when the frame header information of the source board and sink board (the line board that reports the alarm) does not match."

- Severity / type: **Critical / Equipment alarm**.
- Parameters: 18 hex bytes — board id (1–2), port number (4), channel type (5: ODU0/1/2/flex/3/4), timeslot id (6), 12 reserved bytes.
- What's *new*: 8800's ODU LOFLOM fires on **line-side** loss. `BP_ODU_LOFLOM` adds the **backplane-side** detection that exists on 9800's larger chassis where the cross-connect fabric is multi-stage. Our simulator's diamond+crossbar topology has no backplane fabric to model — this alarm is a candidate for a future "intra-NE fabric fault" fault id (not in our priority-10 catalogue).

#### 4.3.2 `MEM_OVER` (§1.2.4.28)

> "The MEM_OVER alarm indicates that the memory utilization is too high. This alarm occurs when a board detects that the memory utilization is higher than the threshold."

- Severity / type: **Major / Process alarm**.
- Parameters: threshold-crossing type (partition / physical system / virtual system / virtual process), process partition or PID.
- Causes (per the 9800 doc): (1) NE running too many alarm/PM tasks, (2) board carrying too much traffic.
- What's *new*: a **process-layer** alarm (not OTN, not equipment-hardware). It surfaces overload conditions on the embedded software. We don't currently model process-level state at all; this is the first alarm in the catalog that argues for adding a coarse "embedded software" layer to the KG.

#### 4.3.3 `XCP_DEGRADE` (§1.2.4.61)

> "Protection capability degrade indication of cross-connect boards. This alarm is reported when the external command that affects the automatic switching or isolation of cross-connect boards is delivered or the active or standby cross-connect board is faulty."

- Severity / type: **Major / Equipment alarm**.
- Parameters: the parameter encodes a state machine over `BP_ODU_LOFLOM` and `ODUk_LOFLOM` self-healing actions. **Read together with §4.3.1**: when multiple service boards report `ODUk_LOFLOM` or `BP_ODU_LOFLOM` simultaneously, the cross-connect-board self-healing logic isolates and switches; `XCP_DEGRADE` is the umbrella alarm that fires while that healing is in progress and the protection capacity is reduced.
- What's *new*: `XCP_DEGRADE` formalises a **fabric-level protection state** alarm — the controller's view of the chassis cross-connect health. Closest 8800 analogue is `APS_INDI` / `OLP_PS` but those are tunnel-level, not fabric-level.

### 4.4 Sample alarm walkthrough — `B1_EXC` (§1.2.4.1)

The very first entry in 1.2.4. It exemplifies the format:

```
1.2.4.1 B1_EXC

Description
  Excessive errors on the regenerator section (B1). Generated when the
  received SDH signals degrade and the B1 bit errors on the regenerator
  section exceed the threshold.

Attribute
  Alarm Severity:  Minor
  Alarm Type:      Service quality alarm

Parameters
  None

Impact on the System
  Affects the QoS of the service signals.

Fault Symptom
  None.

Possible Causes
  • Cause 1 (client side):  Input signals on the client side contain B1 bit errors.
  • Cause 2 (client side):  The board reporting the alarm is faulty.
  • Cause 1 (WDM side):     Receive side of local station (Station B) is faulty.
  • Cause 2 (WDM side):     Excessive attenuation; fiber/connector dirty.
  • Cause 3 (WDM side):     Transmit side of opposite station (Station A) is faulty.

Procedure
  • Check alarm details on NCE; record port and channel IDs.
  • For each cause: per-step handling (loopback, replace SFP, clean connector, …).
```

This is structurally identical to 8800 §3.1 `B1_EXC` (which I parsed in `data/alarms_detail.json`). The only differences: NCE replaces U2000 in the procedure text, and the bullet character is `•` not `l`.

## 5. The real-world ALM-CUR-DATA-ALL format (from `Library/Alm_format.txt`)

This is the wire format the 9800 NMS (NCE) emits when an operator queries the live alarm table. Worth documenting because (a) it's the NMS-level format the simulator's output should eventually conform to, and (b) it's a parseable text format we could ingest if we ever connect to a real NE.

### 5.1 Column spec

```
NUM       integer    NMS-assigned alarm number (sequence id)
EID       string     alarm name (e.g. MUT_LOS, OA_FLATNESS_ADJUSTMENT)
SEVERITY  enum       CR=Critical, MJ=Major, MN=Minor, WR=Warning
STATE     enum       start | end
START-TIME RFC 3339  alarm raised
END-TIME  RFC 3339   alarm cleared (None while still active)
SRVAFF    enum       SA=service-affecting, NSA=not-service-affecting
OBJ-TYPE  string     object class (LASER_GROUP, BOARD, PORT, OPS, ...)
PARA      string     comma/semicolon-separated key=value of the precise location
                     and 18-byte hex parameter blob
```

### 5.2 Sample rows (verbatim from `Alm_format.txt`)

```
272648  MUT_LOS                 CR  start  2025-12-26 07:22:27+00:00  None  SA  LASER_GROUP  board=13,subcard=255,port=16,group=1;;;00 ff 00 000d 00 00 ff ff;
357087  OA_FLATNESS_ADJUSTMENT  MN  start  2026-04-29 12:49:46+00:00  None  SA  LASER_GROUP  board=13,subcard=255,port=11,group=1;;;00 ff 00 00ac 00 00 ff ff;
357477  OA_FLATNESS_ADJUSTMENT  MN  start  2026-04-30 11:02:23+00:00  None  SA  LASER_GROUP  board=13,subcard=255,port=4,group=1;;;00 ff 00 00ac 00 00 ff ff;
```

### 5.3 What this tells us

- The NMS already encodes the location (board / subcard / port / group) in a structured PARA string — **machine-parseable**.
- The 18-byte hex blob is the per-alarm parameters (matches the format documented in §1.2.4.6 of the 9800 doc). For RCA we mostly only need the symbolic location; the hex blob can be ignored.
- Severity mapping: CR/MJ/MN/WR ⇄ our Critical/Major/Minor/Warning, no translation lost.
- `OA_FLATNESS_ADJUSTMENT` is *not* in our catalog (a 9800-specific OA alarm that fires when the EDFA gain-flatness adjustment is in progress). Worth adding to the catalog extension.

## 6. Implications for our project

| Concern | Recommendation |
|---|---|
| Should the alarm catalog be extended with 9800 alarms? | **Yes, optional second pass.** Run the parser on `Library/extracted/osn9800.txt` after a 1-line bullet-character fix. Output goes to `data/alarm_catalog_9800.json` — keep separate from the 8800 catalog so we can decide which platform we're simulating. |
| Should the suppression graph be extended? | **Eventually.** 9800 §1.2.2.9 has the same suppression-diagram structure as 8800 §1.7. The OTN/SDH chains are the same; the **Packet** signal-flow rules are entirely new and would require their own edges. Out of scope until we model Packet faults. |
| Should the simulator add a Packet domain? | **No, not yet.** The 9800 supports Ethernet/MPLS/PW but our YANG topology only models OTN. Adding Packet would mean a new network layer in `topology.json`, plus new fault rules. Park this until the OTN flow is proven end-to-end. |
| Should we ingest real-world ALM-CUR-DATA-ALL? | **Useful but not blocking.** A 30-line parser converts each row to our `Alarm` dataclass. Saves until we want to validate the simulator against a real network capture. |
| Should fault catalog (Design/05) get new fault ids from 9800? | **Three candidates.** `MEM_OVER` argues for a `process_overload` fault. `BP_ODU_LOFLOM` + `XCP_DEGRADE` together argue for a `cross_connect_fabric_fault` fault. Both are deferred — they don't fit the diamond+crossbar topology since we don't model embedded software state or multi-stage fabrics. |

## 7. Concrete action items (prioritised)

| # | Item | Effort |
|---|---|---|
| 1 | Patch `src/parser/parse_alarms.py` to accept both `l` and `•` as cause-bullet markers (the only change needed for the existing parser to work on 9800 text). | 1 line |
| 2 | Run the patched parser on `Library/extracted/osn9800.txt`; produce `data/alarms_index_9800.json` + `data/alarms_detail_9800.json`. Then merge into `data/alarm_catalog.json` with a `product` field per alarm (`8800` / `9800` / `both`). | ~1 hour |
| 3 | Add `BP_ODU_LOFLOM`, `MEM_OVER`, `XCP_DEGRADE`, `OA_FLATNESS_ADJUSTMENT` to `engine.DEFAULT_SEVERITY` so `engine.layer_of()` and severity-lookup don't return `Major / other` for them. | ~10 min |
| 4 | Document the ALM-CUR-DATA-ALL parsing path in `Design/02_simulator_design.md` §11 ("Out of scope, future") so the format is captured if we ever connect to a real NE. | ~20 min |
| 5 | (Optional, deferred) Add Packet network layer to YANG topology. | ~half-day |

## 8. Memory entry

The 9800 docs are referenced material — the existing 8800 alarm catalog and analysis remain the simulator's source of truth in v2. Treat any decision to ingest 9800 alarms as a deliberate scope expansion. Recorded in the project memory.
