# Lab Mesh Topology — `Mesh_network_V1130`

Status: **v3 — adopted (simplified).** What started as a deferred reference capture is now the active simulator topology. The full lab as captured here has 8 OMS line systems with 19 spans and ~11 active channels; v3 ships a **simplified subset**: 8 OMS line systems with **1 span each** (8 spans total), **6 OCh tunnels** `ch1`..`ch6`, 6 hubs A–F. Everything else in this document — calibration tables, OPM layout, full per-channel mapping — remains as reference for a future v4 expansion.

This document captures the analysis of the real Huawei DWDM lab testbed exported by you to `Library/Mesh_network_V1130.pdf` (and the source `Mesh_network_V1130.eddx`). The original §10–§11 "deferred / triggers for pivot" content is preserved below as a historical record of how the v3 cut-over was scoped.

**v3 is live.** Source of truth is `data/yang/{topology,tunnels,inventory}.json` (lab mesh, 8 spans, 6 channels). The pre-v3 4-NE diamond+crossbar is preserved at `data/yang/v2/` for regression. See `Design/02_simulator_design.md` §5 for the v3 ASCII diagram and `Design/05_fault_event_catalog.md` §8 for the OSN 9800 alarm-name inventory the simulator emits against this topology.

---

## 1. Source files

| File | Role |
|---|---|
| `Library/Mesh_network_V1130.eddx` | EdrawMax native diagram (vendor-internal proprietary format) — opened in EdrawMax, page 1 "mesh_f", page 2 "OMS2OMS". 864 + 427 shapes. |
| `Library/Mesh_network_V1130.pdf` | PDF export of the same diagram, two pages. |
| `Library/extracted/mesh_network/` | Unzipped Edraw XML (programmatic mining starting point if we ever build a converter). |
| `Library/extracted/mesh_network_text.txt` | All text labels extracted from the PDF — useful for sanity-check searches. |

---

## 2. Topology at a glance

A meshed DWDM core **larger and more detailed** than v2: 8 OMS line systems, ~4 production NEs, WSS-based ROADM at 6 hub points, 20-channel wavelength plan with ~11 active channels.

```
                  A ─── OMS2 ─── B
                  │              │
                OMS1            OMS5
                  │              │
                  C ─── OMS4 ─── D
                  │              │
                OMS8            OMS6        (newly built — OMS6/7/8 at NE_IP3,
                  │              │           fiber-length generalization)
                  F ─── OMS7 ─── E

   Original 5 OMS line systems (A-D core):
       OMS1 = A ↔ C   (3 spans)
       OMS2 = A ↔ B   (4 spans)
       OMS3 = B ↔ C   (3 spans)   ← shared by ch2 and ch4 (R4 demo basis)
       OMS4 = C ↔ D   (2 spans)
       OMS5 = B ↔ D   (3 spans)

   Newly-built 3 OMS line systems (each 1 span):
       OMS6 = D ↔ E   (40 km)
       OMS7 = E ↔ F   (60 km)
       OMS8 = F ↔ C   (80 km)  ← cross-mesh long-haul

   Plus an additional cross-connect-table (B and C internal):
       B: OMS2.DM3 ↔ OMS3.AM3,   OMS2.DM4 ↔ OMS5.AM4
       C: OMS3.DM3 ↔ OMS1.AM3,   OMS4.DM4 ↔ OMS1.AM4
```

(`AMx` = WSS Add port for channel x; `DMx` = WSS Drop port for channel x. The `↔` is bidirectional through the WSS pair.)

## 3. OMS line-system catalogue

| OMS id | Spans | Notes |
|---|---|---|
| OMS1 | 3 | original |
| OMS2 | 4 | original |
| OMS3 | 3 | original |
| OMS4 | 2 | original |
| OMS5 | 3 | original |
| **OMS6** | 1 (40 km) | **newly built** — D ↔ E |
| **OMS7** | 1 (60 km) | newly built — E ↔ F |
| **OMS8** | 1 (80 km) | newly built — F ↔ C (cross-mesh long-haul) |

Total: **19 OMS spans** across 8 OMS line systems.

## 4. Network elements

| NE id (lab name) | Management IP | Role / what it carries |
|---|---|---|
| `NE_IP1` | `70.31.65.95` | hub A; chassis with WSS at slot 13, ports 23..32 (Tx) and 23 (Rx) |
| `NE_IP2` | `70.31.65.96` | hub B; chassis with WSS |
| `NE_IP3` | `70.31.65.133` | hosts the new OMS6 / OMS7 / OMS8 segments |
| `NE[65.130]` | `70.31.65.130` | additional NE (only ports 1, 2, 3 referenced) |
| 8019 transponder shelf | (not labelled) | 10-channel — TX1..TX10 → WSS DM1..DM10 |
| 8018 transponder shelf | `70.31.65.91` | 2-channel — TX1, TX2 → WSS DM12, DM13 |
| 801x (page-2 reference) | unknown | TX/RX pair feeding DM11 loopback |
| **OSA** | `70.31.65.101` (port 109) | optical-spectrum analyser instrument |
| **Bridge / Opt. switch** | `70.31.66.3` | management bridge |

**4 production NEs + 2 (or 3) transponder shelves + 1 OSA + 1 management bridge.** Approximately **8 distinct logical equipment instances**, vs **4 NEs** in v2.

## 5. Hardware inventory

| Component | What | Quantity (approx) |
|---|---|---|
| OAU (Optical Amplifier Unit) | per-direction line amplifier | one per span direction |
| VOA | variable optical attenuator placed *before* every amplifier | one per OAU |
| WSS | wavelength-selective switch at hub nodes (Tx side: WSS Bid 13, ports 23..35; Rx side: WSS Bid 23, ports 3..15) | per hub |
| **C120 OPM** | optical-power monitor — 8 instruments distributed across IP1/IP2/IP3 | OPM1..OPM8, plus OPM_OMS5 |
| OA models in use | 25S × 24, 21S × 2 (currently). Roadmap: 14S, 21S, 32S, 25H, 32H for "OA generalization" | mixed |
| ASE source | amplified-spontaneous-emission source filling unused channels | one per OMS |
| Coupler | monitoring tap before OPM | one per measurement point |
| 8019 transponder | TX/RX 10-channel | 1 |
| 8018 transponder | TX/RX 2-channel | 1 |
| Dummy DL module | direct-into-WSS fake light source | several |

## 6. Wavelength plan

- Each OMS supports **up to 20 channels** (`AM1..AM20` / `DM1..DM20`).
- **~11 channels are in active use** per OMS in the current run.
- Remaining channels are filled with **ASE sources** for stable per-channel power.
- **AM11 / DM11 is internal loopback**: Tx WSS DM11 → Rx WSS AM11 (used for self-test / diagnostics).
- Channel-to-port mapping is given in the Tx_Bid / Tx_Port / Tx_WSS_Bid / Tx_WSS_Port table on page 1; the matching Rx table assigns Rx_WSS_Port 3..15 (so 13 channels active in the captured snapshot).

The 8019 transponder feeds **TX1..TX10** to **WSS Tx ports 1..10** (which become AM1..AM10 of the OMS). The 8018 transponder feeds **TX1..TX2** to **WSS Tx ports 34, 35** (becoming AM12, AM13). The full per-channel routing table is on the diagram.

## 6.1 Why the lab was rebuilt (Chinese annotations on page 1)

The diagram annotations record the lab's modification history and goals (paraphrased from the Chinese on page 1):

**Goals of this round of changes:**
1. Add DL modules plugged directly into WSS (as fake light sources).
2. Build 3 new OMS segments — varying fiber-span count and length for **generalization**.
3. Replace OA modules — 27U → 25S/H, prefer 21S/25S/32S deployment.
4. Add a VOA before the first fiber of each OMS (and after the last) to simulate ODF insertion loss.
5. Calibrate the actual insertion loss between every OA's `out` port and the long-fiber's true `in` port — used as the PPE/IR validation reference.

**Actual implementation:**
- 3 new OMS segments at 1 span each: 80 km, 60 km, 40 km.
- 27U OAs all replaced — 25S × 24, 21S × 2.
- VOA before first and last span of each OMS.
- Per-OA insertion-loss calibration recorded on the diagram.

**Future (planned):**
- Buy 5 more C120 OPMs (currently borrowed).
- Procure 14S, 21S, 32S, 25H, 32H OAs for true OA generalization.
- Stockpile long fibers and WSSes.

This tells us the lab is being deliberately built as a **generalization** environment — varying span count, span length, OA model, and channel count so test cases cover a broad envelope. That matches the goal of training an ML model that doesn't overfit to one configuration.

## 7. Optical-power calibration data

Page 2 has 8 per-OMS measurement tables (one per OMS) of the form:

```
PD       OAout    linein         (units: dBm)
6.7      6.6      5.4
6.8      6.8      3.6
7.1      6.8      5.5
7.4      7.5      6.0
...
```

- `PD` — photodetector reading at the OA tap
- `OAout` — measured power at the OA's output port
- `linein` — measured power at the *true* fiber input (after VOA)
- The (OAout − linein) gap is the calibrated insertion loss the lab quotes for that span

The diagram also records discrete VOA settings inline (`1.2dB` annotations on individual VOAs). These are baseline-condition values — when we eventually inject a fault like `fiber_attenuation_high` we compare against this baseline.

## 8. Comparison with v2 (diamond+crossbar)

| | **v2 simulator topology** | **Lab Mesh_V1130** |
|---|---|---|
| NEs | 4 (`ne1`..`ne4`) | ~4 production + 2 transponders + 1 OSA + 1 mgmt switch |
| OMS line systems | abstract (1 link = 1 hop) | 8 OMS line systems, **19 spans total** |
| Hubs / ROADM | none | 6 WSS hubs (A, B, C, D, E, F) |
| Service paths | fixed (one ODU per service) | mesh; multiple paths per channel are possible |
| Channels | 2 services on 2 wavelengths | up to 20 per OMS, ~11 in active use |
| Span lengths | abstract distance | concrete (40, 60, 80 km calibrated) |
| OA modelling | abstract (one OA per span) | typed (25S, 21S, 27U, 32S, 25H, 32H) |
| Optical-power baseline | none | full PD / OAout / linein dBm tables |
| Real management IPs | none | yes (`70.31.65.95`, `.96`, `.133`, `.130`, `.91`, `.101`) |
| Useful for ML training? | yes — class balance, controlled cascades | yes — realism, span/OA generalization |
| Useful for system validation? | yes — fast iteration | yes — true-to-life cascade fingerprints |

The lab is **roughly 5× larger and 10× more detailed** than v2.

## 9. v3 pivot plan — what changes when we adopt this as the simulator topology

This section is the **explicit instruction set** for the eventual v3 pivot. Each block lists what changes and the expected effort.

### 9.1 YANG instance files (Phase A redo)

Replace `data/yang/{topology,tunnels,inventory}.json` with versions describing the lab.

| File | Changes |
|---|---|
| `topology.json` (`/ietf-network:networks`) | • optical-topo gains 8 new networks-or-19-links (one per OMS span)<br>• otn-topo OTU pipes scale up to ~30 (one per active channel × span)<br>• Add `wson-node:is-reconfigurable-node = true` for the WSS hub NEs |
| `tunnels.json` (`/ietf-te:te`) | • ~11 OCh tunnels (one per active channel) routed through the WSS hubs<br>• ~11 ODU tunnels with `nfms:client-service-name = "ch_<n>"` |
| `inventory.json` (`/ietf-network-hardware-inventory`) | • inventory per board type: WSS, OAU (with model 25S/21S/etc.), VOA, OPM, ASE source, transponder shelves<br>• real `serial-num`s and `mfg-name = "Huawei"` values |

Effort: ~1 sitting (data entry) plus ~half a sitting validating leafrefs.

### 9.2 Simulator faults (Design/05 catalogue update)

The lab introduces failure modes our v2 fault catalogue doesn't cover:

| New fault id | What it is | Where it'd fit in our priority-set |
|---|---|---|
| `voa_misset` | VOA driver pegged at wrong attenuation → IN_PWR_HIGH or LOW downstream | Bucket B (optical-power & laser) |
| `wss_xc_failure` | WSS cross-connect refuses to bridge a wavelength | new bucket E (ROADM-control) |
| `ase_source_off` | ASE source drops out → channel-balance shifts | Bucket B |
| `transponder_card_swap` | TX1..TX10 relinked through different DM ports | Bucket C (equipment) |
| `oa_model_wrong` | wrong OA model in slot → gain target unmet | Bucket C (config-mismatch sub-class) |

Effort: ~1 sitting per fault, roughly 5 sittings total.

### 9.3 GUI (`src/app/static/`)

The hand-laid `DIAMOND_POS` in `server.py` becomes a dictionary of ~8 NE positions for the lab. The vis-network canvas grows; we'll need to add edge-bundling (currently every span is drawn straight which would clutter at 19 spans). vis-network supports `smooth.curvedCW/CCW` per edge — we already use it.

### 9.4 KG schema (Design/04)

**No change** to the schema — every entity type and predicate we listed (`Alarm`, `OChTunnel`, `Span`, `Connected`, `CarriedBy`, etc.) applies unchanged. The KG just gets bigger.

### 9.5 ML pipeline

**No change** to algorithm or training code, but two beneficial side-effects:

- **Class balance improves**: with 19 spans, `fiber_cut` has 19 valid targets vs 6 in v2 — natural class-size growth.
- **Within-class diversity improves**: every channel that traverses a cut span produces its own cascade. The model learns to be invariant to channel index, which is what we want for production transferability.

### 9.6 Data already produced is **not** wasted

`data/incidents.db` from the v2 run is still useful as a **smaller, simpler regression set**. When we pivot, we'd:

1. Rename it `data/incidents_v2.db`.
2. Run the generator on the new topology to fill `data/incidents.db`.
3. The KG-builder (Phase C of `Design/04`) becomes the first place where **both** datasets get consumed — v2 as smoke test, v3 as the real training set.

## 10. What we are NOT doing now (and the reasons)

| Decision | Why |
|---|---|
| Don't rewrite YANG instances yet | v2 is producing useful data; rewriting before the KG-builder is built means we lose the ability to test the KG with a known-simple input. Build KG against v2, *then* pivot. |
| Don't extend the fault catalogue with WSS / VOA faults yet | Those faults need new alarm types in the catalog (some come from `OSN 9800` analysis in Design/06 — `OA_FLATNESS_ADJUSTMENT`, `XCP_DEGRADE`). Defer to the v3 pivot. |
| Don't touch the GUI's hand-laid positions | Same reason — they get redone wholesale in the v3 pass, not piecemeal. |
| Don't write a `.eddx` ↔ YANG converter | One-off conversion; manual translation following this document is faster than learning Edraw's undocumented schema. |

## 11. Trigger conditions for the v3 pivot

This is when we should switch from "deferred" to "doing it":

1. **KG-builder is working** end-to-end on v2 data (Phase C of `Design/04` complete).
2. **Rule engine** (Phase D of `Design/04`) is producing R1..R4 outputs that match what we'd expect.
3. **At least one ML baseline** trained and evaluated on v2 data — so we have a metric to compare v3 against.

Once those three are in place, do the v3 pivot in one focused iteration. Estimated **3–5 days of work** end-to-end (YANG redo + faults extension + GUI repositioning + dataset regeneration + KG rebuild).

## 12. Memory entry

The v3 pivot is **scheduled but not started**. Source-of-truth topology in v2 remains the diamond+crossbar. This document is the v3 specification — every change required by the pivot is captured in §9.
