# How the KG Engine Reacts to a New Incident

Companion to `README.md`. The README tells you what files are in the package and how to run it. **This document follows a single incident from the moment the simulator commits it to the moment its triples land in the KG, and explains where the KG actually lives.**

If you came here asking "where is the KG file?" — the short answer is **there isn't a CSV**. The KG is an **in-memory triple index** that is continuously rebuilt from a streaming **NDJSON event log**. The two together form the source of truth; everything else (snapshots, CSV exports, GNN inputs) is derived from them.

---

## 1. End-to-end picture

```
┌────────────────────────────┐
│  Simulator (src/simulator) │  user clicks a fault in the GUI
│                            │  ↓
│  src.app.server:           │  POST /api/trigger → engine.expand_cascade
│    /api/trigger            │  ↓
│                            │  src.simulator.db.record(...)  ← one SQL transaction
└──────────────┬─────────────┘
               │ writes
               ▼
┌────────────────────────────┐
│  data/incidents.db         │  one new row in `incidents`
│  (SQLite, DELETE journal)  │  + N rows in `alarms`, all in one commit
└──────────────┬─────────────┘
               │ polled every 0.5 s
               ▼
┌────────────────────────────┐
│ SimulatorAdapter           │  SELECT id FROM incidents WHERE id > ?
│ (adapters/simulator.py)    │  emits Event objects into engine.queue
└──────────────┬─────────────┘
               │ asyncio.Queue
               ▼
┌────────────────────────────┐    handlers/
│ Engine main loop           │      alarms.py        ← emit location triples
│ (engine.py)                │      incidents.py     ← emit label + locality + temporal
│                            │      topology.py      ← emit topology diff
│  pulls Event,              │      grouping.py      ← decide which incident
│  picks a handler,          │
│  handler returns Delta,    │
│  Delta is applied to       │
│  TripleIndex (state.py)    │
│  and broadcast to:         │
└──┬──────────┬──────────┬───┘
   │          │          │
   ▼          ▼          ▼
┌────────┐ ┌────────┐ ┌────────────┐
│Event   │ │SSE     │ │RuleEngine  │  re-runs src.rules.infer_root
│Log     │ │fan-out │ │subscriber  │  submits rule.root_pick back
│(NDJSON)│ │(/api/  │ │            │  through the engine queue
│        │ │ events)│ │            │
└────────┘ └────────┘ └────────────┘
   │           │
   ▼           ▼
data/        browser
kg_engine/   (EventSource)
events.
ndjson.
YYYYMMDD
```

---

## 2. The pipeline, stage by stage

Suppose the user clicks **"Fiber cut on span_OMS3_BC"** in the simulator GUI. Here is exactly what happens.

### Stage 1 — The simulator writes one transaction to SQLite

`src/app/server.py::api_trigger` runs the cascade rules and then calls
`src/simulator/db.py::record(...)`. That function executes **one transaction**:

1. `INSERT INTO incidents (...)` → returns a new `incident_id`, say `5128`.
2. `INSERT INTO alarms (incident_id, ...)` — one row per alarm in the cascade (typically 40–60 rows for a fiber cut).
3. `conn.commit()` — the whole incident becomes visible atomically.

The engine never sees a partial incident.

### Stage 2 — The adapter detects the new id

`SimulatorAdapter._run` is an `async` loop polling every 0.5 s:

```python
SELECT id FROM incidents WHERE id > {last_id} ORDER BY id
```

When this returns `[(5128,)]` the adapter:

- Fetches every alarm row for incident 5128 (`SELECT * FROM alarms WHERE incident_id=5128`).
- Emits **one `alarm.raise` Event per alarm row**, with payload `{"incident_id": 5128, "row": {...}}` (incident-bounded mode) — the row dict carries every column from the SQL schema (`id`, `alarm_name`, `ne`, `board`, `port`, `severity`, `layer`, `container_id`, `t_offset_ms`, `t_observed_ms`, `is_root`, `group_id`).
- Emits **one `incident.boundary` Event** after the last alarm row, payload `{"incident_id": 5128}`.

Each event has a fresh `event_id` (a sortable nanosecond + random suffix string), an `at` timestamp, and `source="simulator"`.

In sliding-window mode (`--sliding-window`) the adapter omits `incident_id` and the boundary event; the engine's grouper assigns incidents and a background tick closes them after 6 minutes of silence.

### Stage 3 — The engine dispatches each event

`Engine.run` pulls events from `asyncio.Queue` one at a time. For each event:

1. **De-duplicate by `event_id`** — replay safety. If the engine restarted and is replaying the log, already-applied events are skipped.
2. **Pick the handler** — the dispatch table in `handlers/__init__.py` maps event kinds to pure functions `(event, state) -> Delta`.
3. **Acquire a lock**, call the handler.
4. **Apply the returned Delta** to `state.triples` (`TripleIndex.remove(...)` for removes, `.add(...)` for adds — both idempotent).
5. **Stamp** `state.last_event_id = event.event_id` for snapshot/replay correctness.
6. **Release the lock**, broadcast the Delta to every subscriber.

For our fiber_cut, the event sequence is:

```
alarm.raise   (alarm 1 of 47)   → Delta(+5 location triples, 0 removes)
alarm.raise   (alarm 2 of 47)   → Delta(+5, 0)
...
alarm.raise   (alarm 47 of 47)  → Delta(+5, 0)
incident.boundary               → Delta(+~1500 triples, 0 removes)
```

So per incident the engine processes **N + 1** events: N alarm-raises (with small Δs) and one incident-boundary (with a large Δ).

### Stage 4 — What each handler computes

#### `handle_alarm_raise` (handlers/alarms.py)

Calls `src.kg.alarm_graph.alarm_location_triples(incident_id, row, topology)`. For one alarm row, that produces up to 5 triples:

```
alarm:5128:101410   IsLayer    layer:OTS         (src=alarm)
alarm:5128:101410   AtNE       ne:C              (src=alarm)
alarm:5128:101410   AtBoard    board:C.FIU       (src=alarm)
alarm:5128:101410   AtPort     port:C.FIU.line_from_B   (src=alarm)
alarm:5128:101410   AtSpan     span:span_OMS3_BC (src=alarm)
```

(`AtSpan` / `OnPipe` / `AtChannel` / `Container` is picked by `classify_container` — fiber-cut roots sit at a span so `AtSpan` fires.)

It also appends the alarm row dict to `state.incidents[5128].alarm_rows` so the boundary handler has the full set later.

#### `handle_incident_boundary` (handlers/incidents.py)

When the boundary arrives, the full alarm list is now in `state.incidents[5128].alarm_rows`. The handler calls three derivers from `src/kg/`:

| Function | What it emits | Typical count |
|---|---|---|
| `alarm_graph.alarm_label_triples` | `RootCause(root, root)` and `Secondary(non-root, root)` keyed by `group_id` | N (one per alarm) |
| `locality.derive_locality_triples` | Pairwise `OnSameNE`, `OnSameBoard`, `OnSamePort`, `OnSameContainer`, `SameSpan`, `OnSameLink` between every pair of alarms in the incident | up to N² × 6 |
| `temporal.derive_temporal_triples` | Compact `AllClose` and `AllWithinSix` flags on `incident:5128` (or pairwise `Earlier`/`Close`/`WithinSix` if `compact=False`) | 0–2 |

For a 47-alarm fiber cut, locality alone is ~47 × 46 / 2 = 1081 pairs × a few predicates each — about 1 500 triples per incident.

#### `handle_topology_snapshot` (handlers/topology.py)

Not in our incident path — fires only when the YANG watcher detects a file change. Diffs the new topology against `state.triples`'s current `source="topology"` triples and emits the delta.

### Stage 5 — Subscribers consume the Delta

Each subscriber gets `(delta, event)`. The engine ships with three:

#### `EventLogSubscriber` (subscribers/event_log.py)

Appends one JSON line to `data/kg_engine/events.ndjson.YYYYMMDD`:

```json
{"event":{"event_id":"01779...-a3f","kind":"alarm.raise","at":1747241234567,"source":"simulator","payload":{"incident_id":5128,"row":{...}}},
 "delta":{"adds":[["alarm:5128:101410","IsLayer","layer:OTS","alarm",1.0,5128],
                  ["alarm:5128:101410","AtNE","ne:C","alarm",1.0,5128],...],
          "removes":[]}}
```

After every line: `f.flush() + os.fsync(...)`. **This file is the engine's durable source of truth.** Lose memory state → load snapshot + replay this file → exact same KG.

#### `SSESubscriber` (subscribers/sse.py)

Pushes the serialised line onto every connected browser's queue. The browser's `EventSource("/api/kg-engine/events")` connection receives one `data: {...}\n\n` per event. That's how the web UI updates live without polling.

#### `RuleEngineSubscriber` (subscribers/rule_engine.py)

This one's interesting — it doesn't just observe, it **feeds back** into the engine.

On every `alarm.raise`:
1. Look at the incident's currently-accumulated alarm rows.
2. Call `src.rules.infer_root(incident_id, alarm_rows, topology)`.
3. If the picked root alarm id changed since last time:
   - Submit a `rule.root_pick` Event to the queue with `{old_pick, new_pick}`.

The engine then processes that synthetic event like any other: `handle_rule_root_pick` returns a Delta that **removes** the old `RootCause` triple (with `source="rule:fingerprint"`) and **adds** the new one. So as more alarms arrive, you see the rule's pick walk — `(+1/-1)` events in the UI.

This is how the same `alarm:5128:X` can end up with **two** `RootCause` triples — one with `source="alarm"` (the simulator's ground truth) and one with `source="rule:fingerprint"` (the rule engine's prediction). They sit side-by-side, and downstream consumers can compare them.

### Stage 6 — Periodic background tasks

While the above runs, two background tasks also tick:

- **Snapshot loop** (every 60 s by default, only if `--snapshot`): pickles the entire `StateStore` to `data/kg_engine/snapshots/snap_<ulid>.pkl`. Keeps the most-recent 3 by default.
- **Closure tick** (every 30 s, only in `--sliding-window` mode): scans `state.incidents` for any whose youngest alarm is older than 6 minutes and emits an `incident.boundary` event for each.

---

## 3. Where the KG actually lives

There is **no kg.csv**. The KG has three physical representations, each with a different purpose.

| # | Location | Type | What it holds | Purpose |
|---|---|---|---|---|
| 1 | `engine.state.triples.all_triples` (in RAM) | Python `set[Triple]` | Every triple currently in the live KG | Source of truth at any given instant. What the SSE / state API and any in-process consumer reads. |
| 2 | `data/kg_engine/events.ndjson.YYYYMMDD` | Append-only NDJSON, one file per UTC day | One JSON record per event, each carrying its `Delta(adds, removes)` | **Durable** source of truth. Replay reconstructs (1) exactly. Survives engine restarts and crashes. |
| 3 | `data/kg_engine/snapshots/snap_<ulid>.pkl` | Pickle (binary) | A `StateStore` checkpoint | **Optional** fast restart. With a snapshot the engine doesn't have to replay the entire log from day 0 — only events newer than the snapshot's `last_event_id`. |

(1) is volatile but always fresh. (2) is the long-term truth — every triple addition and removal is captured with its provenance. (3) is just a performance accelerator on top of (2).

### Why NDJSON and not CSV?

A CSV has one row per triple, which is fine for a **static snapshot** of the KG. But the streaming KG isn't static — triples come and go. We need to represent **removals** too, and we need to group adds/removes that happened in the same event so a replay produces the same result. Each NDJSON line is one event's full diff, in order. CSV can't carry that without auxiliary columns.

You can still produce a CSV any time — `src/kg/builder.py` (the batch builder) reads `incidents.db` and writes one. The streaming engine maintains parity with the batch CSV output for any given incident; the difference is just the channel.

### Reading the KG from your own code

While the engine is running:

```python
# In-process — any subscriber or REPL-attached caller:
for t in engine.state.triples.by_predicate["RootCause"]:
    print(t)

# Cross-process — read the NDJSON file:
import json
with open("data/kg_engine/events.ndjson.20260520") as f:
    for line in f:
        rec = json.loads(line)
        for triple in rec["delta"]["adds"]:
            ...
```

The Triple schema is identical to the batch builder's (`src/kg/schema.py`), so any code that consumes the batch CSV trivially consumes the NDJSON.

---

## 4. Concrete walkthrough — what 47 alarms produce

Let's track byte-for-byte what hits the NDJSON for one `fiber_cut(span_OMS3_BC)` incident with 47 alarms.

**Step 1 — 47 `alarm.raise` events.** Each line is ~600 bytes:

- `event.payload` carries the full alarm row dict.
- `delta.adds` has 5 Triples (location only).
- `delta.removes` is empty.

Total: ~28 KB of NDJSON for the raises.

**Step 2 — 1 `incident.boundary` event.** This line is ~120 KB:

- `event.payload` is tiny (`{"incident_id": 5128}`).
- `delta.adds` carries **1 RootCause + 46 Secondary + ~1080 locality pairs × 4-6 predicate types ≈ 1 500 triples**.
- `delta.removes` empty.

**Step 3 — Up to N `rule.root_pick` events.** Each ~250 bytes. The first picks no prior root (`+1/-0`); subsequent ones swap (`+1/-1`). For our fiber_cut, the rule engine usually converges on the correct root after 2–3 alarms have arrived.

**End state**: one fiber_cut adds **~1 550 triples** to the in-memory index and writes **~150 KB** to the NDJSON file.

You can sanity-check this with:

```bash
python -m src.kg_engine status --log-dir data/kg_engine
```

Which prints the event count, total adds, total removes, and a breakdown by event kind.

---

## 5. What happens on restart

Engine restart (clean SIGINT, OS reboot, hardware crash — they all look the same to the engine):

1. `Engine.__init__` looks in `snapshots_dir` (if `--snapshot` was used).
2. **If a snapshot exists**: `pickle.load(...)` → `StateStore` is fully reconstructed in one step. `state.last_event_id` tells the engine where it was in the log.
3. **If no snapshot**: start with an empty `TripleIndex` and let the YangWatchAdapter emit the initial `topology.snapshot` for the 174 topology triples.
4. The engine resumes processing new events from the queue. Adapters reconnect (the simulator adapter just resumes polling from `_last_id`).

Either way, **no triple is lost** as long as the NDJSON log is intact: between the snapshot and the live event stream, every state transition is reproducible.

(The `replay` CLI command — `python -m src.kg_engine replay --from EVENT_ID` — is R-2+ work and currently raises `NotImplementedError`. Snapshot-based restore is implemented; pure replay-from-log is the next bit.)

---

## 6. So, real-time vs the batch CSV — when to use which

| You want to ... | Use |
|---|---|
| Train a GNN on a frozen, fully-derived KG | `python -m src.kg build` → `data/kg/alarm_triples.csv` |
| Demo live updates as faults arrive | `python -m src.kg_engine web` → http://127.0.0.1:5001 |
| Build an RCA service that consumes new alarms | Engine + `RuleEngineSubscriber` + your own subscriber listening to deltas |
| Audit "what triples did the engine emit for incident X" | `grep '"incident_id":5128' data/kg_engine/events.ndjson.*` |
| Recover after a crash | Engine startup picks up the newest pickle in `data/kg_engine/snapshots/` |
| Export the live KG to CSV for someone | Future `python -m src.kg_engine export` (R-2+ stub today); meanwhile, replay the NDJSON or just re-run the batch builder |

The two pipelines share `src/kg/schema.py` and `src/kg/{alarm_graph,locality,temporal,correlation,topology_graph}.py`, so their outputs are interchangeable.
