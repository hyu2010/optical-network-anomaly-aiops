# Real-Time Knowledge-Graph Engine — Design Document

Status: **Draft for review.**
Scope: Phase-5 deliverable. Phase-3 (the batch KG builder under `src/kg/`) is complete and produces flat CSV triples; Phase-4 (rule engine `src/rules/` and the upcoming GNN) consumes that batch artifact. This document specifies the engine that replaces the batch flow with a streaming, stateful service so the KG stays in sync with a live network in real time.
Related: `Design/04_knowledge_graph_plan.md` (KG concepts), `Design/08_root_cause_rules.md` (rules consumed at inference), `Design/09_knowledge_graph_builder.md` (the batch flow this replaces). The schema (entity types, predicates, URI scheme) defined in `src/kg/schema.py` is unchanged — only the build path is.

---

## 1. Why a Real-Time Engine

The current `src/kg/builder.py` is a **batch** pipeline:

1. Load three YANG JSON files into a `Topology` object once.
2. Iterate every row of `incidents.db` from `id = 1` to the last.
3. Write ~9 M triples to a 691 MB `alarm_triples.csv`.
4. Exit.

That works for ML training where the dataset is frozen. It does **not** work for a deployed RCA system because the real world is not frozen:

- **Topology evolves.** A new NE is brought online, a span is recabled, an OCh tunnel is rerouted, a board is replaced. Each change invalidates a slice of the topology triples.
- **Alarms flow continuously.** An NMS doesn't emit "incidents" — it streams individual alarms with raise / clear events. Incidents have no fixed start or end; they are inferred groupings.
- **Suppression context changes per-event.** An alarm that's a root cause when alone can become a derived symptom seconds later when an upstream alarm arrives.

Rebuilding 9 M triples on every alarm is not viable (~4 minutes per rebuild at our current rate). The engine must update **incrementally** — turn each external event into a small delta of triple additions and removals, broadcast that delta to consumers, and persist enough state to recover from a restart.

---

## 2. Goals & Non-Goals

### In scope

| # | Goal |
|---|---|
| G1 | Consume a live stream of topology changes and alarm events. |
| G2 | Maintain an in-memory KG state matching `src/kg/schema.py`. |
| G3 | Emit incremental triple deltas (`add` / `remove`) per event, in real time. |
| G4 | Persist deltas to an append-only event log; snapshot the in-memory state periodically. |
| G5 | Survive restarts by replaying the event log from the last snapshot. |
| G6 | Expose a query API (REST + in-process) for downstream consumers (rule engine, GNN inference, web UI). |
| G7 | Group continuously-arriving alarms into **incidents** using R1–R4 + a sliding time window — no externally-supplied `incident_id`. |

### Out of scope (initial release)

- Distributed deployment / horizontal scaling. Single-process, single-host.
- Multi-tenant separation.
- Cross-region replication.
- An RDF store / SPARQL endpoint (the in-memory model is sufficient; export to Neo4j / DGL / PyG is a downstream concern).
- The GNN inference service itself (separate phase).

---

## 3. Sources of Change

Three independent streams feed the engine. Each gets its own **adapter** that normalises the vendor / protocol-specific shape into the internal `Event` type.

| Stream | What it carries | Phase-1 source | Production source |
|---|---|---|---|
| Topology | NE / board / port / span / tunnel additions, removals, modifications | File watcher on `data/yang/*.json` | NETCONF notifications, vendor REST API |
| Alarms | Per-alarm `raise` / `clear` / `update` events | Tail `incidents.db` writes via SQLite WAL | NMS alarm stream (SNMP traps, Kafka, syslog) |
| Services | OCh / ODU tunnel provisioning + decommissioning | Same as topology (lives in `tunnels.json`) | Same as topology |

Topology and service changes can be merged into one stream in practice — both come from the network controller — but we model them separately because they trigger different handlers.

---

## 4. Event Model

Internal events are immutable dataclasses with a globally unique `event_id` for idempotency. Schema:

```python
@dataclass(frozen=True)
class Event:
    event_id: str           # ULID — sortable, unique, generated at ingest
    kind: str               # one of the kinds below
    at: float               # ingest timestamp (epoch ms)
    payload: dict           # kind-specific fields
    source: str             # adapter id (e.g. "netconf", "simulator", "snmp")
```

### Event kinds

| Kind                | Payload fields                                                                                  | When emitted                                          |
|---|---|---|
| `topology.snapshot` | `topology_id`, full Topology object (serialised)                                                | First connection / after a controller reboot / replay |
| `topology.add_ne`   | `ne_id`, `chassis_uuid`                                                                          | NE brought online                                      |
| `topology.remove_ne`| `ne_id`                                                                                         | NE decommissioned                                      |
| `topology.add_board`| `ne_id`, `board_uuid`, `type`                                                                    | Board inserted                                         |
| `topology.remove_board` | `ne_id`, `board_uuid`                                                                       | Board removed                                          |
| `topology.add_port` | `ne_id`, `board_uuid`, `port_uuid`, `name`                                                       | Port added (channel-add at a ROADM, etc.)              |
| `topology.add_span` | `link_id`, `from_ne/tp`, `to_ne/tp`, `supporting_link_id`                                       | Fiber connected                                        |
| `topology.remove_span` | `link_id`                                                                                    | Fiber disconnected                                     |
| `topology.add_tunnel` | tunnel record (name, type, ERO, supporting_tunnel, client_service_name)                       | OCh / ODU provisioned                                  |
| `topology.remove_tunnel` | `tunnel_name`                                                                              | Tunnel decommissioned                                  |
| `alarm.raise`       | `alarm_uid`, `name`, `layer`, `ne`, `board`, `port`, `container_id`, `severity`, `t_observed_ms` | Alarm fires                                            |
| `alarm.clear`       | `alarm_uid`, `t_cleared_ms`                                                                      | Alarm resolves                                         |
| `alarm.update`      | `alarm_uid`, modified fields                                                                     | Severity escalation, location refinement               |
| `incident.boundary` | `incident_id`, `closed_at`                                                                       | Engine-internal; emitted when no new alarms have joined an incident for the R1 window |

**`alarm_uid`** is the NMS-side stable identifier (Huawei's `NUM` field — see `Library/Alm_format.txt`). It is **not** an internal alarm row id; the engine maintains its own `alarm:{incident_id}:{seq}` URI.

### Event log format (NDJSON, one event per line)

```
{"event_id":"01HQAB...","kind":"alarm.raise","at":1747241234567,"source":"snmp","payload":{"alarm_uid":"272648","name":"MUT_LOS","layer":"OTS","ne":"C","port":"C.FIU.line_from_B","container_id":"span_OMS3_BC","severity":"Critical","t_observed_ms":1747241234500}}
```

Stored at `data/kg_engine/events.ndjson.YYYYMMDD` (one file per UTC day, rolled at midnight).

---

## 5. Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│ External world                                                         │
│ ┌─────────────┐  ┌──────────────┐  ┌─────────────┐  ┌──────────────┐    │
│ │ NETCONF/    │  │ NMS alarm    │  │ Simulator   │  │ YANG file    │   │
│ │ controller  │  │ stream       │  │ (dev)       │  │ watcher      │   │
│ └──────┬──────┘  └──────┬───────┘  └──────┬──────┘  └──────┬───────┘   │
└────────┼─────────────────┼──────────────────┼────────────────┼─────────┘
         │                 │                  │                │
         ▼                 ▼                  ▼                ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Adapter layer  (src/kg_engine/adapters/)                                │
│   netconf.py        alarm_stream.py     simulator.py     yang_watch.py  │
│   Normalises external messages into our `Event` schema.                 │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                                   ▼
                ┌──────────────────────────────────┐
                │ Event queue (asyncio.Queue)      │
                │ + deduplicate by event_id        │
                │ + best-effort temporal order     │
                └──────────────┬───────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Engine (src/kg_engine/engine.py)                                        │
│                                                                        │
│   ┌──────────────┐   ┌─────────────────┐   ┌──────────────────────┐   │
│   │ State store  │ ◄─┤ Per-event       │   │ Subscribers          │   │
│   │ (in-memory)  │   │ handlers        │──►│ (broadcast deltas)   │   │
│   │ + indexes    │   │ (handlers.py)   │   │ ws / log / callable  │   │
│   └──────┬───────┘   └─────────────────┘   └──────────────────────┘   │
│          │                                                            │
│          │ snapshot every N seconds                                    │
│          ▼                                                            │
│   data/kg_engine/snapshots/snap_<ulid>.pkl                            │
│   data/kg_engine/events.ndjson.<date>                                  │
└────────────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Consumers                                                              │
│   • Rule engine (src/rules/) — live RCA on each incident                │
│   • GNN inference service (future)                                     │
│   • Web UI (WebSocket subscription) — live topology + alarm panel      │
│   • Analytics / archive (replay event log into a data warehouse)        │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 6. State Store

Single in-process structure holding the live KG. Two layers.

### 6.1 Entity registry

A dict per entity type, keyed by URI:

```python
class StateStore:
    nes:       dict[str, NE]        # ne:A -> NE dataclass
    boards:    dict[str, Board]
    ports:     dict[str, Port]
    spans:     dict[str, Link]
    otu_pipes: dict[str, Link]
    och_tunnels: dict[str, Tunnel]
    odu_tunnels: dict[str, Tunnel]
    services:    dict[str, Service]
    alarms:    dict[str, Alarm]    # alarm:incident_id:seq -> Alarm
    incidents: dict[str, Incident] # incident_id -> Incident (grouping + lifecycle)
```

### 6.2 Triple index

The actual triples, indexed three ways for fast query:

```python
class TripleIndex:
    by_subject:    dict[str, list[Triple]]
    by_object:     dict[str, list[Triple]]
    by_predicate:  dict[str, list[Triple]]
    by_incident:   dict[int, list[Triple]]   # incident_id -> triples
    all_triples:   set[Triple]               # for membership tests
```

Memory budget at production scale: a working set of the last 1000 active incidents × ~500 triples each = 500 k live triples ≈ 100 MB. Inactive incidents are flushed to the event log and removed from the in-memory index after their R1 window closes.

### 6.3 Persistence

- **Event log** (append-only NDJSON): every event the engine consumes is appended **before** any handler runs. Source of truth.
- **Snapshots** (pickled `StateStore`): every 60 s by default; checkpoint to resume without replaying the full log.
- **Recovery sequence**: load newest snapshot → replay every event in `events.ndjson` with `event_id` > snapshot's last-applied id.

---

## 7. Per-Event Handlers

One handler per event kind. Each handler:

1. Reads from `StateStore`.
2. Produces a `Delta(adds: list[Triple], removes: list[Triple])`.
3. Mutates `StateStore` to reflect the delta.
4. Returns the delta to the engine for broadcast.

### 7.1 Topology handlers

| Event              | Delta produced                                                                                         |
|---|---|
| `topology.snapshot`| Diff against current store: emit add/remove triples for entities present in new but not old, and vice versa. |
| `topology.add_ne`  | `+ (ne:A, type, NE)`. Hosts edges follow when boards arrive.                                            |
| `topology.add_board`| `+ (ne:A, Hosts, board:A.OTU)`.                                                                         |
| `topology.add_port` | `+ (board:A.OTU, Hosts, port:A.OTU.line_to_B)`.                                                         |
| `topology.add_span` | `+ Connected`, `+ CarriedBy` (for any OTU pipe whose supporting-link is this span).                     |
| `topology.add_tunnel` | `+ CarriedBy` (one per ERO hop), `+ RidesOver`, `+ SupportingTunnel`, `+ Carries`.                    |
| `topology.remove_*`| Inverse: emit `remove` triples for every edge involving the removed entity. Affected alarms are reassigned or tombstoned. |

### 7.2 Alarm handlers

| Event           | Delta produced                                                                                                   |
|---|---|
| `alarm.raise`   | Allocate `alarm:incident_id:seq` URI. Emit location triples (`IsLayer`, `AtNE/Board/Port`, `AtSpan/OnPipe/AtChannel/Container`). Trigger incident-membership decision (§ 7.3). |
| `alarm.update`  | Emit removes for changed location triples, adds for the new values.                                              |
| `alarm.clear`   | Emit `+ Cleared(alarm, at_time)` triple. Do **not** remove the alarm's location triples — historical record matters. After R1 window, the incident is finalised. |

### 7.3 Incident grouping

A new alarm joins an **existing** incident when **any** of the following hold (the four R-rules from Design/08 §2 evaluated against currently-live alarms):

- **R1**: within R1's 6-minute group window of any alarm in the incident.
- **R2**: same OTU pipe as an existing OTU-layer alarm.
- **R3**: same `client_service_name` as an existing ODU-layer alarm.
- **R4**: carrying OCh shares a span with an existing OCh-touching alarm.

If no incident matches, a new incident is created (`incident_id = ULID`).

When an alarm joins, the handler also re-derives:

- Locality edges between the new alarm and every existing alarm in the same incident (`OnSameNE` / `OnSameBoard` / `OnSamePort` / `OnSameContainer` / `SameSpan` / `OnSameLink`).
- Correlation edges (`Correlated` with source `rule:R1` / `R2` / `R3` / `R4`).
- Label edges: re-evaluate `RootCause` for the incident (the rule engine in `src/rules/` is invoked once per new alarm; its picked root is emitted as a `RootCause` triple with `source: "rule:fingerprint"`). The pick can flip as new alarms arrive — the engine emits a `remove RootCause` for the old pick and an `add RootCause` for the new one.

### 7.4 Incident closure

A background tick (every 30 s) finalises any incident whose youngest alarm is older than R1's 6-minute window:

1. Emit `incident.boundary` event.
2. Persist the incident's final triple set as a "closed incident" record (similar in shape to one row of the batch `incident_manifest.csv`).
3. Drop the incident's triples from the live index.

---

## 8. Output Channels

Subscribers consume deltas. Each subscriber implements one method:

```python
class Subscriber:
    def on_delta(self, delta: Delta, event: Event) -> None: ...
```

Built-in subscribers:

| Name           | Channel             | Purpose                                                          |
|---|---|---|
| `EventLog`     | append to NDJSON    | Durability — the event log is the system's source of truth.       |
| `WebSocket`    | broadcast over WS   | Live UI updates (topology canvas, alarm panel).                  |
| `RuleEngine`   | in-process callable | `src/rules/` runs on every new alarm, picks `RootCause`, emits an update delta back to the engine. |
| `QueryAPI`     | indexes update      | A REST handler reads from the in-memory store; the subscriber refreshes its query indexes. |
| `Sink`         | pluggable           | Push deltas to Kafka / file / S3 / Neo4j / wherever a downstream system needs them. |

The engine itself does **not** persist closed-incident triples to CSV by default — the event log + snapshots are enough to reconstruct any past state. A separate offline tool (`python -m src.kg_engine export --since DATE --to data/kg/`) reproduces the batch artifacts when needed.

---

## 9. Consistency & Failure Handling

### Guarantees

- **At-least-once event processing.** Every event in the log is replayed on restart. Handlers must be idempotent (each handler checks the store before emitting an add — duplicate adds are no-ops because the `TripleIndex.all_triples` is a set).
- **In-order processing per stream.** Each adapter delivers events in source order. Cross-stream ordering is best-effort.
- **Eventually consistent KG.** A query against the state store sees a snapshot that reflects every event applied so far in the engine's lock-protected window.

### Out-of-order events

Two failure modes:

1. **Alarm clear arrives before raise.** The engine buffers the clear in a small (~1000-entry) "future events" queue keyed by `alarm_uid`. When the corresponding raise arrives, the clear is applied immediately after.
2. **Topology add arrives after an alarm references the new entity.** The alarm handler queues the alarm in a "pending" list keyed by the missing URI. When the topology event for that URI arrives, pending alarms are drained.

If either queue grows beyond its bound (alarm raised, but topology never arrives), the engine logs a warning and emits the alarm with the missing fields stubbed — the operator can still see the alarm; it just won't be fully correlated.

### Failure & recovery

- **Adapter crash** → adapter restart; on reconnect, request the controller's current snapshot to resync.
- **Engine crash** → process restart loads newest snapshot and replays events from there. Subscribers reconnect.
- **Subscriber crash** (e.g. WebSocket client) → no engine impact; subscriber re-subscribes and the engine sends a fresh state snapshot followed by deltas.
- **Event log corruption** → snapshot is the fallback; lose at most one snapshot interval (60 s) of state.

---

## 10. Performance & Scaling Targets

| Metric                          | Phase-1 target | Production target |
|---|---|---|
| Steady-state alarm event rate   | 10 / s          | 1 000 / s         |
| Burst alarm event rate          | 100 / s         | 10 000 / s         |
| End-to-end latency (raise → delta on WS) | < 100 ms       | < 1 s             |
| Snapshot interval               | 60 s            | 5 min             |
| Recovery time after crash       | < 30 s          | < 5 min           |
| Live incidents in memory        | 1 000           | 100 000           |
| Per-handler CPU budget          | 1 ms            | 100 µs            |

A 10 000 alarms / s rate at 100 µs / handler is ~1 CPU core. We're comfortably within single-process limits for the foreseeable future.

---

## 11. Module Structure

```
src/kg_engine/
    __init__.py                exports run(), Engine class
    events.py                  Event, Delta, EventKind enum
    state.py                   StateStore, TripleIndex
    handlers/
        __init__.py            dispatch table {kind -> handler}
        topology.py            add_ne, add_board, …, remove_*
        alarms.py              raise / clear / update
        incidents.py           grouping + closure logic
    adapters/
        __init__.py
        simulator.py           Phase-1: tails incidents.db via WAL
        yang_watch.py          Phase-1: watch data/yang/*.json mtime
        netconf.py             Phase-2: NETCONF notifications
        alarm_stream.py        Phase-2: Kafka / SNMP / syslog
    subscribers/
        __init__.py
        event_log.py           NDJSON append
        websocket.py           Flask-SocketIO broadcaster
        rule_engine.py         hooks into src/rules/
        query_api.py           REST GET /api/kg/*
    snapshots.py               pickle + restore
    engine.py                  main loop: queue -> dispatch -> snapshot
    __main__.py                CLI: run, replay, export, status
```

### CLI

```
python -m src.kg_engine run --source simulator        # Phase-1 dev
python -m src.kg_engine run --source netconf://...    # Phase-2 production
python -m src.kg_engine status                         # current incidents
python -m src.kg_engine replay --from EVENT_ID         # debug
python -m src.kg_engine export --since 2026-05-01      # produce batch CSVs from history
```

---

## 12. Phased Plan

| Phase | Deliverable | Estimate |
|---|---|---|
| **R-1** | Skeleton: `events.py`, `state.py`, `engine.py`, simulator adapter, event log subscriber. Replay incidents from `incidents.db` and produce the same triples as the batch builder. **End-state**: parity with `src/kg/builder.py` but via streaming. | 3–4 days |
| **R-2** | YANG file watcher; topology add/remove handlers; snapshot+restore; replay tooling. | 2–3 days |
| **R-3** | Incident grouping with R1–R4; cross-incident alarm correlation; rule engine subscriber. | 3–4 days |
| **R-4** | WebSocket subscriber + browser UI integration; query API. | 2–3 days |
| **R-5** | NETCONF adapter (using `ncclient`); production alarm-stream adapter (one of SNMP / Kafka). | 1–2 weeks |
| **R-6** | Performance hardening: profiling, async hot path, bounded queues, back-pressure to slow adapters. | 1–2 weeks |

R-1 through R-3 are the MVP. R-4 makes it demoable. R-5 and R-6 are production-readiness.

---

## 13. Downstream Consumption

| Consumer | How it connects | What it expects |
|---|---|---|
| Web UI | WebSocket subscription | Topology snapshot at connect + delta stream. Render topology canvas + alarm panel + current incident list. |
| Rule engine (`src/rules/`) | In-process subscriber | Called on every `alarm.raise`. Returns its `RootCause` pick which the engine emits as a triple. |
| GNN inference service | REST poll or subscribe | Per-incident: pulls the incident's subgraph as a HeteroData (or as triples and reconstructs). Returns `RootCause` probabilities; the engine emits them as `(alarm, RootCauseProb, value)` triples with source `model:rgcn`. |
| Analytics warehouse | Tails event log | Bulk replays into Parquet / BigQuery for offline analysis. |

The engine itself stays small: ingest → state → broadcast. Everything stateful that downstream consumers care about (running rule predictions, model inferences, archived incidents) is **their** responsibility, and they get the deltas they need from the broadcast layer.

---

## 14. Schema Compatibility

The engine produces **the same triples** as `src/kg/builder.py`:

- Same URI scheme (per `src/kg/schema.py`).
- Same predicate vocabulary.
- Same `(subject, predicate, object, source, weight)` shape; `incident_id` is replaced by `incident_id` (both are integers / ULIDs in different forms but consumers shouldn't care).

This means:

- A rule engine that works on batch CSVs works on a streaming export.
- A GNN trained on batch data can be evaluated on live data without retraining the input pipeline.
- The `export` CLI reproduces batch CSVs from the event log so the two flows stay interchangeable.

---

## 15. Open Questions

1. **Incident vs incident terminology.** The batch flow says "incident"; the streaming flow says "incident". Should we unify to one term ("incident" is more accurate for production)? Implication: rename `incident_id` → `incident_id` in the CSV columns too, eventually.
2. **Event log retention.** NDJSON files grow forever. Rotate after N days? Move to compressed Parquet after closure? Keep forever for forensics?
3. **Async vs threading model.** Pure asyncio (single event loop) or a thread per adapter + lock-protected store? Pure asyncio is simpler but blocks if any handler is CPU-bound (e.g., the rule engine on a 100-alarm incident).
4. **Backpressure when adapters are too fast.** If the NETCONF stream produces 10 000 events / s and the rule engine subscriber takes 5 ms per alarm, the queue grows unboundedly. Drop events? Drop subscribers? Per-subscriber bounded queues with overflow tombstones?
5. **Where does the rule engine's `RootCause` pick get persisted?** As an `alarm`-source triple (overriding the simulator's `is_root` label) or as a separate `rule:fingerprint`-source triple alongside the ground truth? For production data there is no ground truth — only the rule engine's pick — so the latter is cleaner.
6. **Schema migrations.** When we add a new predicate, do existing snapshots remain readable? Need a schema version on snapshots and a one-shot migration in `snapshots.py::restore`.
7. **Multi-controller / multi-vendor topology.** Different controllers expose different naming. Do we normalise at the adapter (clean but vendor-specific code) or carry vendor URIs through and resolve at query time?

---

## 16. Decisions to Make Before R-1

Three blocking decisions:

- **Q1 (open).** Do we keep "incident_id" or rename to "incident_id" everywhere? (Suggest: introduce `incident_id` in the engine; keep `incident_id` in the batch CSVs as legacy.)
- **Q3 (open).** asyncio vs threads? (Suggest: asyncio for adapters + main loop; offload CPU-bound work like rule-engine fingerprint matching to a thread pool.)
- **Q5 (open).** Where does the rule engine's pick land? (Suggest: separate `source = "rule:fingerprint"`, ground truth on `source = "alarm"` when present, model output on `source = "model:rgcn"`. Easier to compare and weight per provenance.)

Resolve those three and R-1 is unblocked.

---

## 17. Out of Scope (Future Phases)

- HA / failover (active-standby engine pair).
- Sharding by topology region (one engine per metro / data-centre).
- A formal RDF store with SPARQL.
- Online learning (continuously training the GNN on the live event stream).
- Multi-tenant isolation (one shared engine serving multiple operator domains).

These are the "after we have one engine working end-to-end" problems. None of them affects the R-1..R-6 implementation.
