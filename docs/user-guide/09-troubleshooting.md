# 9. Troubleshooting

## The agent cannot be imported

```text
ModuleNotFoundError: No module named 'diag_agent'
```

Run every command from the repository root. Prefer `.venv/bin/python -m diag_agent ...`, which puts the root on `sys.path`. If you invoke the modules another way, set `PYTHONPATH=.`.

## Semantic memory is missing or stale

```text
FileNotFoundError: .../data/diag_agent/semantic.trig
```

```bash
.venv/bin/python -m diag_agent kb build
# after editing a TTL file or the topology:
.venv/bin/python -m diag_agent kb build --force
```

Also check that `data/yang/*.json` and `data/alarm_*.json` are present (see [Installation](01-installation.md)).

## YANG validation fails on `alarm-type-id`

```text
ValidationFailed: ... alarm/alarm-type-id} None
```

The validator needs the *generated* alarm-types module (about 1000 identities), not the short excerpt under `diag_agent/incident_server/yang/`. Rebuild:

```bash
.venv/bin/python -m diag_agent kb build --force
ls data/diag_agent/yang/diag-agent-alarm-types.yang
```

## The LLM cassette misses

```text
CassetteMiss: no recorded answer for ...
```

In `read_only` mode a missing recording makes the LLM line abstain; the run continues with the other lines. To re-record after a prompt or KB change:

```bash
rm tests/diag_agent/cassettes/fiber_cut_oms3.sqlite   # or the cache you use
.venv/bin/python -m diag_agent replay --scenario tests/diag_agent/scenarios/fiber_cut_oms3.yaml --cache-mode read_write
```

## The LLM is unreachable

```text
LLMUnavailable: ...
```

or `llm-check` prints `LLM not configured`.

1. Check that `.env` exists and that `VLLM_API_BASE`, `VLLM_MODEL` and `VLLM_API_KEY` are set.
2. Run `.venv/bin/python -m diag_agent llm-check` (the key is shown as `***`).
3. For a local vLLM or Ollama, confirm the process is listening and use `--llm-profile local_vllm` or `ollama`.
4. To continue without an LLM: `--no-llm` or `FMA_NO_LLM=1`.

## The agent never commits on the fibre cut

Expected behaviour: one `CHK_PPE_PROFILE(och_ch2)`, then COMMIT of `FIBRE_CUT @ span_OMS3_BC` with P ≥ 0.80. If it stops at IDK:

- Confirm you are diagnosing raw incident **42** of `fiber_cut_oms3.db` (not a twin or a different fixture).
- Confirm `ppe_access` is `on_demand` (the default). In `streamed` mode the confirm rule may already be satisfied, but the posterior must still pass the other tests.
- Rebuild semantic memory (`kb build --force`) if you have edited the KB.
- Compare with the committed tree under `/tmp` after a known-good run: `.venv/bin/python -m diag_agent replay --scenario tests/diag_agent/scenarios/fiber_cut_oms3.yaml --no-llm --out /tmp/out`.

## `incident-resolve` returns `probable-cause-unresolved`

The RPC refuses to resolve when the decision was not a commit. Inspect the 90% set:

```bash
.venv/bin/python -m diag_agent notifications --full --mode extended | python -m json.tool | head
.venv/bin/python -m diag_agent tree <incident-no> --tree-view decision
```

This is intentional: the server will not close an incident whose cause was never committed.

## Repeated acknowledge

```text
reason: ietf-incident:repeated-acknowledge
```

The incident is already acknowledged. The scenario's client script acknowledges at 09:35; a second call from curl is expected to fail.

## Two cassette runs are not byte-identical

Acceptance criterion A8 requires identical `tree.json` and ledger history for two cassette runs. Differences usually come from:

- wall-clock timestamps leaking into a node label (they must not);
- a non-deterministic sort of equal-priority candidates (report it as a bug);
- running with `--cache-mode read_write` against a live endpoint, whose answers can vary.

Always compare with `--cache-mode read_only` against the committed cassette.

## Label leakage test fails

The substrate must never select `fault_type`, `fault_target`, `t_offset_ms`, `group_id` or `is_root = 1` rows. `is_root = 0` (detector rows only) is allowed: that is how the agent reads the PPE anomalies a real detector would emit. If a new query breaks the allowlist in `environment/substrate/allowlist.py`, the A7 test fails and the query must be fixed.

## The Incident Server binds to a busy port

```text
ERROR: [Errno 98] Address already in use
```

```bash
.venv/bin/python -m diag_agent serve --port 8041
# or find and stop the previous server:
pkill -f "diag_agent serve"
```

## Slow first run

The first run after a clean checkout builds semantic memory (~1 s) and may download nothing else. A no-LLM fibre-cut replay takes about 5–8 s; a live DeepSeek fibre-cut takes about 30–40 s (about 5 LLM calls). Subsequent no-LLM runs reuse the loaded semantic memory inside the same process, but each new process loads it again.

## Where to look next

| Symptom | File / command |
|---|---|
| Wrong hypothesis set | Trace view (`--tree-view trace`), then the KB effects in `diag_agent/semantic_memory/kb/kb-fault-modes.ttl` |
| Wrong PPE classification | `classify_trace` in `procedural_memory/reasoning/windows.py`; the PPE rows of the fixture |
| Invalid payload | `--validate always` and the examples under `docs/design/examples/` |
| Ownership / state bug | `tests/diag_agent/test_ownership.py` and `working_memory/state.py` (`OWNERSHIP`) |
| Design intent | [`docs/design/`](../design/README.md) |
