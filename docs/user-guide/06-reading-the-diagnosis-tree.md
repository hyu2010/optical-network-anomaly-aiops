# 6. Reading the diagnosis tree

Every diagnosis produces a tree. The tree has two views and three formats. The views are defined in design document 04 §9.

## 6.1 The two views

| View | What it shows |
|---|---|
| **decision** (default) | The question / check / answer path that led to the decision, plus the decision and verification nodes. Generation, evaluation and synthesis are hidden. |
| **trace** | Every node, in creation order, nested by parent. Use this to audit the generation lines, pruned candidates and ledger terms. |

```bash
.venv/bin/python -m diag_agent replay --scenario tests/diag_agent/scenarios/fiber_cut_oms3.yaml --no-llm --tree-view decision
.venv/bin/python -m diag_agent replay ... --tree-view trace --format mermaid
.venv/bin/python -m diag_agent tree 1001 --tree-view trace --format json
```

## 6.2 Node kinds

| Kind | Meaning |
|---|---|
| `root` | The diagnosis question |
| `frame` | Onset window, number of items / services / shared elements / quiet instruments |
| `generate` | One generation line (walk, correlation, analogy or llm) and its distribution |
| `pruned` | A candidate that failed the preview or a quiet-instrument filter (drawn dashed) |
| `analogy` | A retrieved episode and the conclusion it transferred |
| `hypothesis` | A pooled hypothesis and its prior |
| `evaluate` | Per-hypothesis MATCHED / MISSING / PENDING counts at a given clock |
| `question` | A prediction answered by the data (a rule violation or a closed window) |
| `check` | A planned remote or field check, with information gain and effort |
| `answer` | One possible answer of a check. The taken branch is solid; the others are dashed. |
| `synthesis` | Posterior, NOTA and entropy after one decision cycle |
| `decision` | COMMIT / IDK / LIMIT, with the leader and its fitted parameters |
| `verify` | Closure: repair confirmed, self-verified, or rejected |

## 6.3 Reading the decision view

```text
Incident 1001 — what is the root cause of Loss of signal on span_OMS3_BC?
├── Q R3: At least 70% of relevant symptoms are explained? ─ violated ─▶ H2, H3, H5, H6 out (IG 0.80 bit)
├── Q LossOfSignal at B.FIU.line_from_C (window closed +7.4 s)? ─ no ─▶ H4 out (IG 0.42 bit)
└── Q [check] CHK_PPE_PROFILE(och_ch2): 0.0858 bits, 1 min, 5.1 bits/h   ✔ done +92.4 s
    └── [a] step-hard@span_OMS3_BC -30.0 dB step 7.0 km into span_OMS3_BC (z = 107.0 km) (0.9863: H1)  ✔
        ├── Q R7: A located PPE step/slope lies where the hypothesis says? ─ violated ─▶ H2, H3, H4, H5, H6 out
        └── ◆ COMMIT FIBRE_CUT @ span_OMS3_BC, 7.0 ± 0.4 km   P = 0.99691  NOTA = 0.00297
            └── ✓ cleared +4930 s: repair — break found 7.0 km from B, spliced (FIBRE_CUT @ span_OMS3_BC)
```

- Each `Q` without `[check]` is a prediction tested against alarms already in hand. `IG` is the information gain of the answer, in bits.
- `R2`, `R3`, `R6`, `R7` are the global consistency rules of the knowledge base (see design document 04 §6).
- A check line reports the mutual information, the effort and the rate (bits per hour). Untaken answer branches appear as dashed lines in the trace view and in Mermaid.
- The decision reports the mode, the location, the fitted position (when PPE gave one), P and NOTA.

## 6.4 Formats

| Format | Use |
|---|---|
| `ascii` | Terminal and logs |
| `mermaid` | Paste into any Mermaid viewer or a Markdown preview |
| `json` | Canonical JSON of the nodes (keys sorted). Two cassette runs produce byte-identical files (acceptance criterion A8). |

With `--out DIR`, all three are written: `tree-decision.txt`, `tree-trace.txt`, `tree-decision.mmd` and `tree.json`.

## 6.5 The ledger

```bash
.venv/bin/python -m diag_agent replay ... --out /tmp/out
python -m json.tool /tmp/out/ledger.json | head -60
# or, against a running Incident Server:
curl -s http://127.0.0.1:8040/diag/v1/incidents/1001/ledger | python -m json.tool | head
```

| Field | Meaning |
|---|---|
| `prior` | The pooled prior of the generation lines, plus NOTA |
| `terms` | The evidence terms: `T_doc`, `T_gc` (rules R1–R6), `T_ppe`, `T_gc_ppe` (R7), `T_check`. Each entry holds the per-hypothesis debit (nats) and the tau weight. |
| `post` | The posterior after all terms |
| `ranked`, `leader`, `runner_up` | Hypothesis ids in posterior order |
| `nota` | Probability of "none of these" |
| `entropy`, `entropy_prior` | Entropy of the posterior and of the prior, in bits |
| `history` | One entry per decision cycle: leader, P, NOTA, entropy, and whether a non-wait check was answered |

A term with a large negative debit is what eliminated a look-alike. In the fibre-cut case, the quiet receivers at A, D and B drive R2/R3; the PPE step drives R7 and `T_ppe`.
