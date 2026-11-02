# R-6 — Performance Hardening Plan

Status: **Acceptance criteria and target hot-spots — not yet implemented.**

R-6 is the perf-hardening pass that takes the engine from "works on the v3 lab simulator (5 k incidents over ~60 s)" to "sustains production alarm rates without back-pressure." This document captures the targets, the work items, and the test plan; the code changes happen when there's a real workload to measure against.

Why this isn't done yet: meaningful perf work requires a real workload trace. The simulator's burst rate is bounded by the GUI click cadence; the production rate is set by deployment context (number of NEs × alarm rate per NE) which we don't have yet.

---

## 1. Targets (per Design/10 §10)

| Metric | Phase-1 target | Production target |
|---|---:|---:|
| Steady-state alarm event rate | 10 / s | 1 000 / s |
| Burst alarm event rate | 100 / s | 10 000 / s |
| End-to-end latency (raise → delta on subscribers) | < 100 ms | < 1 s |
| Snapshot interval | 60 s | 5 min |
| Recovery time after crash | < 30 s | < 5 min |
| Live incidents in memory | 1 000 | 100 000 |
| Per-handler CPU budget | 1 ms | 100 µs |

The current R-1 to R-4 implementation has been measured at small scale only:

| Workload | Wall clock | Rate |
|---|---:|---:|
| 5 incidents (~250 alarms) end-to-end | ~3 s | ~80 alarms/s |
| 500 incidents (~25 k alarms, parity test) | ~70 s | ~350 alarms/s |

The 5-incident rate is dominated by startup + I/O fsync. The 500-incident rate (350 alarms/s) is roughly where R-1 sits unoptimised. Production-target steady-state (1 000/s) is reachable; the burst-target (10 000/s) needs the work in §3 below.

---

## 2. Acceptance criteria

R-6 is done when, on representative hardware (one modern x86 core, 16 GB RAM):

1. The engine sustains the production steady-state rate (1 000 alarm events/s) for ≥1 hour with no monotonically-growing queue depth.
2. The 99th-percentile end-to-end latency (engine ingest → SSE delivery) is under 1 s at steady state.
3. A 10× burst (10 000 events/s for 10 s) does not cause back-pressure failures upstream of the engine. Either the queue absorbs it without OOM, or back-pressure is signalled cleanly via a documented dropped-event counter.
4. Cold-start recovery from a 5-minute-old snapshot completes in under 5 minutes (replay-all-events).
5. With 100 000 live incidents in memory, lookup of "incidents this alarm could join" remains O(1) amortised — no O(N) scans.

---

## 3. Known hot-spots and the fix for each

### 3.1 Per-handler CPU budget

**Issue**: handlers in R-1 call `set` membership against `TripleIndex.all_triples` on every triple. For large incidents the inner `add_triple` is O(1), but the locality/temporal derivers in `handlers/incidents.py` are O(N²) over alarms in the incident — at 1000 alarms that's 10⁶ pair checks.

**Fix**: keep the existing O(N²) path for small incidents (≤100 alarms), switch to an indexed approach above that threshold — bucket alarms by `(ne, board, port, container_id)` and only emit locality edges within each bucket. Reduces 10⁶ pairs to typically 10²–10⁴.

### 3.2 RuleEngineSubscriber called per alarm

**Issue**: `src.rules.infer_root` is called from the subscriber on every `alarm.raise`. For an incident with N alarms that's N invocations, each O(N) — total O(N²). At a 100-alarm incident this is ~10 ms per alarm pair.

**Fix options (in order of preference)**:
1. Debounce: only re-run after 50 ms of silence on the incident. Lossy on very-fast streams but correct at any cadence.
2. Run inside `loop.run_in_executor(...)` so the rule engine doesn't block the main loop.
3. Incremental fingerprint matching: each fingerprint maintains per-incident state and re-evaluates only on changed inputs. Bigger change, R-7 territory.

### 3.3 Event log fsync per write

**Issue**: `subscribers/event_log.py` does `f.flush() + os.fsync(...)` after every line. On rotating media that's a 10 ms penalty per event → caps throughput at ~100 events/s.

**Fix**: batched fsync — group writes that arrive within a 100 ms window and fsync once. Configurable via constructor. Downside: a SIGKILL can lose up to 100 ms of events. For non-financial telemetry this is acceptable.

### 3.4 Bounded queues + back-pressure

**Issue**: `Engine.queue` (`asyncio.Queue`) is unbounded. A bursting adapter can grow it without limit until OOM.

**Fix**: bound the queue (default 100 k events). On `put_nowait` failure, either drop the event (with a counter) or block the adapter (back-pressure upstream). Drop-with-counter is the standard for telemetry pipelines; block-upstream is the standard for command/control. We're telemetry — drop is correct.

### 3.5 SSE fan-out

**Issue**: `SSESubscriber.on_delta` iterates the readers list and pushes to each queue under a lock. With 100 concurrent clients × 1000 events/s, that's 100k put_nowait/s — fine on a single CPU but lock-contended.

**Fix**: per-reader lock instead of one global lock; or move to an asyncio queue and `asyncio.gather(*queues_put)`.

### 3.6 Snapshot pickle size

**Issue**: `pickle.dump(state)` writes the entire `TripleIndex` including all triples. At 100 k live incidents × 500 triples = 50 M triples × ~200 bytes/triple = ~10 GB snapshot.

**Fix options**:
1. Don't pickle `TripleIndex.all_triples` — reconstruct it from the by-incident index at restore.
2. Use an external columnar store (sqlite, lmdb, parquet) for the triple index; snapshot only what's not durable elsewhere.
3. Hot/cold split: keep recent incidents in memory, flush closed ones to disk; pickle only the in-memory tail.

Option 3 is the right long-term answer; option 1 is the cheap intermediate.

### 3.7 Incident closure tick scan

**Issue**: `close_stale_incidents` scans every open incident every 30 s. At 100 k incidents that's a 100 k-iteration scan every tick — under a lock.

**Fix**: maintain a min-heap on `youngest_alarm_time_ms`; only pop entries whose time is older than the window. O(log N) per closure decision instead of O(N) per tick.

---

## 4. Test plan

A new test under `tests/test_kg_engine_perf.py` will:

1. **Generate** a synthetic event stream (just `alarm.raise` events at configurable rate; topology pre-loaded).
2. **Measure** end-to-end latency by timestamping at adapter and at a no-op subscriber.
3. **Assert** the targets from §2 against the measurements.

The test is gated on a `--perf` flag so the existing parity tests stay fast. Use `cProfile` and `py-spy` for hot-spot identification before each optimisation.

---

## 5. Order of work

When R-6 begins:

1. Build the test harness (§4).
2. Capture baseline numbers.
3. Apply fix §3.3 (event log batching) — biggest single win, cheapest change.
4. Apply fix §3.4 (bounded queue + drop counter).
5. Apply fix §3.1 (locality bucketing).
6. Apply fix §3.2 (rule engine debounce).
7. Apply fix §3.6 (don't pickle all_triples) once we measure snapshot size.
8. Apply fixes §3.5 and §3.7 if the test harness shows them as bottlenecks at the target rate.

Each step revalidates the latency / throughput targets and the parity test.

---

## 6. Things deliberately not in R-6

- **Sharding** (multiple engines per region) — Design §17 out-of-scope.
- **Lock-free data structures** — premature; CPython's GIL means single-thread async is the constraint, not locks.
- **C extension** for the hot path — only consider if §3.1 is still the bottleneck after Python-level bucketing.
- **Replacing pickle with a custom binary format** — Python is fast enough for the workloads we target. Revisit only if §3.6 option 1 is insufficient.
