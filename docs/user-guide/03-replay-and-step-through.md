# 3. Replay and step-through

`replay` diagnoses one incident from a frozen simulator database. The substrate exposes only what a real management system would see:

- an alarm becomes visible at its *observed* time;
- a PPE trace becomes visible when it is polled or read by a check.

The fault labels (`fault_type`, `fault_target`, the planted PPE anomaly rows) cannot be read by the agent. Only the scorer and the simulated field technician may read them, and every read is logged.

## 3.1 Choosing the incident

You can give a scenario, or a database and an incident id:

```bash
# A scenario file (database, incident, LLM cassette, episodic memory, OSS client script, repair time)
.venv/bin/python -m diag_agent replay --scenario tests/diag_agent/scenarios/fiber_cut_oms3.yaml

# Any database and raw incident id
.venv/bin/python -m diag_agent replay --db tests/diag_agent/fixtures/variants.db --incident 5 --no-llm
```

The second form uses the defaults from `config/diag_agent.yaml`: the episodic memory at `data/diag_agent/episodes.sqlite`, the live LLM profile, no client script and no repair. Flags given on the command line override the scenario.

### List the incidents in a database

```bash
.venv/bin/python -c "from diag_agent.environment.substrate.simulator_db import list_incidents as l; \
  [print(r) for r in l('tests/diag_agent/fixtures/variants.db', suppressed=None)]"
```

Each trigger is stored twice:

- a **raw** incident, with every alarm;
- a **suppressed** twin with the same `created_at`, with the alarms that the simulator's suppression rules keep.

Use `--view suppressed` to diagnose the suppressed twin of a raw id.

## 3.2 Options

| Option | Meaning |
|---|---|
| `--scenario FILE` | Scenario YAML (see §3.5) |
| `--db FILE`, `--incident N` | Frozen database and raw incident id |
| `--view raw\|suppressed` | Which alarm view to diagnose (default `raw`) |
| `--ppe on_demand\|streamed` | `on_demand` (default): a PPE trace is read only by a `CHK_PPE_PROFILE` check. `streamed`: every polled trace is visible as soon as it is observed. |
| `--no-llm` | Run without any LLM. The LLM line abstains; the narrator and the orchestrator fall back to templates. Setting `FMA_NO_LLM=1` has the same effect. |
| `--llm-profile NAME` | `env`, `deepseek`, `local_vllm`, `ollama` or `none` ([LLM configuration](04-llm-configuration.md)) |
| `--cache-mode read_write\|read_only\|off` | LLM cassette mode. `read_only` never calls the network. |
| `--cache-path FILE` | LLM cassette file |
| `--episodic FILE` | Episodic memory to use. Without this flag, a scenario's episodic fixture is copied to a temporary file, so the fixture is never modified. |
| `--cold` | Start with an empty episodic memory (the analogy line abstains) |
| `--store FILE` | Incident store (default `data/diag_agent/incidents.sqlite`) |
| `--validate always\|debug\|sample\|off` | YANG validation of every published payload (default `debug`, which validates all payloads) |
| `--checkpoints FILE` | Keep LangGraph checkpoints in this SQLite file (thread id `replay-<raw>-<run>`) |
| `--no-learn` | Do not write the episode or the incident graph at closure |
| `--field-seed N` | Seed of the simulated field technician, used when a field check is chosen |
| `--step` | Interactive step-through (§3.3) |
| `--watch` | Print every tree node as it is created |
| `--tree-view decision\|trace` | Which view to print at the end (default `decision`) |
| `--format ascii\|mermaid\|json` | Output format of the printed tree |
| `--score` | Compare the result with the hidden labels (harness only) |
| `--out DIR` | Write artefacts to `DIR` (§3.4) |

## 3.3 Step-through

```bash
.venv/bin/python -m diag_agent replay --scenario tests/diag_agent/scenarios/fiber_cut_oms3.yaml --no-llm --step
```

The graph pauses after each major node (frame, pool, evaluate, synthesize, decide, plan, execute_check, update_incident, await_closure). Every new tree node and the state slots written by each node are printed:

```text
step-through: Enter = next pause, c = run to the end, q = quit
[ingest] wrote clock, obs_log
  + n01 [root] Incident — what is the root cause of Loss of signal on span_OMS3_BC?
  + n02 [frame] frame: onset [-0.4, +0.4] s · 8 items · 2 services · 8 shared · 36 quiet
[frame] wrote cycle, frame, mode, pass_no, tree
-- paused before raise_incident >
[raise_incident] wrote closure, incident, incident_no, task, tree
[retrieve_episodes] wrote line_results
[line_walk] wrote line_results
...
  + n18 [analogy] analogy: episode ep-03a03179d2e3 (incident 1002) similarity 0.824 weight 1.0 → FIBRE_CUT@span_OMS3_BC
```

At the prompt:

- **Enter** runs to the next pause;
- **c** runs to the end without pausing;
- **q** stops, and the tree so far is printed.

The four generation lines run in parallel (LangGraph `Send`), so their order in the output can vary.

## 3.4 Artefacts (`--out DIR`)

| File | Content |
|---|---|
| `tree.json` | All tree nodes (trace view), canonical JSON. Two runs from the same cassette produce byte-identical files. |
| `tree-decision.txt`, `tree-trace.txt` | ASCII renderings of both views |
| `tree-decision.mmd` | Mermaid flowchart of the decision view (paste into any Mermaid viewer) |
| `ledger.json` | The final ledger: prior, terms (`T_doc`, `T_gc`, `T_ppe`, `T_gc_ppe`, `T_check`), posterior, NOTA, entropy and the per-cycle history |
| `notifications.json` | Every notification of the incident, in strict and extended form |
| `score.json` | With `--score`: correctness, whether the truth was in the 90% set, P(truth), position error |

## 3.5 Scenario files

```yaml
incident_db: tests/diag_agent/fixtures/fiber_cut_oms3.db
raw_incident_id: 42
view: raw
ppe_access: on_demand
llm: { profile: env, cache: { mode: read_only, path: tests/diag_agent/cassettes/fiber_cut_oms3.sqlite } }
episodic: tests/diag_agent/fixtures/episodes_warm.sqlite
client_script:                      # the OSS side, driven by the replay clock
  - { at: "+40s",       rpc: incident-diagnose,    input: { ticket-no: TT-20261102-0042 } }
  - { at: "09:35:00Z",  rpc: incident-acknowledge, input: { ticket-no: TT-20261102-0042 } }
  - { at: "10:40:00Z",  rpc: incident-resolve,     input: { ticket-no: TT-20261102-0042 } }
repair: { at: "10:52:10Z", clears: root_symptoms, note: "break found 7.0 km from B, spliced" }
```

- Paths are relative to the repository root.
- `at` is either an offset from the incident's `created_at` (`+40s`, `+5m`, `+1h`) or a UTC time of day on the incident's date (`HH:MM:SSZ`).
- The client script runs against the same Incident Server API that HTTP clients use, so RPC errors (for example `probable-cause-unresolved`) are recorded exactly as a client would receive them.
- `repair.at` is when the root symptoms clear. Without it, the incident stays `updated` after the diagnosis.

## 3.6 The replay clock

The agent's clock is the replay clock, in seconds since the incident's `created_at`, and never the wall clock. Waiting for an alarm window to close (`CHK_WAIT`) or for a one-minute PPE read only advances the replay clock. A replay therefore takes seconds even though the incident spans 80 minutes. All times in the tree, ledger and notifications are replay times, which keeps runs reproducible.
