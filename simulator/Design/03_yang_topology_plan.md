# YANG Topology Model — Design Plan

Status: **v3 — implemented.** Originally written as a forward-looking proposal; the v3 simulator now uses the schema described here. Source-of-truth instance files live at `data/yang/{topology,tunnels,inventory}.json` (lab mesh: 6 hubs A–F, 8 OMS spans, 6 OCh tunnels). §3 / §4 are written against the v3 lab mesh; the v2 4-NE diamond+crossbar baseline is preserved at `data/yang/v2/` for regression (no longer inlined here — see git history for the prior v2 walkthrough). See `Design/07_lab_mesh_topology.md` for the v3 lab capture and `Design/02_simulator_design.md` §5 for the v3 ASCII diagram.

Sources: the seven references in `Library/YANG_Topology_Links.txt`.

---

## 1. What the IETF Stack Gives Us

The seven references compose into a layered, multi-network model, all rooted at **RFC 8345** (`ietf-network` + `ietf-network-topology`). The diagram below shows how each module sits relative to the others; arrows mean *augments / depends on*.

```
                        ┌─ ietf-network                 (RFC 8345)
                        ├─ ietf-network-topology        (RFC 8345)
                        │     ▲
                        │     │ augments
                        │  ietf-te-topology             (RFC 8795)        ← TE
                        │     ▲       ▲
                        │     │       │ augments
                        │  ietf-otn-topology  ietf-wson-topology
                        │  (otn-topo-yang)    (RFC 9094)                 ← OTN, optical
                        │
                        ├─ ietf-te                       (base TE tunnel)
                        │     ▲       ▲
                        │     │       │ augments
                        │  ietf-otn-tunnel    ietf-wdm-tunnel             ← tunnels
                        │
                        ├─ ietf-eth-tran-service / ietf-trans-client-service ← client services
                        │
                        └─ ietf-network-hardware-inventory                ← NEs/boards/ports
                              + ietf-hw-inventory-ref-topo               ← cross-ref to topo TPs
```

Key things we get for free:

- **Multi-layer topology.** A node or link in a "higher" topology references a node or link in a "lower" topology via `supporting-node` / `supporting-link` / `supporting-tp`. This is exactly what we need for correlation rules R2 (OTU↔OCH) and R4 (OCH paths sharing OTS) in `Library/Alarm correlation logical rules.txt`.
- **Termination points (TPs).** A node has TPs (= ports). Higher-layer TPs reference lower-layer TPs. Inventory ports cross-reference topology TPs via `inventory-id`.
- **Tunnels.** OTN tunnels (ODU containers) and WDM tunnels (OCh / OTSi carriers). Tunnels own explicit routes through the underlying topology — the per-hop link list we need to evaluate R4.
- **Client-service tag on tunnels.** Rule R3 ("two ODUs are underlay segments of the same client service") is realised by attaching an `nfms:client-service-name` leaf to each ODU tunnel; two tunnels share a service iff the names match. We do **not** carry a separate IETF client-service module.

## 2. Which Modules We Import vs. Define

### 2.1 Import (consume as-is)

| Module | Use in our simulator |
|---|---|
| `ietf-network`, `ietf-network-topology` (RFC 8345) | base node/link/TP for every layer |
| `ietf-te-topology` (RFC 8795) | TE features on top — node/link admin-status, LTPs, TTPs, multi-layer underlay |
| `ietf-wson-topology` (RFC 9094) | optical layer: ROADM vs FOADM, wavelength labels, line-side TPs |
| `ietf-otn-topology` (draft-ccamp-otn-topo-yang) | OTN layer: ODU rates, supported client signals, tributary slot granularity |
| `ietf-te` (base TE tunnel) | tunnel root container at `/te:te/te:tunnels` |
| `ietf-otn-tunnel` (draft-ccamp-otn-tunnel-model) | ODU tunnels carrying our SDH services |
| `ietf-wdm-tunnel` (draft-ccamp-wdm-tunnel-yang) | OCh tunnels carrying OTU framing |
| `ietf-network-hardware-inventory` (draft-ccamp-network-inventory-yang) | NE chassis → boards (OTU/OA/FIU/SCC/FAN) → ports |
| `ietf-hw-inventory-ref-topo` | cross-link: inventory port ↔ topology TP |
| `ietf-layer0-types`, `ietf-layer1-types` | grid frequencies, ODU rate identities |

The Ethernet client-service module (`ietf-eth-tran-service`) is intentionally **excluded** — our clients are SDH STM-64, not Ethernet. We also do not bring in `ietf-trans-client-service`; the small amount of "shared-service" semantics R3 needs is handled by an `nfms:client-service-name` tag on each tunnel (see §2.2).

### 2.2 Define (project-specific augmentation)

We define **one small project module** of our own:

- **`network-fault-mgmt-state`** *(prefix `nfms`)*. Three augmentation points:
  - on `ietf-network-hardware-inventory`:
    - chassis / board (any class): `power-state` (on/off), `fan-state` (ok/failed), `fiber-attenuation-extra-dB` for soft-fault bit-error injection.
    - **OTU module (v3+, for PPE):** `ppe-enabled`, `ppe-delta-z-m`, `ppe-poll-interval-ms`, `dbp-noise-sigma-db`, `dbp-noise-sigma-per-km-db`.
    - **OA module (v3+, for PPE):** `edfa-gain-db`, `edfa-noise-figure-db`, `edfa-z-km`.
  - on `ietf-te:tunnels/tunnel`: `client-service-name` (string) — the tag that lets two ODU tunnels declare they belong to the same client service (R3). Optionally on each OCh tunnel, `ppe-override` to disable PPE per-channel.
  - **on `ietf-network-topology:link` (v3+, for PPE):** nested inside `ietf-te-topology:te/te-link-attributes` — `length-km` (required for PPE), `fibre-type`, `alpha-db-per-km`, `connector-losses` (list of `{z-km, loss-db}`). Today `length-km` is held only in the free-text `name` and JSON comments; PPE turns it into a structured leaf.

This is the only project-specific YANG; everything else is standard, so any future YANG-aware controller can ingest the topology unchanged.

### 2.3 PPE-related augmentations — worked example (v3+)

Power Profile Estimation (`Design/12_ppe_simulation.md`) needs a small additional set of `nfms` fields on top of the v1 augmentations. The canonical reference is `Design/12 §6.1`; the **minimum viable set** is two fields:

1. `nfms:length-km` on every span link in `topology.json` — currently held in free-text comments (`"OMS6 (D->E, 40 km)"`), needs to become a structured leaf.
2. `nfms:ppe-enabled` on every OTU module in `inventory.json` — feature gate.

A worked example on `span_OMS3_BC`:

```jsonc
{
  "link-id": "span_OMS3_BC",
  "source":      { "source-node": "B", "source-tp": "fiu-to-C" },
  "destination": { "dest-node":   "C", "dest-tp":   "fiu-from-B" },
  "ietf-te-topology:te": {
    "te-link-attributes": {
      "name": "OMS3 (B->C, 60km)",
      "admin-status": "up",
      "nfms:length-km":         60.0,
      "nfms:fibre-type":        "SMF-G.652",
      "nfms:alpha-db-per-km":   0.20,
      "nfms:connector-losses": [
        { "z-km": 0.0,  "loss-db": 0.3 },
        { "z-km": 60.0, "loss-db": 0.3 }
      ]
    }
  }
}
```

Launch power is **not** added here — `tx-channel-power` already lives on each OCh tunnel's `ietf-wdm-tunnel:wdm-constraint` in `tunnels.json` and is reused as `P_launch_dBm`. Per-board PPE settings on OTU and OA modules in `inventory.json` are documented in `Design/12 §6.1.3`.

## 3. Topology Composition for the v3 Lab Mesh

The v3 simulator runs against the **6-hub lab mesh** captured in `Design/07_lab_mesh_topology.md`: six WSS-based ROADM hubs (A..F), eight OMS line systems each modelled as a forward+reverse pair (16 directed optical-topo links), and six OCh tunnels `ch1..ch6`:

```
                  A ─── OMS2 ─── B
                  │              │
                OMS1            OMS5
                  │              │
                  C ─── OMS4 ─── D
                  │              │
                OMS8            OMS6
                  │              │
                  F ─── OMS7 ─── E

   ch1 (AC_direct):   A ─OMS1→ C
   ch2 (AC_via_B):    A ─OMS2→ B ─OMS3→ C
   ch3 (DC_direct):   D ─OMS4→ C
   ch4 (DC_via_B):    D ─OMS5→ B ─OMS3→ C    ← shares OMS3 with ch2 (R4 demo)
   ch5 (DF_2hop):     D ─OMS6→ E ─OMS7→ F
   ch6 (CF_direct):   C ─OMS8→ F
```

Why this shape: `ch2` and `ch4` both include the directed span `span_OMS3_BC` in their explicit-route hop lists. Cutting that span brings down both channels simultaneously; each produces its own alarm cascade, but they share the same physical root cause. This is the canonical R4 demo (OCH paths sharing an OTS link). v3 also exercises the 4-hop reach (`ch5`) and the long-haul cross-mesh path (`ch6`).

The model uses three stacked TE topologies plus inventory plus tunnels (with the `nfms:client-service-name` tag on tunnels in lieu of a separate services root):

```
┌──────────────────────────────────────────────────────────────────────┐
│  Tunnels  (root /ietf-te:te/tunnels)                                 │
│  ▸ OCh tunnels (wdm-tunnel):  och_ch1 .. och_ch6                     │
│  ▸ ODU tunnels (otn-tunnel):  odu_ch1 .. odu_ch6                     │
│       each ODU tunnel carries  nfms:client-service-name              │
└──────────────────────────────────────────────────────────────────────┘
          │ tunnel ↔ topology link via te:underlay
┌──────────────────────────────────────────────────────────────────────┐
│  Network "otn-topo"   (otn-topology)        (TE + OTN augments)      │
│  ▸ Nodes:   A, B, C, D, E, F   (all carry OTU/ODU function)          │
│  ▸ Links:   otu_OMS{n}_{xy}   (one OTU pipe per directed span)       │
│  ▸ TPs:     OTU client-side + line-side ports                        │
└──────────────────────────────────────────────────────────────────────┘
          │ supporting-link / supporting-node / supporting-tp
┌──────────────────────────────────────────────────────────────────────┐
│  Network "optical-topo"  (wson-topology)    (TE + WSON augments)     │
│  ▸ Nodes:   A, B, C, D, E, F  (is-reconfigurable-node = true at all) │
│  ▸ Links:   span_OMS{n}_{xy}  (16 directed = 8 OMS × 2 directions)   │
│  ▸ TPs:     FIU line ports, OA in/out                                │
└──────────────────────────────────────────────────────────────────────┘
          │ inventory-ref-topo  (ports → topology TPs)
┌──────────────────────────────────────────────────────────────────────┐
│  Network-hardware-inventory                                          │
│  • equipment-room → rack → 6 chassis (one per hub A..F)              │
│  • chassis → modules (WSS, OTU, OA, FIU, SCC, FAN)                   │
│  • modules → ports                                                   │
└──────────────────────────────────────────────────────────────────────┘
```

Notes:

- We deliberately **separate optical and OTN topologies** (instead of conflating them) so the layered relationships our correlation rules require are explicit: `otu_OMS3_BC`'s `supporting-link` is `span_OMS3_BC`, etc.
- All v3 hubs carry `is-reconfigurable-node = true` because each chassis hosts a WSS module. If a future variant adds non-reconfigurable in-line amplifier sites, those nodes flip to `false`.
- The `ietf-wson-topology` model intentionally does **not** carry span loss / fiber type / impairments. v1 emitted bit-error alarms heuristically. **v3** introduces a principled physics model for **Power Profile Estimation** (`Design/12_ppe_simulation.md`); the `nfms` augmentation on each span link now carries `length-km` (required), and optional `fibre-type`, `alpha-db-per-km`, `connector-losses`. The original `fiber-attenuation-extra-dB` on inventory boards is preserved as the per-board attenuation injector for soft-fault incidents. See §2.3 for the worked example.
- The OTN topology model focuses on the **ODU layer**. The OTU "pipe" between adjacent NEs is represented as a *link* in this topology; OTU framing details live in the WDM-tunnel that carries the OTU.

## 4. Where the Correlation Rules Land

Our project's four weighted correlation rules map onto YANG concepts as follows:

| Rule | YANG-level realisation |
|---|---|
| **R1 temporal** | not topology-related; runs on alarm timestamps in the database. |
| **R2 OTU↔OCH** | given alarm A on an OTU TP and alarm B on an OCh tunnel: walk `supporting-tunnel` from the OTN link the OTU TP belongs to → if that supporting tunnel is the same OCh tunnel B mentions, rule fires. |
| **R3 ODU↔ODU shared client** | each ODU tunnel carries `nfms:client-service-name`; two ODU tunnels share a client iff their tag values match. In v3 every ODU tunnel `odu_ch1`..`odu_ch6` has a unique service tag (one channel per service), so R3 does not fire on the default setup — the rule is retained for future multi-segment client services that span two ODU tunnels. |
| **R4 OCH↔OCH shared OTS** | intersect the explicit-route hop lists of the two WDM tunnels on the optical topology — non-empty intersection ⇒ rule fires. In v3, `och_ch2` and `och_ch4` both include `span_OMS3_BC` in their ERO; cutting that span triggers R4 between the OCh-layer alarms generated by ch2 and ch4. |

All four are **graph queries on the layered YANG instance**. No additional schema is needed.

## 5. How We Will Store and Load It

YANG defines the *schema*. Instances of YANG data can be encoded as XML (RFC 6020/7950) or **JSON (RFC 7951)**. We will use **JSON encoding** — same shape, easier for the simulator to load and for the GUI to render.

There are **three YANG roots** in our stack, so at minimum **three instance files**:

```
data/yang/
├── topology.json     ← /ietf-network:networks
│                       ▸ network "optical-topo"   (wson-topology)
│                       ▸ network "otn-topo"       (otn-topology)
│                       ▸ inventory-ref-topo augment on TPs
├── tunnels.json      ← /ietf-te:te
│                       ▸ /te/tunnels: OCh tunnels (wdm-tunnel)   och_ch1 .. och_ch6
│                       ▸ /te/tunnels: ODU tunnels (otn-tunnel)   odu_ch1 .. odu_ch6
│                       ▸ each ODU tunnel carries  nfms:client-service-name
└── inventory.json    ← /ietf-network-hardware-inventory
                        ▸ NEs → chassis → boards → ports
                        ▸ nfms-state on each chassis/board (power, fan)
```

We keep all topology layers in one file and all tunnels in another because they share a YANG root — splitting them across files would just mean the loader has to merge them back into the same root tree. (We can split later if any of the three files becomes hard to read by hand.)

Loading approach:

- **Pure-Python JSON load**, no YANG library dependency at runtime. We trust the JSON shape because we wrote it; the YANG modules document the *structure* but we don't need runtime validation in v1.
- **Optional offline validation** with `pyang` on the YANG modules + `yanglint` on our JSON — done as a CI check, not at runtime. If we get the shape wrong, the validator catches it before any code runs.
- A small `src/simulator/yang_loader.py` translates the three JSON files into the same in-memory `Topology` objects already drafted in `src/simulator/topology.py`. The rest of the simulator (faults, engine, GUI) is unaffected.

Why not pyangbind / YDK: they generate code from YANG modules, which is overkill for what is essentially a small static dataset. We can adopt them later if/when we want strict schema conformance at runtime.

## 6. Implementation Phases

| Phase | Deliverable | Estimate |
|---|---|---|
| **A** | Hand-write the **three** `data/yang/*.json` files (`topology.json`, `tunnels.json`, `inventory.json`) for the v3 lab mesh. No code yet. | 1 sitting |
| **B** | Validate offline with `pyang` + `yanglint` against the IETF modules; iterate until clean. | ~1 sitting |
| **C** | Write `src/simulator/yang_loader.py` to load the three JSONs into `Topology`. Retire `data/topology.json`. | small |
| **D** | Resume the original simulator plan (faults → engine → DB → GUI), now with the layered topology accessible to the engine. | per `02_simulator_design.md` |
| **E** *(optional, later)* | Author one project-specific YANG file `network-fault-mgmt-state.yang` for the `nfms` augmentation (formal source rather than just JSON convention); wire it into yang-loader. | small |

## 7. Why This Path Is Worth It

- **Correctness.** Our correlation rules are explicitly about layered structure; the YANG models give us that structure for free, with semantics matching the project notes.
- **Reusability.** Anyone who later wants to consume our incidents from a real OTN controller can ingest the topology as-is, because we're conforming to ratified IETF schemas.
- **Discipline.** Writing the topology against a published schema forces us to think in the same units the operator world thinks in — `OTUk` containers, `ODU2/4/Cn` rates, `OCh` tunnels, `OTS` spans — instead of inventing local terminology.

## 8. Open Questions for Review

1. **Scope of optical layer.** WSON (RFC 9094) is fixed-grid and impairment-free. v3 stays with WSON (fine for the lab mesh's fixed-wavelength assignment) plus the `nfms` augmentations of §2.3 for PPE-relevant physics. If we ever want flexi-grid super-channels or full coherent transponder modeling, we'll need to import the flexi-grid impairment-aware topology drafts.
2. **OTN topology only models ODU.** "OTU between NEs" is represented as a *link* in the OTN topology rather than its own layer. This matches the spec but is one extra mental indirection — flagged in case a future contributor expects a separate OTU layer.
3. **JSON instances vs. fully YANG-bound Python.** Current choice (A): JSON instances loaded as plain dicts, validated offline with `pyang`. (B) `pyangbind`-generated Python classes remain available if we want strict runtime conformance later.

Phases A–D are complete in the v3 lab-mesh codebase (`data/yang/`, `src/simulator/yang_loader.py`, simulator + GUI all running). Phase E (formal `network-fault-mgmt-state.yang` module) remains optional and is the natural pickup point if the `nfms` augmentation surface grows further beyond §2.2 / §2.3.
