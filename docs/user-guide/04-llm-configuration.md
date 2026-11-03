# 4. LLM configuration

The LLM is optional. It plays three roles:

| Role | What it does | Without an LLM |
|---|---|---|
| `proposer` | One of the four generation lines. It is a ReAct loop with read-only knowledge-graph tools (`frame_get`, `kg_term`, `kg_modes`, `kg_effects`, `kg_preview`, `kg_neighbors`), and it returns a distribution over (fault mode, location) pairs. | The line abstains; the walk, correlation and analogy lines still run. |
| `orchestrator` | Picks one of the top 3 admissible checks and gives a rationale | The check with the best bits per hour is taken |
| `narrator` | Writes the incident `detail` text from fixed slots | A template is used |

Guardrails apply to everything the LLM produces:

- **Proposer.** Every proposal must name a documented fault mode and a location that resolves through a knowledge-graph walk the proposer actually made. Proposals that fail are pruned and appear in the trace view as `pruned` nodes.
- **Narrator.** Text is rejected if it contains a number, identifier or slot name that is not in its input slots; the template is used instead.
- **Orchestrator.** It can only choose among admissible checks.

The LLM never decides the outcome. The commit tests run on the ledger alone.

## 4.1 The `.env` file

Settings are read from `config/diag_agent.yaml`, which interpolates environment variables. The `.env` file at the repository root is loaded first, but it never overrides a variable that is already set in your shell.

```bash
cp .env.example .env      # then edit .env
```

```dotenv
VLLM_API_BASE=https://api.deepseek.com/v1
VLLM_MODEL=deepseek-v4-flash
VLLM_API_KEY=sk-...your key...
```

The default profile, `env`, uses exactly these three variables, so this is all that is needed to use DeepSeek.

**Keeping the key safe:**

- `.env` is git-ignored.
- The agent never logs or prints the key. `llm-check` and `LLMProfile.redacted()` show it as `***`.
- Cassette files store requests and responses, not the key.
- Do not paste `.env` into issues or chats.

## 4.2 Profiles

The `llm.profile` setting selects one of these profiles. Set it in the YAML, with `DIAG_LLM_PROFILE=<name>`, or with `--llm-profile <name>`.

| Profile | Endpoint | Notes |
|---|---|---|
| `env` (default) | `VLLM_API_BASE`, `VLLM_MODEL`, `VLLM_API_KEY` | The `.env.example` convention. Works for DeepSeek and any OpenAI-compatible server. |
| `deepseek` | `https://api.deepseek.com/v1` | Model `DEEPSEEK_MODEL` (default `deepseek-v4-flash`). Key `DEEPSEEK_API_KEY`, falling back to `VLLM_API_KEY`. |
| `local_vllm` | `VLLM_API_BASE` (default `http://192.168.198.141:8000/v1`) | Uses `json_schema` structured output and turns off Qwen's thinking mode |
| `ollama` | `http://localhost:11434/v1` | Model `qwen3:8b`, with prompted tool calls |
| `none` | – | No LLM (the same as `--no-llm`) |

Per-role parameters (temperature, max_tokens) are under `llm.roles` in the YAML.

### Using a local vLLM

```dotenv
VLLM_API_BASE=http://192.168.198.141:8000/v1
VLLM_MODEL=qwen3.5-9B-awq-4bit
VLLM_API_KEY=test123
```

Then run with `--llm-profile local_vllm`, or keep `env` if you replaced the three DeepSeek lines.

### Using Ollama

```bash
ollama pull qwen3:8b
.venv/bin/python -m diag_agent replay --scenario tests/diag_agent/scenarios/fiber_cut_oms3.yaml \
    --llm-profile ollama --cache-mode off
```

## 4.3 The LLM cassette (cache)

Each LLM request is keyed by a hash of the model, the role, the messages, the tools and the parameters. The response is stored in an SQLite *cassette*.

| Mode | Behaviour |
|---|---|
| `read_write` (config default) | Returns the cached answer if present; otherwise calls the endpoint and records the answer |
| `read_only` (scenario default) | Never calls the network. A cache miss makes the LLM line abstain (`CassetteMiss`), and the run continues without it. |
| `off` | Always calls the endpoint and records nothing |

The default cassette is `data/diag_agent/llm_cache.sqlite`. The fibre-cut scenario uses its own committed cassette, `tests/diag_agent/cassettes/fiber_cut_oms3.sqlite`, in `read_only` mode. This makes the acceptance test deterministic and lets it run offline.

### Re-recording the scenario cassette

After changing a prompt, the knowledge base or the frame logic, the recorded requests no longer match. Re-record the cassette with the live endpoint:

```bash
rm tests/diag_agent/cassettes/fiber_cut_oms3.sqlite
.venv/bin/python -m diag_agent replay --scenario tests/diag_agent/scenarios/fiber_cut_oms3.yaml --cache-mode read_write
```

One fibre-cut run makes about 5 LLM calls (about 17k tokens) and takes about 30–40 s with `deepseek-v4-flash`.

## 4.4 Running without an LLM

```bash
.venv/bin/python -m diag_agent replay --scenario tests/diag_agent/scenarios/fiber_cut_oms3.yaml --no-llm
FMA_NO_LLM=1 .venv/bin/python -m diag_agent score
```

The deterministic lines are enough for every fixture in the repository. On the fibre cut, the no-LLM and LLM runs commit to the same cause (acceptance criterion A9). The LLM mainly widens the set of look-alikes considered, and the tree shows how the guardrails pruned or eliminated them.
