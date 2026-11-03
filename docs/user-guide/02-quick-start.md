# 2. Quick start: diagnose the fibre cut

The repository ships a frozen incident: a fibre cut on `span_OMS3_BC`, 7 km from NE B. It is recorded as raw incident 42 in `tests/diag_agent/fixtures/fiber_cut_oms3.db`. The scenario file `tests/diag_agent/scenarios/fiber_cut_oms3.yaml` ties together:

- the database;
- the recorded LLM answers (a *cassette*);
- a warm episodic memory of 11 past incidents;
- a short OSS client script, which sends diagnose, acknowledge and resolve requests;
- the time of the repair.

## 2.1 Run it

```bash
.venv/bin/python -m diag_agent replay --scenario tests/diag_agent/scenarios/fiber_cut_oms3.yaml
```

This uses the LLM answers recorded in the cassette, so it needs no network access and produces the same output every time. It runs in about 10 s. To run it without any LLM, add `--no-llm`. To call the live LLM configured in `.env`, add `--cache-mode read_write`; see [LLM configuration](04-llm-configuration.md).

## 2.2 What you see

The command prints the decision view of the diagnosis tree, followed by a summary:

```text
Incident 1001 — what is the root cause of Loss of signal on span_OMS3_BC?
├── Q R3: At least 70% of relevant symptoms are explained? ─ violated ─▶ H2, H3, H5, H6 out (IG 0.80 bit)
├── Q R2: Every observed service-down is predicted by the hypothesis? ─ violated ─▶ H2, H3, H6 out (IG 0.76 bit)
├── Q LossOfSignal at B.FIU.line_from_C (window closed +7.4 s)? ─ no ─▶ H4 out (IG 0.42 bit)
├── Q LossOfSignal at A.FIU.line_from_B (window closed +7.4 s)? ─ no ─▶ H5, H6 out (IG 0.38 bit)
└── Q [check] CHK_PPE_PROFILE(och_ch2): 0.0858 bits, 1 min, 5.1 bits/h   ✔ done +92.4 s
    └── [a] step-hard@span_OMS3_BC -30.0 dB step 7.0 km into span_OMS3_BC (z = 107.0 km) (0.9863: H1)  ✔
        ├── Q R7: A located PPE step/slope lies where the hypothesis says? ─ violated ─▶ H2, H3, H4, H5, H6 out
        ├── Q PPE profile of och_ch2 as predicted? ─ step-hard@span_OMS3_BC ─▶ H2, H3 out (IG 0.73 bit)
        └── ◆ COMMIT FIBRE_CUT @ span_OMS3_BC, 7.0 ± 0.4 km   P = 0.99691  NOTA = 0.00297
            └── ✓ cleared +4930 s: repair — break found 7.0 km from B, spliced (FIBRE_CUT @ span_OMS3_BC)

incident 1001  decision: commit  clock +4930.0 s  cycles 2
committed: FIBRE_CUT @ span_OMS3_BC, 7.0 ± 0.4 km from B  P = 0.99691  NOTA = 0.00297
checks: CHK_PPE_PROFILE(och_ch2)  effort 1.0 min  LLM calls 5  tokens 16768
detail: The diagnosed cause is Fibre cut (one direction) at span_OMS3_BC on B->C, ...
closure: cleared at +4930 s  verification: repair_confirmed
```

Read it as follows:

1. **Frame (+32.4 s).** The first alarm arrives at +0.37 s. The agent holds for 32 s, then folds the 53 alarms seen so far into 8 symptoms, 2 affected services and 8 shared elements.
2. **Generation.** The walk, correlation, analogy and LLM lines each propose hypotheses. Pooling them gives six hypotheses: the fibre cut, plus look-alikes such as an amplifier failure, a section cut or an NE power loss.
3. **Questions answered by the data.** Each `Q` line is a prediction tested against the alarms. For example, "LossOfSignal at A.FIU.line_from_B? no" rules out the hypotheses that would have darkened that receiver.
4. **One check.** The leader's Required PPE prediction can be tested remotely in one minute, so the agent reads the PPE profile of `och_ch2`. The trace shows a −30 dB step 7.0 km into the span, exactly where the hypothesis predicted it.
5. **Commit.** P = 0.997 ≥ 0.80, NOTA ("none of these") < 0.20, and no Required prediction is pending, so the agent commits.
6. **Closure.** The scripted OSS client acknowledges at 09:35 and asks to resolve at 10:40. The repair at 10:52:10 clears the root alarms, the incident is `cleared`, and the episode is stored in episodic memory.

## 2.3 See the published incident

Each run writes to the incident store (`data/diag_agent/incidents.sqlite` by default):

```bash
.venv/bin/python -m diag_agent incidents
.venv/bin/python -m diag_agent notifications
.venv/bin/python -m diag_agent notifications 1001 --full --mode strict   # full ietf-incident JSON
```

```text
#1 2026-11-02T09:30:32.372Z 1001 raised   raised  LossOfSignal (OTS) at C.FIU.line_from_B on span_OMS3_BC; 8 frame items ...
#2 2026-11-02T09:30:40.000Z 1001 updated  updated  LossOfSignal (OTS) at C.FIU.line_from_B on span_OMS3_BC; 8 frame items ...
#3 2026-11-02T09:31:32.372Z 1001 updated  updated  Fibre cut (one direction) on span_OMS3_BC (B->C), 7.0 +/- 0.4 km from B ...
#4 2026-11-02T09:31:32.372Z 1001 task     diagnosed
#5 2026-11-02T09:31:32.372Z 1001 task     finished
#6 2026-11-02T09:35:00.000Z 1001 updated  updated  Fibre cut (one direction) on span_OMS3_BC (B->C), 7.0 +/- 0.4 km from B ...
#7 2026-11-02T10:52:10.000Z 1001 cleared  cleared  Fibre cut (one direction) on span_OMS3_BC (B->C), 7.0 +/- 0.4 km from B ...
```

Each notification has a cause:

| # | Cause |
|---|---|
| 1 | The incident is raised at frame time |
| 2 | The client's `incident-diagnose` attaches ticket `TT-20261102-0042` at +40 s |
| 3 | The diagnosis commits |
| 4, 5 | The diagnosis task is reported as diagnosed, then as finished |
| 6 | The client acknowledges the incident at 09:35 |
| 7 | The repair clears the root alarms at 10:52:10 |

Every payload is validated against the `ietf-incident` YANG module, which is extracted from `references/draft-ietf-nmop-network-incident-yang-20.txt`, before it is stored.

## 2.4 Next steps

- Watch the reasoning step by step with `--step` ([Replay and step-through](03-replay-and-step-through.md)).
- Serve the incident over RESTCONF and SSE with `serve` ([Incident Server](05-incident-server.md)).
- Score the agent on the other fault types with `score` ([Testing and scoring](07-testing-and-scoring.md)).
