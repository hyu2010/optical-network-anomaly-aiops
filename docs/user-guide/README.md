# diag_agent user guide

`diag_agent` is a diagnosis agent for the optical test bed. It reads the alarms and PPE (power-profile) traces of an incident, builds a diagnosis tree, and publishes its conclusion through an `ietf-incident` Incident Server. The agent follows the CoALA architecture, and its knowledge is an RFC 9940 knowledge graph. An optional LLM (DeepSeek, a local vLLM, or Ollama) proposes hypotheses, but every hypothesis is checked by a walk of the knowledge graph. The design is described in [`docs/design/`](../design/README.md).

| Guide | What it covers |
|---|---|
| [1. Installation](01-installation.md) | Python environment, dependencies, building semantic memory |
| [2. Quick start](02-quick-start.md) | Diagnose the frozen fibre-cut incident in one command |
| [3. Replay and step-through](03-replay-and-step-through.md) | Every `replay` option, scenarios, the interactive step-through, output artefacts |
| [4. LLM configuration](04-llm-configuration.md) | `.env`, profiles (DeepSeek, vLLM, Ollama), the LLM cassette, running without an LLM |
| [5. Incident Server](05-incident-server.md) | `serve`, the RESTCONF endpoints, RPCs, SSE streams, curl examples |
| [6. Reading the diagnosis tree](06-reading-the-diagnosis-tree.md) | Decision and trace views, node kinds, Mermaid and JSON output, the ledger |
| [7. Testing, scoring and fixtures](07-testing-and-scoring.md) | `pytest`, `score`, `kb conformance`, fixtures, episodic memory |
| [8. Configuration reference](08-configuration-reference.md) | `config/diag_agent.yaml`, environment overrides, file locations |
| [9. Troubleshooting](09-troubleshooting.md) | Common errors and how to fix them |

## The commands at a glance

All commands are run from the repository root:

```bash
.venv/bin/python -m diag_agent <command> [options]
```

| Command | Purpose |
|---|---|
| `kb build [--force]` | Build semantic memory (`data/diag_agent/semantic.trig`) and the alarm-type YANG module |
| `kb conformance` | Check that the knowledge base describes what the test bed actually emits |
| `replay` | Diagnose one frozen incident (from a scenario file or `--db` and `--incident`) |
| `score` | Replay many labelled incidents and score them against the hidden labels |
| `serve` | Run the Incident Server (RESTCONF + SSE), optionally replaying a scenario into it |
| `incidents`, `tree`, `notifications` | Inspect what is stored in the incident store |
| `episodes list \| build` | Inspect or rebuild episodic memory |
| `llm-check` | Send one tiny request to the configured LLM (the key is never printed) |

Run `python -m diag_agent <command> --help` for the full list of options of any command.
