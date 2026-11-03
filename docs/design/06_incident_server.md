# 06 — Incident Server (ietf-incident)

`diag_agent` acts as the **Incident Server** of `draft-ietf-nmop-network-incident-yang-20`, and the OSS is the **Incident Client**. In the draft's terms, the LangGraph decision procedure is the server's *Incident Process*. It detects the incident, assesses the service impact, and diagnoses the probable root causes. The server exposes the results through the `ietf-incident` data model, RPCs and notifications.

## 1. Capabilities

| Draft capability | diag_agent implementation | § |
|---|---|---|
| Incident detection, reporting the incident when it is identified | The framer forms a case frame; `raise_incident` assigns `incident-no` and sends `incident-notification` (`raised`) | 2, 3 |
| Service Impact Assessment (`service-instance`) | `dx:dependsOn` closure over the frame's shared elements | 5 |
| Probable root-cause identification (`probable-causes`, `probable-events`) | The ledger's committed leader, or the 90% set (doc 04 §7) | 5 |
| Self-diagnosis followed by `updated` notifications | Automatic diagnosis on raise (`auto_diagnose: true`) | 4 |
| `incident-acknowledge` RPC | Sets `ack-status` and `ack-time`, and sends a notification | 6 |
| `incident-diagnose` RPC, results reported by notification | Joins the running task or starts an on-demand re-diagnosis. In extended mode it returns a `task-id`. | 6 |
| `incident-resolve` RPC | Resolution is *advised and verified*: the server records the request, watches the root symptoms clear, then sends `cleared` | 6 |
| Error structures `incident-*-error-info` | `ietf-restconf:errors` with `error-info` | 6.4 |
| A.3 ticket correlation (`ticket-no`) | `diag-agent-incident-ext:ticket-no` on the RPCs, the notification and the incident | 5.4 |
| A.4 diagnosis task list | `incident-diagnosis-tasks` and `diagnosis-task-notification` | 5.4 |
| §9 Operational considerations: stable topology view and ticket-state translation | The bitemporal topology graph (doc 03 §5) and a deterministic state map (§8) | 8 |

## 2. Incident identification and grouping

- **One frame is one incident.**
  - An incident is created when a case frame forms, i.e. at the first ingest of alarms that are not already explained by an open incident.
  - New alarms are *correlated* into an open incident if both conditions hold:
    1. their resource is in the incident's scope, or within two hops of it;
    2. their time is inside the incident's `W_onset` or `W_post` window, or a cascade rule predicts them from an existing frame item.
  - Otherwise they open a new incident.
- **Compound faults.** These come from `/api/trigger_multi`. When the scopes overlap, the faults stay **one incident** with several `probable-cause` entries (doc 04 §7.5). When the scopes are disjoint, they become separate incidents.
- **`incident-no`.** A server-wide `uint64` sequence starting at 1001, persisted in the incident store. Following the draft, it is unique but is *not* the list key. The simulator's `incidents.id` (raw view) is kept internally as `raw_incident_id` and appears only inside event ids (`alm-<raw>-<row>`).

### 2.1 Key policy: `name`, `type` and `incident-qualifier`

The list key `[name type incident-qualifier]` must stay stable for the life of the incident. It is therefore derived at raise time from **the frame**, never from the diagnosis:

| Leaf | Rule | Fibre-cut value |
|---|---|---|
| `name` | `"<Condition label> on <container of the anchor symptom>"`. The anchor is the earliest root-capable symptom (lowest `optx:layerRank`); the container is its `optx:containerRef`. | `"Loss of signal on span_OMS3_BC"` |
| `type` | `ietf-incident:problem`. It is `ietf-incident:sla-violation` only when every symptom is a service metric crossing an SLO threshold with no fault-layer alarm, which is not produced by this test bed. | `"ietf-incident:problem"` |
| `incident-qualifier` | `""` (the draft's normal case). If an open incident already has the same `name` and `type`, the qualifier is set to the onset lower bound in ISO form, so the key stays unique. | `""` |

## 3. Lifecycle

```
                      raise (frame formed)
                              │
                              ▼
   ┌──────────────── status = raised ────────────────┐
   │  ack-status = unacknowledged                     │
   │  task dx-<no>-1: created → diagnosing            │
   └───────────────┬──────────────────────────────────┘
                   │ commit / set report / cause change / ack / ticket / priority change
                   ▼
          status = updated  ◄──────────┐ (each further change; verify mode, ≥ 0.01 movement)
                   │                    │
                   ├────────────────────┘
                   │ root symptoms cleared (substrate) after a resolve request or spontaneously
                   ▼
          status = cleared  → closure: learning writers (doc 02 §6.3)
```

| Agent state (doc 04 §7.3) | `status` | Task `task-state` | `diagnosis-result` | Extension `decision` |
|---|---|---|---|---|
| Framed, first cycle running | `raised` | `diagnosing` | — | `in-progress` |
| `COMMIT` | `updated` | `diagnosed` | `succeeded` | `committed` |
| Verification windows closed | `updated` (only if leader or NOTA moved ≥ 0.01) | `finished` | `succeeded` | `committed` |
| `IDK` with NOTA < 0.2 | `updated` | `finished` | `failed` | `set-reported` |
| `IDK` with NOTA ≥ 0.2 | `updated` | `finished` | `failed` | `probably-not-considered` |
| Limit reached | `updated` | `finished` | `failed` | `limit-reached` |
| Root symptoms cleared | `cleared` | (unchanged) | (unchanged) | (unchanged) |

**Clears.** The simulator does not yet emit clears: `kg_engine`'s `alarm.clear` is stubbed. The cleared state therefore comes from one of three sources:

| Mode | Source of the clear |
|---|---|
| Live | `alarm.clear` events, when available |
| Live, without clear events | A `CHK_SERVICE_STATE` or `CHK_ALARM_AUDIT` showing that the root symptoms are gone |
| Replay | The scenario's scripted repair, `repair_at` (doc 07 §2) |

## 4. Notification policy

`incident-notification` is sent:

1. **`raised`**, once, after framing. It carries the key, `service-instance`, `domain`, `priority`, `category` and the events so far. There are no `sources` or `probable-causes` yet: the draft says sources are populated once the incident is diagnosed.
2. **`updated`** on any of these:
   - the ledger commits or reports a set;
   - the probable-cause list changes;
   - after commit, the leader or NOTA probability moves by at least 0.01;
   - `ack-status` changes;
   - a `ticket-no` is attached;
   - `priority` or `category` changes.

   Updates within 5 s of each other are coalesced into one.
3. **`cleared`**, once.

Each notification carries the full current `incident-info`, so a client that missed one loses nothing. The `time` leaf is the occurrence time of the first event, as the draft defines it. The RESTCONF stream wraps each notification in `ietf-restconf:notification` with an `eventTime` (RFC 8040 §6.4).

## 5. Payload mapping

### 5.1 `incident-info`

| Leaf | Source |
|---|---|
| `incident-no` | Server sequence (§2), encoded as a JSON string (uint64, RFC 7951) |
| `name`, `type`, `incident-qualifier` | Key policy (§2.1) |
| `service-instance` | Services whose ODU or client reports down or degraded, **plus** services whose `dx:dependsOn` includes a frame shared element carrying a hard symptom. This is the impact assessment, which can run ahead of the slower ODU-layer alarms: ch4 is listed at raise time although its `ODU_PM_SSF` arrives only at +51.7 s. |
| `domain` | `ietf-incident:transport`. All modes in this test bed are transport (WDM/OTN). |
| `priority` | Policy table (§5.3) |
| `status`, `ack-status` | Lifecycle (§3) |
| `category` | `dx:incidentCategory` of the leader once diagnosed. Before diagnosis it comes from the anchor symptom's layer: OTS, OMS, OCh or OTU gives `network`; equipment gives `device-hardware`; environment gives `power-environment`. |
| `detail` | The narrator's text, validated against the ledger (doc 04 §11). It always states the leader, P, NOTA and the decisive evidence. |
| `resolve-advice` | `dx:resolveAdvice` of the leader, instantiated with its fitted parameters (e.g. "OTDR from B, expected at 31.4 km") |
| `sources/source` | After diagnosis: the end nodes of the probable-cause location, each listing its resources on the path. For a span these are the tx port at the source NE and the rx port at the destination NE. `network-ref` is `optical-topo` for OTS/OMS/OCh resources and `otn-topo` for OTU/ODU/client resources. |
| `probable-causes/probable-cause` | One entry per committed cause, or per member of a compound. If no commitment was made, one entry per member of the 90% set. The fields: <br>• `node-ref`: the source NE of a link-located cause, or the hosting NE of a port, board or NE cause; <br>• `cause-name`: strict or extended (§5.2); <br>• `detail`: mode, location, P and the runner-up; <br>• `resource[name]`: the location resource, with resource-level `cause-name` and a `detail` holding the fitted parameters. |
| `probable-events/probable-event` | The events matched by the leader's **Required** predictions at its location, plus the evidence of the deciding check. Each must also appear in `events` (a leafref). |
| `events/event` | Every alarm correlated into the incident, plus every PPE anomaly read by a check (or every one, if `ppe_access: streamed`). Alarm events: `type ietf-incident:alarm`, `event-id "alm-<raw>-<row>"`, `alarm/resource` = the port, board or NE `yangId`, and `alarm/alarm-type-id` = `diag-agent-alarm-types:<ALARM_NAME>`. PPE events use the metric case (§5.2). |
| `time` | The `t_observed` of the first event (`created_at + t_observed_ms`) |

The incidents container adds `incident-time-info`:

| Leaf | Value |
|---|---|
| `raise-time` | Notification 1 |
| `occur-time` | First event (mandatory) |
| `ack-time` | Acknowledge RPC |
| `clear-time` | Cleared |
| `last-updated` | Last notification |

**Encoding rules (RFC 7951):**

- `uint64` and `decimal64` values are JSON strings.
- `decimal64` values are **quantised by the mapper** to the type's fraction digits, which is 2 for metric values. `yangson` does not enforce this, as found in validation.
- Identities are always module-qualified. The draft's A.1 example uses unqualified values (`"ptn"`, `"loss-of-signal"`); this server never does.
- `date-and-time` values are UTC with milliseconds.

### 5.2 Strict and extended payload modes

| | `strict` | `extended` (default) |
|---|---|---|
| Modules | `ietf-incident`, plus `diag-agent-alarm-types` for alarm-type identities (an RFC 8632 deployment always has vendor alarm types) | Adds `diag-agent-incident-ext` (`yang/diag-agent-incident-ext.yang`) |
| `cause-name` | `dx:incidentCauseName`, e.g. `ietf-incident:loss-of-signal` or `ietf-incident:hardware-failure` | `dx:incidentCauseNameExt`, e.g. `diag-agent-incident-ext:fibre-cut`, which derives from `inc:loss-of-signal`. A strict client can still match it with `derived-from-or-self`. |
| PPE event `type` | `ietf-incident:metric` | `diag-agent-incident-ext:ppe-anomaly` (base `inc:metric`, so the `metric` case still applies) |
| Extras | — | `ticket-no`, `diagnosis-info` (decision, leader and NOTA probabilities, onset estimate, candidates, KB release, LLM profile, `tree-uri`), `incident-diagnosis-tasks`, `diagnosis-summary` in notifications, `task-id` in the diagnose output, `diagnosis-task-notification` |

The cause-name mapping per fault mode is held in the KB (`kb-fault-modes.ttl`):

| Fault mode | Strict `cause-name` | Extended `cause-name` | `category` |
|---|---|---|---|
| FIBRE_CUT | loss-of-signal | fibre-cut | network |
| SECTION_CUT | loss-of-signal | section-cut | network |
| SPAN_LOSS_INCREASE | loss-of-signal | span-loss-increase | network |
| CONNECTOR_DIRTY | loss-of-signal | connector-dirty | network |
| SDH_B1_ERRORS, SDH_B2_ERRORS | interface-hardware-failure | bit-error-degradation | network |
| FEC_DEGRADATION | interface-hardware-failure | fec-degradation | network |
| RX_POWER_DRIFT | interface-hardware-failure | rx-power-drift | line-card |
| CONFIG_MISMATCH | service-misconfiguration | configuration-mismatch | network |
| AMP_FAIL | hardware-failure | amplifier-failure | line-card |
| BOARD_POWER_LOSS | hardware-failure | board-power-loss | line-card |
| NE_POWER_LOSS | hardware-failure | ne-power-loss | power-environment |
| FAN_FAILURE | hardware-failure | fan-failure | power-environment |

### 5.3 Priority policy

| Priority | When |
|---|---|
| `critical` | At least one service is down, or a hard LOS is on a span carrying at least one service |
| `high` | Services are degraded (a `ServiceDegraded` item), or an equipment-down condition has no service down |
| `medium` | Soft or incipient symptoms with no service impact |
| `low` | Monitoring-only or environment symptoms |

The priority is recomputed after each update and is never lowered while services are still down.

### 5.4 Ticket and task augments (draft A.3 and A.4)

- **Ticket.** `ticket-no` arrives on any RPC input. It is stored on the incident and echoed in later notifications and task records.
- **Task records.** Each diagnosis run is a task `dx-<incident-no>-<n>`, holding:
  - start and end time, state and result, and a description, which is the narrator's one-line summary of cycles and checks;
  - the probable causes and probable events at the end of the task;
  - `repair-advices`.
- **Task notification.** `diagnosis-task-notification` is sent when a task reaches `diagnosed` or `finished`.

## 6. RPCs

All three RPCs take `incident-no*` (at least one). Each listed incident is processed independently. If any fails, the RPC returns an error for the first failure, and the per-incident results are logged.

### 6.1 `incident-acknowledge`

1. Sets `ack-status = acknowledged` and `ack-time`, and stores `ticket-no` if given.
2. Sends an `updated` notification.
3. If the incident is already acknowledged, returns error `repeated-acknowledge`.

### 6.2 `incident-diagnose`

- **No task running.** A new task starts:
  - If the incident's thread is parked in `await_closure`, the graph is resumed with `Command(resume={"rediagnose": true})`. This takes a return pass (doc 04 §7.4) or, after a commit, runs any remaining verification checks.
  - Otherwise a new thread starts.
- **A task is running.** The RPC **joins** it: it returns the same `task-id` and attaches the `ticket-no`.
- **Output.** `task-id` (extended mode). In strict mode there is no output, and the results arrive by notification as the draft specifies.
- **Errors:**

  | Error | When |
  |---|---|
  | `incident-not-found` | No such `incident-no` |
  | `probable-cause-unlocated` | The previous task ended `failed` with the return-pass and check limits exhausted, and nothing new is observable |
  | `resource-unavailable` | The substrate or semantic memory is down. An unavailable LLM is *not* an error: the run degrades to the no-LLM profile. |
  | `operation-timeout` | The task could not be scheduled within 10 s |
  | `permission-denied` | The token lacks the `diagnose` scope |

### 6.3 `incident-resolve`

The server never writes to the network (doc 01 §6). Resolution is **advised and verified**:

- **Accepting the request.** If a cause is committed, the RPC is accepted: the server records that resolution was requested, along with the ticket. If no cause is committed, it returns `probable-cause-unresolved`. `11-error-resolve-unresolved.json` is the example.
- **Verifying the repair.** The thread watches the root symptoms of the committed cause, the Required matched predictions such as `OTS_LOS` at `C.FIU.line_from_B`. When they clear, and the services listed in `service-instance` are up again (`CHK_SERVICE_STATE`), the server sends `cleared`.
- **Timeout.** If the symptoms persist past `resolve_timeout` (4 h by default), the server sends an `updated` notification whose `detail` says "resolution not verified". The task's `repair-advices` gains the next advised check.
- **Other errors.** `incident-not-found`, `permission-denied` and `operation-timeout` apply as in §6.2.

### 6.4 Error encoding

Errors are RFC 8040 `ietf-restconf:errors`, with `error-tag operation-failed`. The `error-info` holds the draft's `sx:structure`, e.g. `ietf-incident:incident-diagnose-error-info { incident-no, reason, description }`. The `reason` is a qualified identity and `incident-no` is a string. Malformed input instead returns `invalid-value` or `missing-element`, with no `error-info`.

## 7. API (RESTCONF-style, FastAPI, `diag-agent serve`)

All endpoints use media type `application/yang-data+json` and a bearer token with scopes `read`, `ack`, `diagnose` and `resolve`.

| Method and path | Purpose |
|---|---|
| `GET /restconf/data/ietf-incident:incidents` | The incident list. Query `?mode=strict\|extended` overrides the configured mode; `?status=raised,updated` filters. |
| `GET /restconf/data/ietf-incident:incidents/incident=<name>,<type>,<qualifier>` | One incident, by key (URL-encoded) |
| `POST /restconf/operations/ietf-incident:incident-acknowledge` | RPC (§6.1) |
| `POST /restconf/operations/ietf-incident:incident-diagnose` | RPC (§6.2); 200 with `ietf-incident:output` in extended mode, 204 in strict mode |
| `POST /restconf/operations/ietf-incident:incident-resolve` | RPC (§6.3); 204 |
| `GET /restconf/streams/incident/json` | SSE stream of `incident-notification` and `diagnosis-task-notification`, each wrapped in `ietf-restconf:notification` |
| `GET /diag/v1/incidents/{no}/tree?view=decision\|trace&format=json\|mermaid\|ascii` | The diagnosis tree (doc 04 §9); the target of `tree-uri` |
| `GET /diag/v1/incidents/{no}/stream` | SSE of tree deltas and ledger snapshots (doc 05 §5) |
| `GET /diag/v1/incidents/{no}/ledger` | Ledger terms and posterior history |

**Storage.** `incident_server/store.py` uses SQLite (`data/diag_agent/incidents.sqlite`). It holds the incident rows (key, no, status, times, ticket), the current payload JSON per mode, the notification log and the task rows. The LangGraph thread `inc-<no>` remains the source of truth for the reasoning, and the store holds only the published view.

## 8. Operational considerations (draft §9)

- **Topology freshness.** Diagnosis reads the topology graph valid at onset (doc 03 §5). A `topology.*` event during an open incident creates a new `g:net` version. It does not change the open diagnosis, but it is noted in `detail` if it touches the scope.
- **Ticket-state translation.** The mapping is deterministic, which prevents split-brain between incident and ticket states:

  | Incident state | Ticket state |
  |---|---|
  | `raised` and `unacknowledged` | Open |
  | `acknowledged` | Assigned |
  | Resolve requested | In-Progress |
  | `cleared` | Resolved |

  Re-raising a cleared incident is not allowed. A recurrence after clear is a new incident, linked through `onm:RecurrentProblem` in the closure graph.
- **Interworking with alarm management (draft §9.1).** Alarm events reference RFC 8632 alarms by `resource` and `alarm-type-id`. The leafrefs are `require-instance false`, so the server does not need to host `ietf-alarms`.

## 9. Validation

`docs/design/examples/validate.py` builds a YANG library of the following modules and validates every example with `yangson`:

- `ietf-incident`, extracted from the draft;
- the two `diag-agent-*` modules;
- the pyang-bundled IETF dependencies.

The checks it performs:

- **Data trees** (`07-incidents-strict.json`, `08-incidents-extended.json`) get full validation, including the `when` rules of `event-type-info`, the `probable-event` leafrefs and the mandatory leaves.
- **Notifications and RPC bodies** are parsed against their schema nodes, which checks types and identities.
- **Negative tests** confirm that the validator catches a bad priority, a non-cause identity, a metric container under an alarm-typed event, a dangling probable-event, a missing `domain`, a numeric `uint64`, and a probability above 1.
- **YANG modules:** `pyang --strict` passes on both `diag-agent-*` modules.

In the server, every outgoing payload is validated in the same way in CI and in debug mode. In production, validation runs on a 1% sample.

### Example files (`examples/`)

| File | Content |
|---|---|
| `01-notification-raised.json` | Raised at framing (09:30:35.6Z) |
| `02-rpc-diagnose-request.json` / `-response.json` | OSS diagnose with ticket (09:30:40Z); joins task `dx-1001-1` |
| `03-notification-updated-diagnosed.json` | Commit (09:31:35.6Z), P = 0.99287 |
| `04-rpc-acknowledge-request.json`, `04-notification-updated-acknowledged.json` | Ack (09:35:00Z); P = 0.99831 after verification |
| `05-rpc-resolve-request.json` | Resolve request (10:40:00Z) |
| `06-notification-cleared.json` | Cleared (10:52:10Z) |
| `07-incidents-strict.json`, `08-incidents-extended.json` | `GET` of the incidents container after closure, in each mode |
| `09-diagnosis-task-notification.json` | Task finished |
| `10-error-diagnose-not-found.json`, `11-error-resolve-unresolved.json` | Error responses |
