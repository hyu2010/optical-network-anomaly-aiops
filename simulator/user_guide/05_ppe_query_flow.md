# PPE Query Flow — how the diag_agent asks the PPE agent for data

This note clarifies **what the PPE agent asks for, and how**, when the
diagnostic agent escalates an alarm to it. It follows the operator-facing
call flow specified in `Promts/System_design_prompt.txt` (lines 47–61) and
`Design/00_system_design.md` §6.

The short version: **the PPE agent is asked for one specific OCh, at the time
recorded on the alarm — not by any "incident id".** The operator never sees an
incident id; the only handles that exist in the real world are *which OCh / NE*
the alarm names and *when* it was observed.

---

## 1. The real-world call flow (no incident id)

```
1. Operator sees alarms in the NMS dump. Each alarm row carries a TIME and a
   LOCATION (NE, board, port, container/channel). No incident id is shown.
        │
2. Operator → copilot: "what happened / what is the root cause?"
        │  copilot triages → RCA-related → routes to diag_agent
        ▼
3. diag_agent → kg_abduction + knowledge_gateway: "does the KG already explain this?"
        ├─ KG has it (electrical, or optical & already localised) → answer. done.
        └─ KG knows it is OCh/OTN-optical but has NO PPE evidence to localise →
        ▼
4. diag_agent → ppe_agent (the instance on the NE that raised the alarm, found
   via the fabric by ne_id). It asks for ONE OCh, AT the alarm's time:
        "for OCh ch2 (dropping at NE C), at time T, give me P(z) and the
         points the receiver flags abnormal."
        ▼
5. ppe_agent → ppe_calc MCP → reads the profile that OCh's RX reconstructed at
   that time from the simulator's incidents.db → returns the curve + anomalies
   (each with a span + offset along the fibre).
        ▼
6. ppe_agent pushes that fresh PPE to knowledge_gateway → KG-Engine folds it
   into the live KG (the KG is never complete; this extends it in real time).
        ▼
7. diag_agent fuses the PPE anomaly's z with the YANG topology from the gateway
   → "fibre issue ~30 km into OMS3 (B→C)" → wraps it → copilot → operator.
```

The decisive step is **4**: the PPE request is keyed on **OCh / NE+port + time**,
both taken straight from the alarm record. No incident id.

---

## 2. How the PPE query is keyed

| Input | Where it comes from in the real flow | Required? |
|---|---|---|
| **OCh identity** — `och="och_ch2"` *or* `(ne="C", otu_port="rx_ch2")` | the alarm's location (which NE it fired at, which channel/container) | **yes** |
| **time** — `at_time_ms` | the **time recorded on the alarm** (its observation time) | **yes** |
| `incident_id` | — (a simulator shortcut only) | **no — omitted in production** |

`list_rx_ports(ne)` is the discovery step: given the NE that raised the alarm,
it returns that NE's per-OCh RX ports (`rx_ch1`…`rx_ch4` on C), so the agent can
pick the OCh that matches the alarm's channel.

> **PPE is per-OCh-drop-receiver.** "The PPE data of one OCh" is the profile the
> OCh's *destination* NE reconstructs over the OCh's whole route. So an OCh is
> implicitly tied to its drop NE (ch1–ch4 drop at C; ch5–ch6 at F), which is why
> the PPE fleet is per-NE and selected by the NE that raised the alarm:
> `ppe@C` owns ch1–ch4, `ppe@F` owns ch5–ch6.

---

## 3. The MCP tools and their I/O

The PPE agent (`tools_agts/ppe_agent.py`) is a thin, LLM-free wrapper over the
simulator-backed MCP server (`tools_agts/ppe_calc_mcp_server.py`), which reads
`incidents.db` (`ppe_traces` + `ppe_anomalies`).

| Tool | Asks | Returns |
|---|---|---|
| `list_rx_ports(ne)` | which OChs drop at this NE | `[{port_uuid, port_name, och}]` |
| `get_power_profile(och \| (ne,otu_port), at_time_ms?, incident_id?)` | the full curve | `{och, drop_ne, total_length_km, delta_z_m, n_samples, span_ids, degraded, samples[], baseline[], t_observed_ms}` |
| `get_ppe_anomalies(...)` | the abnormal points | `{och, drop_ne, degraded, anomalies:[{kind, z_km, span_id, offset_in_span_km, magnitude_db, confidence, is_root}]}` |
| **`locate_optical_fault(...)`** | the headline skill — localise | `{och, drop_ne, path[], located_span, offset_in_span_km, z_km, kind, confidence, reasoning}` |
| `health_check()` | liveness | server/DB status |

`locate_optical_fault` has three meaningful outcomes:
- **spatial anomaly found** → `located_span` + offset, `confidence: high/medium`;
- **degraded, no anomalies** → "receiver sees no light → likely a hard cut";
- **no spatial anomaly** (only `rx_offset`) → `located_span: null`, `confidence:
  low`, "optical path healthy → cause is receiver-side / electrical." (This is
  the optical-vs-electrical gate.)

---

## 4. The time-base nuance (relative vs. absolute)

Each alarm carries `t_offset_ms` (physical propagation) and **`t_observed_ms`**
(poll-jittered observation time). In the **simulator** these are **relative
milliseconds from the incident trigger** (e.g. ~4000 ms), *not* a wall-clock
date — the calendar `START-TIME` in the NMS dump is derived from the incident's
`created_at` plus that offset. The PPE traces carry the **same** `t_observed_ms`
base, so matching `at_time_ms` ↔ `ppe_traces.t_observed_ms` is apples-to-apples
*within one incident*.

The consequence for selection (handled in `ppe_calc_mcp_server._find_trace_row`):

- A **relative** offset is **not globally unique** — two unrelated incidents can
  share a near-identical offset. So `at_time_ms` is **not** used to pick *across*
  incidents.
- When no `incident_id` is given, the server scopes to the **latest incident**
  for that OCh (in a live session, the just-triggered one) and uses `at_time_ms`
  only to disambiguate that incident's **raw vs. suppressed twin**.
- Passing `incident_id` (the simulator shortcut; the live app can supply the
  just-triggered id) makes selection fully deterministic.

In a **real deployment** the alarm timestamp is absolute and unique, and the
receiver's PPE snapshots are timestamped the same way, so OCh + time pins the
snapshot directly — the logic (*match OCh, then nearest time*) is identical; only
the time base differs.

---

## 5. Where this lives in the code

| Step | Code |
|---|---|
| Extract the alarm's time to pin the snapshot | `diag_agt/diag_agent.py::_alarm_time_ms` (latest `t_observed_ms`, falls back to `t_offset_ms`) |
| diag_agent escalates to PPE with OCh + NE + time | `diag_agt/diag_agent.py::locate_optical_fault` → `ppe.locate_optical_fault(och, alarms, at_time_ms=…)` |
| Fabric picks the PPE on the alarm's NE | `knowledge_gateway.discover_ppe_agent(ne_id=…)` |
| Gateway's PPE annotation also pins by alarm time | `knowledge_engine/knowledge_gateway.py::get_diagnostic_kg` (derives `at_time_ms` from the alarm set) |
| PPE agent forwards to the MCP | `tools_agts/ppe_agent.py::locate_optical_fault` / `get_power_profile` / `get_ppe_anomalies` |
| Time/OCh → the right trace | `tools_agts/ppe_calc_mcp_server.py::_find_trace_row` (latest-incident scoping + twin disambiguation) |
| Fresh PPE pushed back into the live KG | `ppe_agent.push_fresh_ppe_to_gateway` → `knowledge_gateway.push_ppe_reading` → KG-Engine |

---

## 6. Worked example

`ppe@C` is asked to localise ch2 from a fibre fault on OMS3, using only what the
alarms carry:

```python
# diag_agent, real-world style — NE + time come from the alarms; no incident id
diag_agent.locate_optical_fault(
    "och_ch2",
    alarms=[
        {"name": "IN_PWR_LOW", "ne": "C", "container_id": "span_OMS3_BC",
         "layer": "OTS", "t_observed_ms": 4200},
        {"name": "BEFFEC_EXC", "ne": "C", "container_id": "span_OMS3_BC",
         "layer": "OTU", "t_observed_ms": 4500},
    ],
)
# → _alarm_time_ms picks 4500; discover_ppe_agent(ne_id="C") → ppe@C;
#   MCP scopes to the latest och_ch2 incident and localises:
# {
#   "located_span": "span_OMS3_BC", "offset_in_span_km": 40.4, "z_km": 140.4,
#   "kind": "step", "confidence": "high", "responding_agent": "ppe_agent@C",
#   "reasoning": "PPE flags a step anomaly at cumulative z=140.4 km →
#                 40.4 km into span_OMS3_BC on the och_ch2 path."
# }
```

---

## See also

- `Promts/System_design_prompt.txt` — the source spec (call flow, lines 47–61).
- `Design/00_system_design.md` §4.7 (PPE MCP), §5 (real-time KG push), §6 (call flow).
- `Design/12_ppe_simulation.md` — the per-OCh PPE model.
- `tools_agts/ppe_agent.py`, `tools_agts/ppe_calc_mcp_server.py` — the agent + MCP.
- `knowledge_engine/README.md` — kg_abduction + knowledge_gateway (the KG-first stop).
