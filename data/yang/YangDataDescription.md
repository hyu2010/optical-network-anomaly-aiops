# `data/yang/` — YANG Data Description

> **v3 update (2026-05-04)** — the current YANG instance files describe the **lab mesh topology** per `Design/07_lab_mesh_topology.md`: 6 WSS hubs (A..F), 8 OMS line systems (5 main + 3 newly-built at 40/60/80 km), 5 channels with diverse routings (ch2 and ch4 share span `span_OMS3_BC` — basis for the R4 demo).
> The previous v2 (4-NE diamond+crossbar) instance files are archived at `data/yang/v2/` and stay valid as a smaller smoke-test set. The schema description below is unchanged — only the specific node / link / tunnel **values** differ between v2 and v3. Numbers in §8 are updated for v3.

A walk-through of every meaningful key in the three YANG instance files, what it means, where it comes from in the IETF YANG stack, and what value it can take. Reading this side-by-side with the JSON files should answer "what does *this* line do?"

Phase A artefact for `Design/02_simulator_design.md` and `Design/03_yang_topology_plan.md`.

---

## 0. Read this first — three RFC 7951 conventions you'll see everywhere

### 0.1 Module-prefixed keys (`module-name:leaf-name`)

In RFC 7951 (the JSON encoding of YANG), a key is **prefixed with its defining module name** when the key is added to its parent by a *different* module from the one that defined the parent. So:

- A `node` lives directly inside `network` (both in `ietf-network`) — no prefix needed.
- A `link` is added to `network` by `ietf-network-topology` (a different module) — must appear as **`ietf-network-topology:link`**.
- A `te` container is added to `node` by `ietf-te-topology` — appears as **`ietf-te-topology:te`**.

This is why so many of our keys look like `module:thing` — they are augmentations from a different module than the parent. **The prefix is the only signal of "where did this attribute come from".**

The example you flagged, `ietf-hw-inventory-ref-topo:inventory-id`, is the canonical case: it's a leaf added to every termination-point by the inventory cross-reference module.

### 0.2 Leafrefs

Many fields don't store a value — they **point at another node** by its key. RFC 7951 doesn't mark them visually; they just look like ordinary leaves. Examples:

| Leaf | Points at | Resolution |
|---|---|---|
| `network-ref` (in `supporting-network`) | a `network-id` value | `/networks/network[network-id=…]` |
| `node-ref` (in `supporting-node`) | a `node-id` value | `/networks/network[…]/node[node-id=…]` |
| `link-ref` (in `supporting-link`) | a `link-id` value | `/networks/network[…]/link[link-id=…]` |
| `inventory-id` (in TPs) | a component `uuid` value | `/network-hardware-inventory/network-elements/network-element/components/component[uuid=…]` |
| `tunnel-ref` (in `supporting-tunnel`) | a tunnel `name` value | `/te/tunnels/tunnel[name=…]` |
| `chassis-ref` (in `contained-chassis`) | a chassis component `uuid` | same target as `inventory-id` |
| `child-uuid` (in `contained-child-component`) | another component `uuid` | same target as above |

If a leafref doesn't resolve, the instance is invalid. We verify all our cross-references resolve (see §6).

### 0.3 `_comment` keys

`_comment` is **not** a YANG concept. It's plain JSON noise. Standard RFC 7951 readers ignore unknown keys, so it's safe at runtime. Strict validators (`yanglint --strict`) will warn — strip these before formal validation.

---

## 1. `topology.json` — root: `/ietf-network:networks`

### 1.1 Top-level

```jsonc
{
  "ietf-network:networks": {        // root container, RFC 8345
    "network": [ … ]                // list of stacked layered topologies
  }
}
```

| Key | From | Type | Meaning |
|---|---|---|---|
| `ietf-network:networks` | RFC 8345 `ietf-network` | container | YANG root for all topology data; required namespace prefix because we use module-qualified top-level. |
| `network` | RFC 8345 | list | One entry per layered topology; in our file there are two — `optical-topo` and `otn-topo`. |

### 1.2 Each `network` entry

| Key | From | Type | Meaning |
|---|---|---|---|
| `network-id` | RFC 8345 | string (key) | The unique id of this layer. We use `optical-topo` and `otn-topo`. |
| `network-types` | RFC 8345 | container | Marks what *kind* of network this is. Empty containers under it are presence flags. |
| `network-types/ietf-te-topology:te-topology` | RFC 8795 | empty container | Indicates this is a TE topology. |
| `network-types/ietf-te-topology:te-topology/ietf-wson-topology:wson-topology` | RFC 9094 | empty container | Indicates this TE topology is at the WSON (optical) layer. |
| `network-types/ietf-te-topology:te-topology/ietf-otn-topology:otn-topology` | OTN-topo draft | empty container | Indicates this TE topology is at the OTN layer. |
| `supporting-network` | RFC 8345 | list | Points at *lower* layers this network sits over. The OTN topology has `[{ "network-ref": "optical-topo" }]`. The optical topology has none — it's at the bottom. |
| `node` | RFC 8345 | list | Network nodes (NEs) in this layer. |
| `ietf-network-topology:link` | RFC 8345 | list | Directed links between nodes. Module-prefixed because added to `network` by `ietf-network-topology`. |

### 1.3 Each `node` entry

| Key | From | Type | Meaning |
|---|---|---|---|
| `node-id` | RFC 8345 | string (key) | Unique id of this NE inside its network. We use `ne1`/`ne2`/`ne3`/`ne4`. |
| `supporting-node` | RFC 8345 | list of `{network-ref, node-ref}` | Identifies the corresponding node in lower layers. In the OTN topology, every node has `[{ "network-ref": "optical-topo", "node-ref": "ne1" }]` to bind the OTN view of `ne1` to its optical view. |
| `ietf-te-topology:te` | RFC 8795 | container | TE-specific attributes on this node. |
| `ietf-te-topology:te/te-node-id` | RFC 8795 | te-types `te-node-id` (IPv4-like) | A TE-routing identifier. We use `0.0.0.<n>` placeholder. |
| `ietf-te-topology:te/te-node-attributes` | RFC 8795 | container | Holds further node attributes (and is the augmentation point for OTN/WSON). |
| `te-node-attributes/ietf-wson-topology:wson-node` | RFC 9094 | container | WSON augmentation present on optical nodes. |
| `wson-node/is-reconfigurable-node` | RFC 9094 | boolean | `true` = ROADM (can switch wavelengths), `false` = FOADM (fixed). v1: all `false`. |
| `te-node-attributes/ietf-otn-topology:otn-node` | OTN-topo | empty container | Marks this node as having OTN function. |
| `ietf-network-topology:termination-point` | RFC 8345 | list | TPs (ports / endpoints) on this node. Module-prefixed because added to `node` by `ietf-network-topology`. |

### 1.4 Each `termination-point` entry (= a port)

| Key | From | Type | Meaning |
|---|---|---|---|
| `tp-id` | RFC 8345 | string (key) | TP identifier inside this node. We use names like `fiu-tx-to-ne2`, `otu-line-from-ne1-svcA`. |
| `ietf-te-topology:te` | RFC 8795 | container | TE attributes on this TP. We use it as the augmentation point. |
| `ietf-te-topology:te/ietf-otn-topology:client-svc` | OTN-topo | container | OTN augmentation on a TP indicating client-signal capability. |
| `client-svc/supported-client-signal` | OTN-topo | leaf-list of identityref | Which client signal types this TP can carry. v1: `ietf-layer1-types:client-stm-64`. |
| **`ietf-hw-inventory-ref-topo:inventory-id`** | inventory-ref-topo | **leafref** to a component `uuid` in `inventory.json` | **The cross-reference between the topology view of a port (the abstract TP) and its physical inventory record.** This is the answer to the line you highlighted: it lets a tool walk from "alarm reported on this TP" to "this is the physical port on this board on this NE". |

### 1.5 Each `link` entry

| Key | From | Type | Meaning |
|---|---|---|---|
| `link-id` | RFC 8345 | string (key) | Unique link id inside this network. Examples: `span_12`, `otu-23-a`. |
| `source` / `destination` | RFC 8345 | container of two leafrefs | Identifies the link endpoints by `(node, tp)` pair. Each contains `source-node`+`source-tp` (or `dest-node`+`dest-tp`). All four are leafrefs. |
| `supporting-link` | RFC 8345 | list of `{network-ref, link-ref}` | Identifies the corresponding link in a lower layer. Each OTU pipe in `otn-topo` lists exactly one supporting link in `optical-topo` — that's how layer stacking works. |
| `ietf-te-topology:te` | RFC 8795 | container | TE attributes on this link. |
| `te/te-link-attributes/name` | RFC 8795 | string | Display label. |
| `te/te-link-attributes/admin-status` | RFC 8795 | enum (`up`/`down`/`testing`) | Administrative state. v1: all `up`. |
| `te-link-attributes/ietf-otn-topology:otn-link` | OTN-topo | container | OTN-link augmentation. Only on links in `otn-topo`. |
| `otn-link/tsg` | OTN-topo | identityref | Tributary Slot Granularity. v1: `ietf-layer1-types:tsg-1.25g` (1.25 Gbps slots — standard ODU2 granularity). |
| `otn-link/distance` | OTN-topo | uint32 (km) | Physical distance the OTU pipe spans. v1: 60–80 km. |
| `te-link-attributes/ietf-otn-topology:client-svc` | OTN-topo | container | Mirror of the per-TP client-svc — what client signals this link can carry. |
| `client-svc/supported-client-signal` | OTN-topo | leaf-list of identityref | We list `client-stm-64` on every OTU pipe. |

---

## 2. `tunnels.json` — root: `/ietf-te:te`

### 2.1 Top-level

```jsonc
{
  "ietf-te:te": {           // root container of TE config
    "tunnels": {
      "tunnel": [ … ]       // list of tunnel instances
    }
  }
}
```

### 2.2 Each `tunnel` entry

Keys defined by `ietf-te` unless noted.

| Key | From | Type | Meaning |
|---|---|---|---|
| `name` | ietf-te | string (key) | Tunnel name. We use `och-A`, `och-B`, `odu-A`, `odu-B`. |
| `type` | ietf-te | identityref | Selects which tunnel-type augment applies. `ietf-wdm-tunnel:wdm-tunnel` for OCh; `ietf-otn-tunnel:otn-tunnel` for ODU. |
| `source` / `destination` | ietf-te | leafref to a `te-node-id` | The endpoint NE node ids. |
| `src-tp-id` / `dst-tp-id` | ietf-te | string | Endpoint TP ids on the source/destination nodes. |
| `te-bandwidth` | ietf-te | container | Bandwidth specification — concrete fields chosen via the `type` selector. |
| `te-bandwidth/ietf-otn-tunnel:otn` | OTN-tunnel draft | container | OTN-bandwidth augmentation. Carried only on `otn-tunnel` instances. |
| `otn/rate-type` | OTN-tunnel | identityref | Which ODU rate (`odu-0`, `odu-1`, `odu-2`, `odu-2e`, `odu-3`, `odu-4`, `odu-cn`, `odu-flex`). v1: `ietf-layer1-types:odu-2` (10 Gbps). |
| `primary-paths/primary-path` | ietf-te | list | Each path is one explicit route through the underlying topology. |
| `primary-path/name` | ietf-te | string (key) | Path name (we use `primary`). |
| `primary-path/explicit-route-objects` | ietf-te | container | The Explicit Route Objects (EROs) for this path. |
| `explicit-route-objects/route-object-include-exclude` | ietf-te | list | Ordered hop list. |
| `index` | ietf-te | uint8 | Hop ordering — 1, 2, 3, … |
| `explicit-route-usage` | ietf-te | identityref | Whether to include or exclude the hop. We always use `route-include-object`. |
| `type/numbered-link-hop` | ietf-te | container | One of the choice options that say "this hop is a specific link". |
| `numbered-link-hop/numbered-link-hop/link-tp-id` | ietf-te | string | The TP from which this hop departs. We use `node/tp` notation. |
| `numbered-link-hop/numbered-link-hop/supporting-link-ref` | ietf-te | leafref `{network-ref, link-ref}` | The actual topology link this hop traverses. **This is what binds a tunnel to the topology.** For `odu-A` the EROs reference `otn-topo` links; for `och-A` they reference `optical-topo` links. |
| `primary-path/ietf-wdm-tunnel:wdm-constraint` | WDM-tunnel | container | WDM augmentation per primary-path. Only on WDM tunnels. |
| `wdm-constraint/otsi-carrier-frequency` | WDM-tunnel | uint64 (units 0.1 THz) | Carrier frequency. v1: `193000` for `och-A` (= 193.0 THz, λ1) and `193050` for `och-B` (= 193.05 THz, λ2). |
| `wdm-constraint/operational-mode` | WDM-tunnel | identityref | Transponder operational mode. v1: `ietf-layer0-types:standard-mode`. |
| `wdm-constraint/tx-channel-power` | WDM-tunnel | string (dBm) | Transmit power. v1: `0.0` dBm. |
| `wdm-constraint/wavelength-assignment` | WDM-tunnel | identityref | How wavelength was chosen. v1: `ietf-layer0-types:fixed-wavelength-assignment`. |
| `supporting-tunnel` | ietf-te | list of leafrefs | Lower-layer tunnels carrying this tunnel. Each ODU has `[{tunnel-ref: och-A}]` or `[{tunnel-ref: och-B}]`. |
| **`network-fault-mgmt-state:client-service-name`** | **project module** | **string** | **Tag identifying the client service this tunnel carries. Two ODU tunnels with the same value belong to the same client service. This is the data fed to correlation rule R3.** v1: `"svc_A"` on `odu-A`, `"svc_B"` on `odu-B`. |

---

## 3. `inventory.json` — root: `/ietf-network-hardware-inventory:network-hardware-inventory`

### 3.1 Top-level

```jsonc
{
  "ietf-network-hardware-inventory:network-hardware-inventory": {
    "equipment-rooms": { … },     // physical placement view
    "network-elements": { … }     // logical NE → component-tree view
  }
}
```

### 3.2 The physical-placement branch (`equipment-rooms`)

| Key | From | Type | Meaning |
|---|---|---|---|
| `equipment-room` | inventory | list | Buildings / data-center rooms. v1: one room `DC-1`. |
| `equipment-room/name` | inventory | string (key) | Room id. |
| `rack` | inventory | list | Racks within the room. v1: one rack `rack-1`. |
| `rack/name` | inventory | string (key) | Rack id. |
| `rack/contained-chassis` | inventory | list of `{chassis-ref}` | Chassis components physically housed here. Each `chassis-ref` is a leafref to a component uuid. |

### 3.3 The logical-NE branch (`network-elements`)

| Key | From | Type | Meaning |
|---|---|---|---|
| `network-element` | inventory | list | One NE per entry. v1: `ne1`/`ne2`/`ne3`/`ne4`. |
| `network-element/uuid` | inventory | string (key) | Production: RFC 4122 UUID. v1: short name like `ne1` for readability. |
| `network-element/name` | inventory | string | Display label. |
| `network-element/components` | inventory | container | Holds the component tree. |
| `components/component` | inventory | list | Flat list of components belonging to this NE; the *tree shape* is encoded by `parent` / `contained-child-component` references. |

### 3.4 Each `component` entry

Per-component fields are common across every component class:

| Key | From | Type | Meaning |
|---|---|---|---|
| `uuid` | inventory | string (key) | Unique id of this component. v1: dotted name (`ne1.OTU.line_tx`); production: RFC 4122 UUID. |
| `name` | inventory | string | Local label (often last segment of uuid). |
| `class` | inventory | identityref (`iana-hardware:*`) | Category: `chassis`, `module`, `port`, `fan`, `power-supply`, etc. We use `chassis`/`module`/`port`/`fan`. |
| `parent` | inventory | leafref to component `uuid` | Direct parent in the component tree. (The standard model uses `parent-rel-pos` + a relative-position scheme; we use a simpler `parent` reference for readability — both express the same hierarchy.) |
| `is-fru` | inventory | boolean | Field-Replaceable Unit flag. `true` for boards/fan, `false` for the chassis. |
| `mfg-name` | inventory | string | Vendor name. v1: `Huawei`. |
| `part-number` | inventory | string | Vendor part number. v1: e.g. `TN52NS3` (an OTU board), `TN12OAU` (an OA), `TN11FIU` (an FIU), `TN11FAN` (a fan), `TN51SCC` (system-control). |
| `serial-num` | inventory | string | Equipment serial number. |
| `hardware-rev` | inventory | string | Hardware revision. |
| `software-rev` | inventory | string | Firmware/software revision. v1: `V100R011C10`. |
| `contained-child-component` | inventory | list of `{child-uuid}` | The component's children, by uuid. The *forward* edges of the tree (the chassis lists its boards, each board lists its ports). |
| `child-uuid` | inventory | leafref | Resolves to another component's `uuid`. |
| **`network-fault-mgmt-state:state`** | **project module** | **container** | **Live operational state for the simulator. Not standard IETF — see §5.1.** |
| `state/power-state` | project | string (`on`/`off`) | Whether this chassis/board is powered. The GUI's "NE power off" / "Board power off" buttons toggle this. |
| `state/fan-state` | project | string (`ok`/`failed`) | Fan health. The "Fan failure" button toggles this on the FAN component (and on the chassis as a roll-up). |

---

## 3.5 Hardware vocabulary — what the boards and ports actually are

The `part-number` field on each `module` component names a real Huawei OSN 9800 board. The TN-codes are not arbitrary — each board plays a specific role in the optical path, and (more importantly for fault reasoning) **the board type determines which alarms can appear there**. When an alarm location reads `C.FIU.line_from_D`, it decomposes as `<NE>.<board>.<port>` — and the meaning of each segment is fixed by the conventions below.

### 3.5.1 Boards inside each chassis (v3)

Every v3 hub carries exactly six modules — one of each role below.

| `part-number` | Acronym | Full name | Role in the optical path | Alarms that originate here |
|---|---|---|---|---|
| `OptiX OSN 9800` | — | Huawei optical-transport platform | The chassis itself — passive enclosure. Carries `power-state`, aggregates `fan-state`. | chassis-level: `POWER_ABNORMAL`, `BD_OFFLINE` (when the chassis goes down) |
| `TN56WSD9` | **WSS** | **W**avelength **S**elective **S**witch | ROADM core — switches individual wavelengths between fibre directions. In v3 every hub has one; it's the board that makes a node "reconfigurable" (`is-reconfigurable-node = true` would refer to this). | OCh-layer channel-switching alarms |
| `TN56NS3`  | **OTU** | **O**ptical **T**ransponder **U**nit (coherent line-card) | The transponder. Converts client signals into OTUk-framed coherent wavelengths and back. Runs digital back-propagation in the DSP — therefore the board that can produce **PPE** traces when `nfms:ppe-enabled: true`. | OTU / SDH / FEC / ODU / client-port layers — `BEFFEC_EXC`, `FEC_BEF_COR_ER`, `B1_EXC`, `B2_EXC`, `OPU_PLM`, `OTU_TIM`, `ODU_PM_TIM`, `IN_PWR_LOW` at the OTU RX, … |
| `TN12OAU25S` | **OA** / **OAU** | **O**ptical **A**mplifier **U**nit (EDFA) | In-line amplifier. Boosts the WDM aggregate before launch and after reception (booster + pre-amp). | OMS-layer / OA-internal alarms (gain drift, pump degradation when v4 faults ship) |
| `TN11FIU` | **FIU** | **F**iber **I**nterface **U**nit | Line-side optical interface. Multiplexes the WDM aggregate from WSS+OA onto the outside fibre and separates monitor / supervisory channels. **Every OTS-layer alarm lands on an FIU port** because the FIU is what physically touches the external fibre. | OTS-layer: `OTS_LOS`, `MUT_LOS`, `IN_PWR_LOW` / `IN_PWR_ABN` at the line port |
| `TN51SCC` | **SCC** | **S**ystem **C**ontrol & **C**ommunication board | Management plane: chassis CPU, NMS gateway, alarm router. **Not in the optical path.** If SCC fails the whole chassis goes NMS-unreachable; otherwise SCC doesn't show up in cascades. | NE-management alarms (rare in this dataset) |
| `TN11FAN` | **FAN** | Fan tray | Forced-air cooling. Carries `fan-state ∈ {ok, failed}`. Fan failure triggers `FAN_FAIL`, and within minutes `TEMP_OVER` on the boards it cools. | environment layer: `FAN_FAIL`, downstream `TEMP_OVER` |

**Why the alarm-source column matters.** The rule reasoner's §3 layer-cascade rule (`src/rules/layer_cascade.py`) and §8 fingerprints (`src/rules/fingerprints.py`) read `alarm.layer` and `alarm.board` to decide what suppresses what. The board type is what *physically constrains* the layer — an `OTS_LOS` can only occur on an FIU port; a `BEFFEC_EXC` can only occur on an OTU port; a `FAN_FAIL` only on a FAN. The layer-cascade rule encodes this physics: an alarm low in the optical stack (FIU/OTS) suppresses alarms higher up (OTU/SDH/FEC/ODU) at the same physical port.

### 3.5.2 Port-name convention

Inside each module, ports follow a strict naming pattern. The full path appears in alarm rows as `<NE>.<board>.<port>` (e.g. `C.FIU.line_from_D`).

| Pattern | Direction | Meaning | Example | Boards that use it |
|---|---|---|---|---|
| `line_to_<peer>`   | TX (egress)  | The line-side port emitting light toward NE `<peer>`. | `A.FIU.line_to_B`     | FIU, OTU |
| `line_from_<peer>` | RX (ingress) | The line-side port receiving light from NE `<peer>`. | `C.FIU.line_from_D`   | FIU, OTU |
| `client_ch<N>`     | client       | The client-facing drop port that hands the demuxed channel `<N>` to a customer device. | `A.OTU.client_ch1`    | OTU only (the WDM signal terminates here) |

Two consequences worth keeping in mind:

1. **An OTS-layer alarm is always at an `FIU.line_*` port** — the photodiode that loses the signal is on the FIU, never on the OTU. So `MUT_LOS` at `C.FIU.line_from_D` tells you the optical signal arriving at C from D is gone; the matching `OTS_LOS` will appear at the same port.
2. **Cross-port pairing is implicit in the name** — `A.FIU.line_to_B` is fibre-connected to `B.FIU.line_from_A`. The topology JSON (`topology.json`) makes the pairing explicit via `link.source` / `link.destination`; the inventory just gives both endpoints names that let you eyeball the peer.

### 3.5.3 Project extensions on components

Two project-defined leaves augment certain component entries. The short prefix **`nfms`** and the full prefix **`network-fault-mgmt-state`** refer to the same project-defined module — both forms appear in the JSON for readability.

| Leaf | Where it appears | Type | Meaning |
|---|---|---|---|
| `nfms:ppe-enabled` | OTU modules only | boolean | Marks the OTU as having coherent-DSP firmware new enough to expose **Power Profile Estimation** (`Design/12_ppe_simulation.md`). The simulator's PPE generator (`src/simulator/ppe.py`) only emits a trace when the affected channel terminates on a PPE-enabled OTU. |
| `network-fault-mgmt-state:state` | chassis / module / fan | container | Runtime state (`power-state`, `fan-state`) — see §3.4. The Web UI's "NE power off" / "Board power off" / "Fan failure" buttons toggle these fields, and the simulator's fault rules read them when deciding which alarms can fire. |

### 3.5.4 Worked example — decoding the alarm location `C.FIU.line_from_D`

Given the alarm row from the Web UI: `MUT_LOS, layer=OTS, location=C.FIU.line_from_D, container=span_OMS4_DC`:

| Segment | Resolves to | Source of truth |
|---|---|---|
| `C` | NE C — one of the 6 hub NEs (A–F) in the v3 lab mesh | `inventory.json` → `network-element[uuid="C"]` |
| `FIU` | The Fiber Interface Unit module (TN11FIU) on chassis C | `inventory.json` → `component[uuid="C.FIU"]` |
| `line_from_D` | RX line port on C's FIU that receives light arriving from NE D | `inventory.json` → `component[uuid="C.FIU.line_from_D"]` (parent = `C.FIU`) |
| `span_OMS4_DC` (container) | The OMS line system between D and C, riding the fibre that terminates on this port | `topology.json` → `optical-topo` link `span_OMS4_DC` whose destination `tp-id` references this port |

Reading the alarm: *"The FIU at NE C just lost the multiplexed signal on the fibre coming in from D — i.e. something has broken the light path between D and C, on the D-to-C direction."* That is precisely the signature §8.fp_fiber_cut looks for, and `MUT_LOS` is the OTS-layer trunk-RX fault summary.

---

## 3.6 Layer model — what each `layer` tag on an alarm means

Every alarm row in `incidents.db` carries a `layer` field (`OTS`, `OMS`, `OCH`, `OTU`, `SDH`, `FEC`, `ODU`, `OPU`, `client`, `equipment`, `environment`). The YANG topology pre-declares two of them at the `network-types` level (`wson-topology` ⇒ optical layers; `otn-topology` ⇒ digital layers) — but the per-alarm `layer` tag is finer-grained than that, and it is the single most load-bearing input to the rule reasoner. This section is the canonical reference.

The optical-transport stack uses **9 distinct layer values** ranked into **6 propagation tiers** (the strict upstream→downstream chain of the ITU-T G.872 model) plus **2 off-path tags** for non-optical events. The canonical ranking is encoded in `src/rules/layer_cascade.py:18-28`:

```python
LAYER_ORDER = {
    "OTS":    0,
    "OMS":    1,
    "OCH":    2,
    "OTU":    3,
    "SDH":    4, "FEC": 4, "ODU": 4, "OPU": 4,
    "client": 5,
}
```

### 3.6.1 The 6 stacked optical-transport tiers

These form a strict *upstream → downstream* propagation chain. A failure low in the stack propagates upward; the rule reasoner therefore treats lower-rank alarms as **cause** and higher-rank alarms at the same port as **Secondary**.

| Rank | Layer | Full name | What it is | Physical home | Alarms emitted here |
|---|---|---|---|---|---|
| **0** | **OTS** | **O**ptical **T**ransmission **S**ection | The raw fibre span between two amplifier / FIU sites — the optical signal as a single composite waveform on the fibre. Bottom of the optical stack. | FIU line ports; the fibre itself | `OTS_LOS`, `MUT_LOS`, `IN_PWR_LOW`, `IN_PWR_ABN`, `IN_PWR_HIGH` |
| **1** | **OMS** | **O**ptical **M**ultiplex **S**ection | The multiplexed WDM aggregate — all wavelengths bundled together — between two ROADM nodes (which may span one or more OTS hops). | WSS / OA boards | `OMS_LOS` (rare; `OTS_LOS` usually fires first) |
| **2** | **OCh** (`OCH`) | **O**ptical **Ch**annel | A single wavelength (one λ) as it traverses the network end-to-end from transponder to transponder. `ch1`…`ch6` in v3. | WSS demux outputs, OTU line side | OCh-layer power / quality alarms |
| **3** | **OTU** | **O**ptical channel **T**ransport **U**nit | The digital wrapper applied to a wavelength: framing, OTUk overhead, FEC encoding. First layer where bit errors become meaningful. | OTU board (TN56NS3) line side | `BEFFEC_EXC` (pre-FEC BER excess), `FEC_BEF_COR_ER`, `OTU_TIM` (trace-identifier mismatch) |
| **4** | **SDH** / **FEC** / **ODU** / **OPU** | (four sub-layers at the same rank) | The digital payload layers that sit *on top* of OTU. All grouped at rank 4 because they peer at each other rather than cascade. | OTU board, ODU cross-connect | see §3.6.2 |
| **5** | **client** | client-signal layer | Customer-facing payload (STM-64, 10/100GE, …) after demapping from ODU. | OTU client ports (`client_chN`) | client-signal alarms |

### 3.6.2 Why §4 has four sub-layers at the same rank

`SDH`, `FEC`, `ODU`, `OPU` all sit *above* OTU but describe different facets of the same digital frame, so the reasoner treats them as a tied tier — none is "upstream" of any other:

| Sub-layer | Full name | What it is | Alarms |
|---|---|---|---|
| **SDH** | **S**ynchronous **D**igital **H**ierarchy | Legacy SONET/SDH framing carried inside OTUk for backward compatibility. Carries the regenerator / multiplex-section overheads (B1 / B2 bytes for BIP error counts). | `B1_EXC` (regenerator-section BIP excess), `B2_EXC` (multiplex-section BIP excess) |
| **FEC** | **F**orward **E**rror **C**orrection | Error-correction parity engine inside OTU. Counts pre-correction bit errors before they get fixed. | `FEC_BEF_COR_ER`, `BEFFEC_EXC` (also tagged OTU in some seeds — the simulator uses both layer tags depending on rule) |
| **ODU** | **O**ptical channel **D**ata **U**nit | The path-layer container inside an OTUk frame (ODU0 / 1 / 2 / 2e / 3 / 4). What gets cross-connected at OTN switches. | `ODU_PM_SSF` (server-signal-fail), `ODU_PM_BDI` (backward-defect indicator), `ODU_PM_TIM`, `BP_ODU_LOFLOM` |
| **OPU** | **O**ptical channel **P**ayload **U**nit | Sub-container inside ODU that holds the mapped client signal. Tells you *what* client was mapped in. | `OPU_PLM` (payload-type mismatch — primary `config_mismatch` signature) |

The stack as a picture (most-physical at the bottom, customer at the top):

```
        client signal (STM-64 / 10GE / …)         ← rank 5
                    ▲
            OPU  ─── ODU                          ← rank 4 (digital payload)
                    │
            FEC  ─── SDH                          ← rank 4 (digital framing)
                    ▲
                   OTU                            ← rank 3 (digital wrapper)
                    ▲
                  OCh λ                           ← rank 2 (one wavelength)
                    ▲
                   OMS                            ← rank 1 (WDM aggregate)
                    ▲
                   OTS                            ← rank 0 (raw fibre span)
                    │
                  fibre
```

### 3.6.3 The 2 off-path "layers"

These are not part of the propagation stack — they describe non-optical failures. The layer-cascade rule deliberately doesn't order them; they are handled by §5 "equipment trump cards" instead (`src/rules/equipment.py`).

| Tag | Meaning | Physical home | Alarms | Suppression scope |
|---|---|---|---|---|
| **`equipment`** | NE / board hardware events | chassis or any module | `POWER_ABNORMAL`, `BD_OFFLINE`, `HARD_BAD` | Suppresses **every** alarm at the same NE (for `POWER_ABNORMAL`) or same board (for `BD_OFFLINE` / `HARD_BAD`) regardless of optical layer |
| **`environment`** | physical-environment events | FAN tray, board thermistor | `FAN_FAIL`, `TEMP_OVER` | `FAN_FAIL` suppresses `TEMP_OVER` at the same NE on different boards (heat takes minutes to build) |

The simulator's polling-cadence map (`src/simulator/engine.py`, `POLL_PERIOD_MS`) gives `environment` a much slower poll (~5 min) than the optical layers (5–30 s) — that's why thermal alarms arrive long after the fan failure that caused them.

### 3.6.4 Two propagation refinements worth knowing

These turn the bare layer ordering into something accurate, and explain most of the rule reasoner's behaviour at the OTS port:

1. **`OTS_LOS` and `MUT_LOS` are *dominating*** (`layer_cascade.py:30`). If either fires at a port, every other alarm at that port is automatically `Secondary` to it — *regardless of layer*. Reasoning: if the bit stream is gone (LOS = Loss Of Signal), then bit-error counts on overlying layers (`B1_EXC`, `BEFFEC_EXC`, …) are physically meaningless.

2. **Within a port, lower rank dominates higher rank.** For any two alarms at the same `(NE, board, port)`, if `LAYER_ORDER[a₁] < LAYER_ORDER[a₂]`, then `a₂` is `Secondary` to `a₁`. This is what makes the reasoner pick `OTS_LOS` on `C.FIU.line_from_D` as the root and demote co-occurring `BEFFEC_EXC` / `B2_EXC` at OTU / SDH to secondary status — they're all consequences of the fibre going dark.

### 3.6.5 What the GNN sees

The training plan (`Design/11_gnn_training_plan.md` §4.1) one-hot-encodes only **7 layer values** as a per-alarm feature: `OTS / OMS / OCh / OTU / SDH / ODU / environment`. It collapses `FEC` / `OPU` into adjacent layers and excludes `client` (the simulator currently emits no client-layer alarms) and `equipment` (which the model picks up via board-level node features instead). The full 9-value vocabulary is preserved in the database for explainability and for future model variants.

---

## 4. The two-letter glossary (modules referenced in this doc)

| Prefix | Module | Source |
|---|---|---|
| `ietf-network` (no prefix at top level) | `ietf-network` | RFC 8345 |
| `ietf-network-topology` | `ietf-network-topology` | RFC 8345 |
| `ietf-te-topology` | `ietf-te-topology` | RFC 8795 |
| `ietf-wson-topology` | `ietf-wson-topology` | RFC 9094 |
| `ietf-otn-topology` | OTN topology | draft-ietf-ccamp-otn-topo-yang-20 |
| `ietf-hw-inventory-ref-topo` | hw-inventory ↔ topo cross-reference | (companion to inventory draft) |
| `ietf-te` | base TE / tunnel | `ietf-te` (RFC-pending) |
| `ietf-otn-tunnel` | OTN tunnels | draft-ietf-ccamp-otn-tunnel-model-24 |
| `ietf-wdm-tunnel` | WDM tunnels | draft-ietf-ccamp-wdm-tunnel-yang |
| `ietf-network-hardware-inventory` | Physical inventory | draft-ietf-ccamp-network-inventory-yang-01 |
| `ietf-layer0-types` | Optical-layer types (frequencies, modes) | RFC 9093 |
| `ietf-layer1-types` | OTN-layer types (ODU rates, TSG, client signals) | RFC 9094 (or its companion) |
| **`network-fault-mgmt-state`** | **project-defined** (not yet authored as `.yang`) | local |

---

## 5. The example, walked through end to end

### 5.1 The diamond + crossbar

```
                      ne1
                     /   \
               span_12   span_14
                  /         \
                ne2 ─span_24─ ne4
                  \         /
               span_23   span_43
                    \   /
                    ne3
```

Six directed fiber spans appear in `topology.json`'s `optical-topo`:

| `link-id` | source NE | dest NE | used by |
|---|---|---|---|
| `span_12` | ne1 | ne2 | svc_A |
| `span_14` | ne1 | ne4 | (free in v1) |
| `span_23` | ne2 | ne3 | **svc_A and svc_B** |
| `span_24` | ne2 | ne4 | (free in v1) |
| `span_42` | ne4 | ne2 | svc_B |
| `span_43` | ne4 | ne3 | (free in v1) |

Stacked above, four OTU pipes appear in `otn-topo`. Each carries `supporting-link` referencing exactly one optical span:

| OTU pipe | wavelength | optical underlay | used by |
|---|---|---|---|
| `otu-12-a` | λ1 | `span_12` | svc_A |
| `otu-23-a` | λ1 | `span_23` | svc_A |
| `otu-42-b` | λ2 | `span_42` | svc_B |
| `otu-23-b` | λ2 | `span_23` | svc_B |

`otu-23-a` and `otu-23-b` are **two parallel OTN links** between `ne2` and `ne3` riding the same fiber on different wavelengths. RFC 8345 allows parallel links; the `link-id` is the disambiguator.

### 5.2 The two services

```
client svc_A:    ne1 ─ otu-12-a ─ ne2 ─ otu-23-a ─ ne3       (ODU2 over λ1)
client svc_B:    ne4 ─ otu-42-b ─ ne2 ─ otu-23-b ─ ne3       (ODU2 over λ2)
                                       ▲
                       both wavelengths share span_23
```

In `tunnels.json`:

- `och-A` carries λ1, ERO `[span_12, span_23]`.
- `och-B` carries λ2, ERO `[span_42, span_23]`.
- `odu-A` carries svc_A traffic, ERO `[otu-12-a, otu-23-a]`, `supporting-tunnel: och-A`, `network-fault-mgmt-state:client-service-name = "svc_A"`.
- `odu-B` carries svc_B traffic, ERO `[otu-42-b, otu-23-b]`, `supporting-tunnel: och-B`, `network-fault-mgmt-state:client-service-name = "svc_B"`.

### 5.3 How correlation rules read this

| Rule | Query against the YANG instance |
|---|---|
| **R1 temporal** | not topology — runs on alarm timestamps in `incidents.db`. |
| **R2 OTU↔OCH** | from an alarm on TP `ne2/otu-line-from-ne1-svcA` → walk up OTN link `otu-12-a`'s `supporting-link` → `span_12` → find tunnels whose ERO includes `span_12` → `och-A`. R2 fires between an `otu-12-a` alarm and an `och-A` alarm. |
| **R3 ODU↔ODU shared client** | compare `network-fault-mgmt-state:client-service-name` on `odu-A` (`svc_A`) and `odu-B` (`svc_B`). Different ⇒ R3 does **not** fire between them. |
| **R4 OCH↔OCH shared OTS** | intersect `och-A`'s ERO (`{span_12, span_23}`) and `och-B`'s (`{span_42, span_23}`) → `{span_23}` ⇒ **R4 fires**. This is what the demo demonstrates when `span_23` is cut. |

---

## 6. Self-checks already passed

- All three JSON files parse with `json.load`.
- `topology.json`: 2 networks, 8 nodes total (4 + 4), 10 links (6 + 4).
- `tunnels.json`: 4 tunnels — 2 WDM (`och-A`, `och-B`) and 2 OTN (`odu-A`, `odu-B`); the OTN tunnels carry `client-service-name` correctly.
- `inventory.json`: 4 NEs, 47 components, equipment-room/rack contains 4 chassis-refs.
- **All 23 `inventory-id` leafrefs in `topology.json` resolve to real component uuids in `inventory.json`** — no orphan references.

---

## 7. v1 simplifications (and what to fix later)

### 7.1 Component identifiers vs. real UUIDs

The inventory module says component `uuid` is a `yang:uuid` (RFC 4122 32-hex format). v1 uses readable dotted names instead (`ne1.FIU.line_to_ne2`). Same role, less typing, and the `inventory-id` leafrefs work. To pass strict validation later we'll swap each name for a deterministic UUID — the JSON's structure stays identical.

### 7.2 `_comment` keys

Free-text annotations sprinkled through the JSONs. RFC 7951 readers ignore unknown keys (no harm), strict validators may warn. The loader will pass them through; before formal `yanglint` runs we will strip them.

### 7.3 Forward direction only

Only forward-direction services (`ne1→ne3`, `ne4→ne3`) are modelled. Reverse-direction equivalents (`span_21`, `span_32`, `otu-21-a`, etc.) are not present. Adding them is mechanical — copy-and-flip.

### 7.4 The project augmentation isn't formalised yet

`network-fault-mgmt-state` doesn't yet have a published `.yang` source. Until Phase E in `Design/03_yang_topology_plan.md`, the `state` containers in `inventory.json` and the `client-service-name` leaf on tunnels are JSON-only conventions. The intent is the spec. The loader will read them; strict validators will not, until the module exists.

### 7.5 `_comment_underlay` on `odu-A`

A second commentary key — JSON allows multiple `_comment*` siblings as long as the keys are distinct. We use it occasionally to explain non-obvious cross-layer links.

---

## 8. File summary (v3 — lab mesh)

```
data/yang/
├── YangDataDescription.md         ← this file
├── YangDataDescription.zh-CN.md   ← Chinese mirror
├── topology.json                  ← /ietf-network:networks    (2 networks, 12 nodes total, 32 links total)
├── tunnels.json                   ← /ietf-te:te               (10 tunnels: 5 OCh + 5 ODU)
├── inventory.json                 ← /ietf-network-hardware-inventory  (6 hub NEs, 116 components, 74 inventory ports)
└── v2/
    ├── topology.json              ← v2 archive — 4-NE diamond+crossbar (10 links)
    ├── tunnels.json               ← v2 archive — 4 tunnels
    └── inventory.json             ← v2 archive — 4 NEs, 47 components
```

Cross-reference check (run via `python -m src.simulator.yang_loader`):

```
v3 — Loaded YANG topology, id=e3a4c75bdc48
  NEs:           ['A', 'B', 'C', 'D', 'E', 'F']
  optical-links: 16  (8 OMS × 2 directions)
  otn-links:     16  (one OTU pipe per optical span)
  WDM tunnels:   ['och_ch1', 'och_ch2', 'och_ch3', 'och_ch4', 'och_ch5']
  OTN tunnels:   ['odu_ch1', 'odu_ch2', 'odu_ch3', 'odu_ch4', 'odu_ch5']
  Inventory ports: 74
  inventory-id leafrefs in topology.json: all 74 resolve in inventory.json
```
