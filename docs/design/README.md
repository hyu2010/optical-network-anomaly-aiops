# diag_agent — Design Documents

`diag_agent` is an agentic AI that diagnoses incidents in the 6-node optical test bed (`simulator/`). Its main outputs are:

- a **decision procedure**: a diagnosis tree that shows how the root cause was reached;
- **ietf-incident** JSON payloads, so that it can act as an Incident Server (draft-ietf-nmop-network-incident-yang-20).

Status: **implemented**. The package lives under `diag_agent/`. How to install and run it is in [`docs/user-guide/`](../user-guide/README.md).

## Reading order

| # | Document | What it settles |
|---|---|---|
| 01 | [Overview and requirements](01_overview_and_requirements.md) | Scope, design principles, the test bed as `diag_agent` sees it, where each requirement is met |
| 02 | [CoALA architecture](02_coala_architecture.md) | The CoALA Figure-4 modules (working, procedural, semantic, episodic memory; internal and external actions; decision cycle), their interfaces and the package layout |
| 03 | [Knowledge graph (RFC 9940)](03_knowledge_graph_rfc9940.md) | The RDF/OWL schema layers, the mapping from YANG and the alarm catalogue to RDF, bitemporal named graphs, and what is stored in semantic memory |
| 04 | [Decision procedure and diagnosis tree](04_decision_procedure.md) | How the tree is formed: KG-walk-guardrailed LLM proposals, winnowing, analogy, the Bayesian ledger, the stopping rules and the tree display |
| 05 | [LangGraph implementation and LLM configuration](05_langgraph_and_llm_config.md) | State schema, nodes and edges, checkpointing, streaming, human-in-the-loop, and config-file LLM switching between DeepSeek and local vLLM |
| 06 | [Incident server (ietf-incident)](06_incident_server.md) | Incident identification, lifecycle, the RPCs and their errors, notifications, the payload mapping, and the extension YANG module |
| 07 | [Test case: fibre cut on span_OMS3_BC](07_test_case_fiber_cut.md) | An end-to-end worked diagnosis: reasoning steps, tree growth, ledger arithmetic, the JSON payloads and acceptance criteria |

## Design artefacts beside the documents

| Path | Content |
|---|---|
| `ontology/onm-rfc9940.ttl` | Upper ontology: the RFC 9940 terms and relationships as OWL |
| `ontology/optx-optical.ttl` | Optical-domain layer aligned with the simulator's YANG (RFC 8345, TE, WSON, OTN, inventory) |
| `ontology/dx-diagnosis.ttl` | Diagnostic-knowledge layer: fault modes, effects, location terms, checks, rules, hypotheses and the tree |
| `ontology/kb-fault-modes.ttl` | Populated documentation: 13 fault modes covering the 11 simulator faults plus SECTION_CUT and the documented-only AMP_FAIL, with their effects, cascade rules, location terms, alarm types, checks and consistency rules |
| `ontology/example-lab-mesh.trig` | Populated instances (a lab-mesh excerpt plus the fibre-cut case) in bitemporal named graphs |
| `yang/diag-agent-incident-ext.yang` | Extension module (prefix `dax`): optical cause identities, the PPE event type, `ticket-no` and the diagnosis-task list (draft A.3/A.4), and the diagnosis summary with a tree reference |
| `yang/diag-agent-alarm-types.yang` | RFC 8632 `alarm-type-id` identities for the test bed's alarm names. This is an excerpt; the full module is generated from the catalogue. |
| `examples/*.json` | The 11 ietf-incident payloads of the test case (doc 07 §6), in RFC 7951 JSON |
| `examples/validate.py` | Validates the examples with `yangson` against `ietf-incident` (extracted from the draft) and the extension modules |

**Checks already run on these artefacts:**

- All ontology files parse with `rdflib`, and no term is undeclared.
- The rule queries compile, and the walk patterns resolve on the example network.
- Both YANG modules pass `pyang --strict`.
- All examples pass `validate.py`.

**Version control.** The repository `.gitignore` ignores `*.ttl`. An exception, `!docs/design/ontology/*.ttl`, has been added so that the ontology files are committed.

## Sources these documents build on

- **Domain methodology**: `anomaly-diagnosis-agent/docs`, the Janz test bed (case frame, generation lines, ledger, winnowing, advisor, outcome scoring) and its measured lessons.
- **Architecture**: `references/Cognitive-Architectures-for-LLM-agents.pdf` (CoALA), Figure 4.
- **Terminology**: `references/rfc9940.txt`.
- **Incident model**: `references/draft-ietf-nmop-network-incident-yang-20.txt`.
- **Test bed**: `simulator/Design/02–12`, `simulator/user_guide/`, and the simulator code (`simulator/simulator/*.py`, `rules/`, `kg/`, `kg_engine/`).
