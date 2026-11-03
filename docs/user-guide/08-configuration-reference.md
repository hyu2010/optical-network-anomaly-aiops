# 8. Configuration reference

Settings are loaded with this precedence (highest first):

1. CLI flags (`--llm-profile`, `--ppe`, `--validate`, ...)
2. Process environment (`DIAG_LLM_PROFILE`, `DIAG__AGENT__LEDGER__COMMIT_P`, ...)
3. `.env` (never overrides a variable already set in the process environment)
4. `config/diag_agent.yaml`

`${VAR:-default}` placeholders in the YAML are interpolated after `.env` is loaded. Nested keys can be overridden with double-underscore environment variables: `DIAG__AGENT__LEDGER__COMMIT_P=0.85` sets `agent.ledger.commit_p`.

## 8.1 File locations

| Path | Role |
|---|---|
| `config/diag_agent.yaml` | Default configuration |
| `.env` | Secrets and endpoint URLs (git-ignored) |
| `data/yang/*.json` | Test-bed topology |
| `data/alarm_*.json` | Alarm catalogue |
| `data/diag_agent/semantic.trig` | Built semantic memory (regenerable) |
| `data/diag_agent/yang/diag-agent-alarm-types.yang` | Generated alarm-type identities |
| `data/diag_agent/episodes.sqlite` | Default episodic memory |
| `data/diag_agent/incidents.sqlite` | Default incident store |
| `data/diag_agent/llm_cache.sqlite` | Default LLM cassette |
| `data/diag_agent/checkpoints.sqlite` | Optional LangGraph checkpoints |
| `diag_agent/semantic_memory/kb/kb-fault-modes.ttl` | Fault-mode knowledge base |
| `diag_agent/semantic_memory/ontology/*.ttl` | Ontology (RFC 9940 + optical + diagnosis) |
| `diag_agent/procedural_memory/prompts/*.md` | LLM prompts (proposer, orchestrator, narrator) |
| `diag_agent/incident_server/yang/*.yang` | Extension YANG modules |

`data/diag_agent/` is regenerable and git-ignored. The committed fixtures and cassettes live under `tests/diag_agent/`.

## 8.2 `llm`

| Key | Default | Meaning |
|---|---|---|
| `profile` | `${DIAG_LLM_PROFILE:-env}` | Active profile name |
| `roles.<role>.temperature`, `max_tokens` | per role | Sampling parameters |
| `roles.interface.profile` | `null` | Optional override of the profile for the interface role |
| `profiles.<name>` | see YAML | `base_url`, `model`, `api_key`, `structured_output`, `tools`, `timeout_s`, `max_retries`, optional `extra_body` |
| `cache.enabled` | `true` | Master switch for the cassette |
| `cache.path` | `data/diag_agent/llm_cache.sqlite` | Cassette file |
| `cache.mode` | `read_write` | `read_write` \| `read_only` \| `off` |

## 8.3 `agent`

| Key | Default | Meaning |
|---|---|---|
| `lines` | `[walk, correlation, analogy, llm]` | Generation lines to run |
| `line_weights` | all 1.0, specialised 0.25 | Weights of the linear opinion pool |
| `frame.hold_s` | `32` | Seconds to hold after the first alarm before building the frame |
| `frame.view` | `raw` | Default alarm view |
| `frame.scope_hops` | `2` | Topology hops that define the frame's scope |
| `ppe_access` | `on_demand` | `on_demand` \| `streamed` |
| `ledger.commit_p` | `0.80` | Minimum leader posterior to commit |
| `ledger.nota_max` | `0.20` | Maximum NOTA to commit |
| `ledger.residual_nats` | `4.5` | Largest single debit allowed on the leader |
| `ledger.set_mass` | `0.90` | Mass of the reported 90% set |
| `ledger.regenerate_nota` | `0.25` | NOTA above which the agent regenerates |
| `ledger.stall_bits`, `stall_terms` | `0.10`, `3` | Entropy stall that triggers regeneration |
| `ledger.confirm_remote_h` | `0.1` | A Required prediction testable remotely within this many hours is tested before commit |
| `ledger.clip_nats` | `8` | Per-term clip below the best value |
| `advisor.gate_hours`, `gate_p` | `0.5`, `0.05` | A check that costs this much must have an answer with P ≥ `gate_p` that would commit |
| `advisor.site_hours`, `interrupt_hours` | `2`, `1` | Effort accounting for a new site visit and for interrupting a running check |
| `advisor.max_checks`, `max_effort_h`, `max_returns` | `12`, `8`, `3` | Hard limits |
| `advisor.forward_depth`, `forward_min_mass` | `2`, `0.01` | Depth of the forward branch enumeration |
| `proposer.max_tool_calls`, `max_neighbors` | `8`, `30` | ReAct budget of the LLM proposer |
| `orchestrator`, `narrator` | `true` | Enable the LLM roles (templates are used when false or when the LLM is unavailable) |

## 8.4 `substrate`, `memory`, `incident_server`

| Key | Default | Meaning |
|---|---|---|
| `substrate.incidents_db` | `${FMA_INCIDENTS_DB:-data/incidents.db}` | Default live incidents database |
| `substrate.yang_dir` | `${FMA_YANG_DIR:-data/yang}` | Topology directory |
| `substrate.catalog_dir` | `data` | Directory that holds `alarm_*.json` |
| `memory.semantic.path` | `data/diag_agent/semantic.trig` | Built semantic memory |
| `memory.semantic.kb_release` | `kb-2026.11.0` | Named graph of the knowledge base |
| `memory.episodic.path` | `data/diag_agent/episodes.sqlite` | Default episodic memory |
| `memory.episodic.k` | `20` | Retrieval depth of the analogy line |
| `memory.checkpoints` | `data/diag_agent/checkpoints.sqlite` | Default checkpoint file |
| `incident_server.host`, `port` | `127.0.0.1`, `8040` | Bind address |
| `incident_server.payload_mode` | `extended` | `strict` \| `extended` |
| `incident_server.validate` | `debug` | YANG validation policy |
| `incident_server.store` | `data/diag_agent/incidents.sqlite` | Incident store |
| `incident_server.incident_no_start` | `1001` | First incident number |
| `incident_server.tokens` | `{}` | Bearer token → rights; empty means no auth |
| `incident_server.network_refs` | `{optical: optical-topo, otn: otn-topo}` | Network refs used in `probable-causes` |

## 8.5 Useful environment variables

| Variable | Effect |
|---|---|
| `DIAG_CONFIG` | Path of an alternative YAML config |
| `DIAG_LLM_PROFILE` | Active LLM profile |
| `VLLM_API_BASE`, `VLLM_MODEL`, `VLLM_API_KEY` | Endpoint used by the `env` (and, as a fallback, `deepseek` / `local_vllm`) profiles |
| `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL` | Overrides for the `deepseek` profile |
| `FMA_NO_LLM=1` | Same as `--no-llm` |
| `FMA_YANG_DIR`, `FMA_INCIDENTS_DB` | Topology and live incidents database |
| `DIAG_LIVE_LLM=1` | Enable the live LLM pytest marks |
| `DIAG__...` | Nested YAML override (for example `DIAG__AGENT__LEDGER__COMMIT_P=0.85`) |
