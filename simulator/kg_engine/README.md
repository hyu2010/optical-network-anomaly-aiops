# src/kg_engine — Real-time Knowledge-Graph Engine

## What it does

The streaming counterpart to `src/kg/`. Where `src/kg/builder.py` walks `incidents.db` from start to end and writes ~9 M triples to a CSV in one shot, this package consumes the same incidents **incrementally** and emits triples per alarm event — keeping an in-memory KG, an append-only event log on disk, and (optionally) live CSV mirrors of the batch artifacts.

In one sentence: it turns external events (new alarms, YANG topology edits) into KG triple deltas in real time, and broadcasts those deltas to anything that wants to react — rule engine, SSE web UI, on-disk CSV mirrors, future GNN inference service.

Concretely, the engine handles four kinds of inputs:

1. **New alarms** from the simulator (or eventually an NMS stream) — emits per-alarm location triples + per-incident label / locality / temporal triples.
2. **Topology changes** from YANG file edits (or eventually NETCONF) — diffs the new topology against the live triple index and emits add/remove triples accordingly.
3. **Sliding-window incident grouping** — over a 6-min window, decides which alarms belong to the same incident using the four R-rules (R1–R4) from `Design/08`.
4. **Snapshot / restore** — every 60s pickles state to disk so a restart resumes in milliseconds instead of replaying the full event log.

- **Design**: `Design/10_realtime_kg_engine.md` (English) / `.zh-CN.md` (Chinese)
- **Schema**: shared with batch via `src/kg/schema.py` (Triple shape, URI scheme, predicate names — all unchanged)
- **Current status**:
  - **R-1 complete** — streaming parity with the batch builder (2.1 M triples match at 500 incidents)
  - **R-2 complete** — pickle snapshots + restore; YANG file watcher emits `topology.snapshot` events; topology triples land in state
  - **R-3 complete** — sliding-window incident grouping (R1–R4); rule-engine subscriber emits `RootCause` triples with `source="rule:fingerprint"` (20/20 agreement with simulator ground truth in smoke test)
  - **R-4 complete** — SSE subscriber + a self-hosted demo Flask app at `web.py` (`python -m src.kg_engine web`)
  - **Live CSV mirror complete** — `CsvMirrorSubscriber` keeps `data/kg/{topology,alarm}_triples.csv` and `incident_manifest.csv` in sync with engine state at each `incident.boundary` / topology delta. On by default in `web`; opt-in via `--mirror-csv` on `run`.
  - **R-5 scaffolded** — `adapters/netconf.py`, `adapters/alarm_stream.py` are placeholders with implementation outlines; awaits deployment-target specifics
  - **R-6 documented** — perf targets, hot-spots, fixes, and test plan in `docs/04_kg_engine_perf_plan.md`; awaits a real workload trace

---

## Architecture at a glance

```
                        Adapters                       Engine                       Subscribers
                  ┌─────────────────────┐   ┌──────────────────────────┐   ┌──────────────────────┐
 incidents.db ──▶ │ SimulatorAdapter    │   │                          │   │ EventLogSubscriber   │ ──▶ events.ndjson
 data/yang/ ────▶ │ YangWatchAdapter    │ ─▶│  asyncio.Queue           │   │ RuleEngineSubscriber │ ──▶ in-process RootCause
 (R-5: NETCONF/  │ NetconfAdapter      │   │       │                  │ ──▶│ SSESubscriber        │ ──▶ HTTP SSE clients
 alarm stream)   │ AlarmStreamAdapter  │   │       ▼                  │   │ CsvMirrorSubscriber  │ ──▶ data/kg/*.csv
                  └─────────────────────┘   │  dispatch (under lock)   │   └──────────────────────┘
                                            │       │                  │
                                            │       ▼                  │
                                            │  handlers/{alarms,       │
                                            │           incidents,     │
                                            │           topology,      │
                                            │           grouping}.py   │
                                            │       │                  │
                                            │       ▼                  │
                                            │  Delta(adds, removes)    │
                                            │       │                  │
                                            │       ▼                  │
                                            │  StateStore.triples      │
                                            │  (TripleIndex + index    │
                                            │   by subject/object/     │
                                            │   predicate/incident)    │
                                            │       │                  │
                                            │       ▼                  │
                                            │  snapshot every 60s ────▶│ ──▶ snapshots/*.pkl
                                            └──────────────────────────┘
```

The three layers — adapters, engine, subscribers — are deliberately decoupled:

- **Adapters** translate the outside world (file watchers, DB tails, NETCONF) into a uniform `Event` stream. Adding a new source is one new file in `adapters/`.
- **The engine** owns the in-memory KG (`StateStore`) and the dispatch loop. Handlers are pure functions `(event, state) -> Delta`, so they're easy to test and to extend.
- **Subscribers** consume each `Delta` after it's been applied. They never mutate state; they just react. Adding a new output channel (a new database, a webhook, a metrics counter) is one new file in `subscribers/`.

That separation is what makes the CSV mirror, the SSE feed, and the rule engine all sit alongside each other without entangling — each is a 50-100 line subscriber over the same event stream.

---

## Quick start

```bash
# Drain every incident in data/incidents.db, write event log, exit.
python -m src.kg_engine run --once --log-dir data/kg_engine

# Bounded drain (parity-test / smoke).
python -m src.kg_engine run --once --max-incidents 10 --log-dir data/kg_engine

# Summarise the newest event-log file.
python -m src.kg_engine status --log-dir data/kg_engine

# Run with R-2 + R-3 features (YANG watcher, snapshots, rule engine, sliding window).
python -m src.kg_engine run \
    --yang-watch --snapshot --rule-engine --sliding-window \
    --log-dir data/kg_engine

# Mirror engine state into data/kg/*.csv in real time (matches batch CSV columns).
python -m src.kg_engine run --mirror-csv --yang-watch --snapshot \
    --log-dir data/kg_engine

# R-4 — launch the SSE demo UI on http://127.0.0.1:5001 (CSV mirror is ON by default
# here; pass --no-mirror-csv to disable).
python -m src.kg_engine web
```

---

## Incremental update of an existing KG (production workflow)

You already have a complete KG (`data/kg/alarm_triples.csv` from `python -m src.kg build`) and want to keep it updated in real time as:

- Topology changes (operator edits `data/yang/*.json`, or — once R-5 ships — the controller pushes NETCONF notifications)
- New alarms arrive (simulator writes new incidents; or a live NMS stream feeds them)
- New alarm types / fault classes appear

**Don't re-run the batch builder every time.** That throws away the engine's continuous-update guarantees and is slow. Use the engine in two phases instead.

### Phase 1 — One-time bootstrap

Drain everything in `incidents.db` once, with snapshot enabled. The engine rebuilds the same KG the batch builder would, then pickles its state to disk:

```powershell
python -m src.kg_engine run `
    --once --snapshot --yang-watch --rule-engine `
    --log-dir data\kg_engine
```

Result:
- `data\kg_engine\events.ndjson.YYYYMMDD` — full event log
- `data\kg_engine\snapshots\snap_<ulid>.pkl` — `StateStore` checkpoint with the full KG in memory form

### Phase 2 — Long-running incremental mode

From now on, start the engine **without `--once`**. It restores from the snapshot in milliseconds and processes only what's new:

```powershell
python -m src.kg_engine run `
    --snapshot --yang-watch --rule-engine `
    --log-dir data\kg_engine
```

The engine now does, automatically and in the background:

| Trigger | What the engine does |
|---|---|
| Operator clicks a fault in the simulator GUI | New row in `incidents.db` → `SimulatorAdapter` polls (0.5 s) → emits `alarm.raise` + `incident.boundary` → handlers add triples → snapshot updates on next tick |
| You edit `data\yang\topology.json` (or `tunnels.json` / `inventory.json`) | `YangWatchAdapter` detects the mtime change (1 s) → reloads the YANG → emits `topology.snapshot` → `handle_topology_snapshot` diffs old vs new and emits `Delta(adds, removes)` |
| A new alarm type or layer appears in the data | Handlers are data-driven — they read `row['layer']`, `row['ne']`, etc. directly. Unknown alarm names or new layer strings produce valid triples (e.g. `IsLayer alarm:... layer:newlayer`) with no code change. |

### One caveat — `web.py` defaults

The `python -m src.kg_engine web` demo uses `start_from_now=True` so the UI starts empty (helpful for demos: only new fault clicks show up). For an incremental-update use case where you want existing `incidents.db` rows processed too, edit `src\kg_engine\web.py::create_app`:

```python
# remove start_from_now so existing incidents.db rows are processed
engine.register_adapter(SimulatorAdapter(db_path=db_path)(engine))

# enable snapshots so the demo restores on restart
engine = Engine(snapshots_dir="data/kg_engine/snapshots",
                snapshot_interval_s=60.0)
```

The CSV mirror is **on by default** in `web` — every fault click appends to `data/kg/alarm_triples.csv` + `incident_manifest.csv`, and any YANG edit rewrites `topology_triples.csv`. Pass `--no-mirror-csv` if you don't want the on-disk CSVs touched (e.g. when running the UI purely as a transient demo).

### Verifying it's wired

```powershell
python -m src.kg_engine status --log-dir data\kg_engine
```

After Phase 1 you should see hundreds of thousands of `triple adds`. Then make any change — touch a YANG file, click a fault — and run `status` again: event count and triple delta should both increase.

### Where the "current KG" lives during this workflow

| Layer | Volatility | When to read it |
|---|---|---|
| In-memory `engine.state.triples` | Lost on restart | Live consumers — rule engine, SSE, in-process subscribers |
| `data/kg_engine/events.ndjson.*` | Durable, append-only | Long-term truth — every triple addition / removal recorded with provenance |
| `data/kg_engine/snapshots/*.pkl` | Disposable, regenerated every 60 s | Fast restart only — deletable any time; the log can rebuild state |
| `data/kg/{topology,alarm}_triples.csv` + `incident_manifest.csv` | Durable, append-only for alarms + manifest, atomic-rewrite for topology | Optional convenience for downstream consumers that already expect the batch CSV columns (e.g. the planned `src/ml/` trainer). Generated by `CsvMirrorSubscriber`. |

Trade-off: if the NDJSON log is intact, the KG is recoverable from zero state. The snapshot just bounds how long that takes. You don't *have* to maintain CSVs — the streaming log is richer (it captures removals and event grouping). Turn the CSV mirror on when downstream tools (the GNN trainer, pandas notebooks, batch consumers) want the batch-builder layout updated continuously rather than rebuilt on demand.

See `INCIDENT_FLOW.md` for the full step-by-step trace of one incident through this pipeline.

Long-running mode (poll `incidents.db` forever, emit events as the simulator commits new incidents) — omit `--once`:

```bash
python -m src.kg_engine run --log-dir data/kg_engine
```

The event log lives at `data/kg_engine/events.ndjson.YYYYMMDD` (one file per UTC day, one JSON record per event). Each line is `{"event": {...}, "delta": {"adds": [...], "removes": [...]}}`.

---

## What R-1 does

| Stage | Responsibility | Where |
|---|---|---|
| Adapter | Polls `incidents.db` for `id > last_id`, emits one `alarm.raise` per row and one `incident.boundary` per incident | `adapters/simulator.py` |
| Engine | Pulls events from an `asyncio.Queue`, dispatches to handlers under a lock, applies the returned `Delta`, broadcasts to subscribers | `engine.py` |
| Alarm handler | Emits per-alarm location triples (`IsLayer`, `AtNE`, `AtBoard`, `AtPort`, `AtSpan`/`OnPipe`/`AtChannel`/`Container`); accumulates rows on the open incident | `handlers/alarms.py` |
| Incident handler | At `incident.boundary`, runs label (`RootCause`/`Secondary`) + locality (`OnSameNE`, `OnSameBoard`, …) + temporal (`AllClose`, `AllWithinSix`) derivers across all rows in the incident | `handlers/incidents.py` |
| Subscriber | Appends `(event, delta)` as one NDJSON line per event; fsyncs each write | `subscribers/event_log.py` |

**Parity bar (proven)**: for the same `incidents.db`, the streaming engine's set of `(subject, predicate, object, source, incident_id)` tuples equals the batch builder's `alarm_triples` after excluding topology triples and `rule:R*` correlation edges.

---

## What R-2 / R-3 / R-4 add

| Phase | Feature | Where |
|---|---|---|
| R-2 | Pickle snapshots + restore | `snapshots.py`, wired into `engine.py` |
| R-2 | YANG file watcher (mtime polling, no new deps) | `adapters/yang_watch.py` |
| R-2 | Topology diff via `topology.snapshot` event | `handlers/topology.py` |
| R-3 | Sliding-window incident grouping under R1–R4 | `handlers/grouping.py` (engine flag `sliding_window=True`) |
| R-3 | Background tick closes incidents past the 6-min window | `engine.py::_closure_loop` |
| R-3 | Rule-engine subscriber runs `src.rules.infer_root` and emits `RootCause` with `source="rule:fingerprint"` | `subscribers/rule_engine.py` |
| R-4 | Server-Sent Events fan-out subscriber | `subscribers/sse.py` |
| R-4 | Standalone demo Flask app (`python -m src.kg_engine web`) | `web.py` |
| Live CSV mirror | `CsvMirrorSubscriber` — rewrites `data/kg/topology_triples.csv` on topology deltas; appends to `data/kg/alarm_triples.csv` + `incident_manifest.csv` at each `incident.boundary`. Restart-safe via manifest-seeded dedup. | `subscribers/csv_mirror.py` |

## What's still deferred

| Phase | Feature | Spec |
|---|---|---|
| R-5 | NETCONF + alarm-stream adapters | `Design/10 §11`; outlines in `adapters/netconf.py`, `adapters/alarm_stream.py` |
| R-5 | UI live-stream wired into `src/app/server.py` (R-4 demo app stands alone) | — |
| R-6 | Performance hardening (bounded queues, batched fsync, locality bucketing, rule-engine debounce) | `docs/04_kg_engine_perf_plan.md` |

The package layout already contains the seams (`adapters/`, `subscribers/`, the `handlers/` dispatch table); each later phase fills in code without restructuring.

---

## Module layout

```
src/kg_engine/
├── __init__.py            # exports Engine, run
├── README.md              # this file
├── README.zh-CN.md        # Chinese version
├── events.py              # Event, Delta, EventKind, Source constants, new_event_id
├── state.py               # StateStore, TripleIndex (idempotent add/remove), Incident
├── engine.py              # async main loop: queue → dispatch → broadcast,
│                          # snapshot + closure tick loops
├── snapshots.py           # R-2: pickle snapshots + restore
├── web.py                 # R-4: standalone Flask SSE demo app
├── handlers/
│   ├── __init__.py        # dispatch table {kind → handler}
│   ├── alarms.py          # alarm.raise (clear/update stubs)
│   ├── incidents.py       # incident.boundary → labels + locality + temporal
│   ├── topology.py        # R-2: topology.snapshot (diff via index)
│   └── grouping.py        # R-3: sliding-window R1-R4 incident assignment
├── adapters/
│   ├── __init__.py
│   ├── simulator.py       # tails incidents.db
│   ├── yang_watch.py      # R-2: data/yang/*.json mtime poller
│   ├── netconf.py         # R-5: placeholder (NotImplementedError on construction)
│   └── alarm_stream.py    # R-5: placeholder (NotImplementedError on construction)
├── subscribers/
│   ├── __init__.py
│   ├── event_log.py       # NDJSON append-only log, daily rotation, fsync
│   ├── rule_engine.py     # R-3: wraps src.rules.infer_root; emits rule:fingerprint
│   ├── sse.py             # R-4: per-reader queue fan-out for HTTP SSE
│   └── csv_mirror.py      # mirrors data/kg/*.csv from live engine state
└── __main__.py            # CLI: run | status | web | replay/export (R-2+ stubs)
```

Runtime data (created on first run, gitignored):

```
data/kg_engine/
├── events.ndjson.YYYYMMDD       # one file per UTC day
└── snapshots/                   # only when --snapshot is used
    └── snap_<ulid>.pkl          # most recent state pickle (R-2)
```

---

## Programmatic use

```python
import asyncio
from src.kg_engine.engine import Engine
from src.kg_engine.adapters import SimulatorAdapter
from src.kg_engine.subscribers import EventLogSubscriber

async def main():
    engine = Engine()                              # loads YANG from data/yang/
    engine.register_subscriber(EventLogSubscriber("data/kg_engine"))
    engine.register_adapter(SimulatorAdapter(once=True, max_incidents=50)(engine))
    await engine.run()
    print(f"final state: {len(engine.state.triples.all_triples)} triples")

asyncio.run(main())
```

Subscribers only need to implement one method:

```python
class MySubscriber:
    async def on_delta(self, delta, event):
        for t in delta.adds:    ...
        for t in delta.removes: ...
```

Register the subscriber with `engine.register_subscriber(MySubscriber())`. Handlers are pure functions `(event, state) -> Delta`; register custom ones with `engine.register_handler(kind, fn)`.

---

## Design decisions (load-bearing)

Pulled forward from `Design/10 §16`. Override by editing the relevant constants in `events.py` / handlers — they're isolated to make this easy.

| ID | Decision | Why |
|---|---|---|
| Q1 | Engine uses `incident_id` internally; batch CSVs keep `incident_id` | Avoids touching the batch artifacts; in R-1 they're 1:1 anyway |
| Q3 | Pure `asyncio` for adapters + main loop. CPU-bound subscribers can offload via `loop.run_in_executor` later | Single event loop, simple to reason about |
| Q5 | Rule-engine pick uses `source="rule:fingerprint"`; simulator ground truth keeps `source="alarm"`; model output reserves `source="model:rgcn"` | Clean provenance separation; the same alarm can have multiple `RootCause` triples differing only by source |

---

## Verification

Run the parity test as a standalone script or under pytest (if installed):

```bash
python tests/test_kg_engine_parity.py --max-incidents 5     # quick
python tests/test_kg_engine_parity.py --max-incidents 500   # heavier
```

Or directly against the CLI:

```bash
python -m src.kg_engine run --once --max-incidents 10 --log-dir /tmp/kg
python -m src.kg_engine status --log-dir /tmp/kg
```

The test fails loud if streaming triples diverge from `src.kg.builder.build_kg(...)` output (modulo the documented exclusions).

---

## Related code

- `src/kg/` — batch builder this replaces. Still useful for cold-start full-DB exports.
- `src/kg/schema.py` — `Triple`, URI helpers, predicate names. Engine imports verbatim.
- `src/kg/alarm_graph.py` — `alarm_location_triples` and `alarm_label_triples` were extracted here during R-1 so both batch and streaming share the per-row code.
- `src/kg/locality.py`, `src/kg/temporal.py` — reused as-is by `handlers/incidents.py`.
- `src/simulator/db.py` — schema of `incidents.db` (what the adapter reads).
- `src/simulator/yang_loader.py` — loads the `Topology` object the engine holds in `StateStore.topology`.
- `src/rules/` — rule-based RCA reasoner. Will be wired in as an in-process subscriber in R-3 to emit `RootCause` triples with `source="rule:fingerprint"`.
