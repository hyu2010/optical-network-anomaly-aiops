# 1. Installation

## Requirements

- Linux, macOS or WSL2. The agent was developed and tested on WSL2 (Ubuntu) with **Python 3.14**. Python 3.11 or later should work.
- About 300 MB of disk space for the virtual environment.
- Optional: an OpenAI-compatible LLM endpoint (DeepSeek, vLLM or Ollama). Without one, the agent still runs, using only its deterministic generation lines (see [LLM configuration](04-llm-configuration.md)).

## 1.1 Create the virtual environment

Run these commands from the repository root (the directory that contains `diag_agent/`, `simulator/` and `requirements.txt`):

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

The `diag_agent` section at the end of `requirements.txt` adds:

- LangGraph (with its SQLite checkpointer) and `langchain-openai`;
- rdflib, for the knowledge graph;
- yangson and pyang, for YANG validation of every published payload;
- `sse-starlette`, PyYAML, httpx and pytest.

> All examples in this guide use `.venv/bin/python`, so they work whether or not the environment is activated. If you activate it, plain `python` is enough.

## 1.2 Check the test-bed data

The agent needs the test bed's YANG topology and alarm catalogue:

| Path | Content |
|---|---|
| `data/yang/topology.json`, `inventory.json`, `tunnels.json` | The lab-mesh topology (6 NEs, 16 spans, 6 OCh tunnels) |
| `data/alarm_*.json` | The alarm catalogue. Copy it from `simulator/kg_lib/alarm_*.json` if it is missing. |

```bash
ls data/yang/*.json
ls data/alarm_*.json || cp simulator/kg_lib/alarm_*.json data/
```

## 1.3 Build semantic memory

Semantic memory is an RDF dataset with named graphs. It holds the ontology, the fault-mode knowledge base (`kb-2026.11.0`), the topology and the alarm catalogue. It is built once and saved to `data/diag_agent/semantic.trig` (about 1 s to load):

```bash
.venv/bin/python -m diag_agent kb build
```

Expected output:

```text
semantic memory: .../data/diag_agent/semantic.trig
  topology_id: 6a59b54cbe35
  kb_release: kb-2026.11.0
  modes: 13
  alarm_types: 1026
  checks: 11
  rules: 7
  location_terms: 13
```

The command also generates the full `diag-agent-alarm-types` YANG module (one identity per alarm name) in `data/diag_agent/yang/`. The Incident Server uses it to validate `alarm-type-id` values.

Use `kb build --force` after editing a TTL file under `diag_agent/semantic_memory/` (the ontology or `kb/kb-fault-modes.ttl`) or the topology. Any command builds semantic memory automatically if the file is missing.

## 1.4 Verify the installation

```bash
.venv/bin/python -m pytest -q
```

This runs the unit tests, the fibre-cut acceptance test (both without an LLM and from the recorded LLM cassette), the variants, the Incident Server API test and the ownership test. It takes about 30 s and needs no network access. A healthy installation reports `32 passed, 2 skipped`. The two skipped tests call a real LLM; see [Testing](07-testing-and-scoring.md#live-llm-tests).

## 1.5 Optional: the LLM key

Copy `.env.example` to `.env` and fill in the endpoint, model and key. `.env` is git-ignored. Details are in [LLM configuration](04-llm-configuration.md). To check the connection:

```bash
.venv/bin/python -m diag_agent llm-check
```

```text
profile: env {"name": "env", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-v4-flash", "api_key": "***", ...}
reply: {"ok": true} (1.0 s, {'input': 62, 'output': 20})
```
