# 7. Testing, scoring and fixtures

## 7.1 Run the test suite

From the repository root:

```bash
.venv/bin/python -m pytest -q
```

| Suite | What it covers |
|---|---|
| `test_units.py` | Frame / PPE classification, mutual information, synthesis, pooling, narrator validation, tree renderers |
| `test_fiber_cut.py` | Acceptance criteria A1–A9 of design document 07, both without an LLM and from the recorded cassette |
| `test_variants.py` | All 12 labelled variants: no wrong commit, accuracy floor, KB conformance of the fibre cut |
| `test_incident_api.py` | RESTCONF data, RPC errors, tree and ledger HTTP endpoints |
| `test_ownership.py` | Each LangGraph node writes only the state slots it owns |
| `test_live_llm.py` | Live endpoint smoke test (skipped by default) |

A healthy offline run reports `32 passed, 2 skipped` in about 30 s. The two skipped tests need `DIAG_LIVE_LLM=1` and a key in `.env`.

### Live LLM tests

```bash
DIAG_LIVE_LLM=1 .venv/bin/python -m pytest -q -m live
```

This sends one tiny JSON request and re-runs the fibre-cut scenario against the live endpoint. The key is never printed. Expect about 30–40 s.

## 7.2 Score many incidents

```bash
# Deterministic lines only (no network)
.venv/bin/python -m diag_agent score --no-llm

# With the configured LLM (uses the cache under data/diag_agent/llm_cache.sqlite)
.venv/bin/python -m diag_agent score --json /tmp/scores.json
```

```text
✔ #1   fiber_cut              -> commit    FIBRE_CUT @ span_OMS3_BC P 0.997  checks 1
✔ #3   fiber_attenuation_high -> commit    SPAN_LOSS_INCREASE @ span_OMS3_BC P 0.977  checks 1
✔ #5   connector_dirty        -> commit    CONNECTOR_DIRTY @ span_OMS3_BC P 0.994  checks 1
~ #7   b1_exc_high            -> idk       FEC_DEGRADATION @ otu_OMS3_BC P 0.685  checks 2
~ #9   b2_exc_high            -> idk       FEC_DEGRADATION @ otu_OMS5_DB P 0.560  checks 1
~ #11  fec_pre_cor_high       -> idk       FEC_DEGRADATION @ otu_OMS2_AB P 0.685  checks 1
✔ #13  rx_power_drift         -> commit    RX_POWER_DRIFT @ C.FIU.line_from_B P 0.819  checks 1
✔ #15  config_mismatch        -> commit    CONFIG_MISMATCH @ C.OTU.line_from_B P 0.978  checks 0
✔ #17  ne_power_off           -> verified  NE_POWER_LOSS @ B P 0.940  checks 1
✔ #19  board_power_off        -> commit    BOARD_POWER_LOSS @ B.OTU P 0.980  checks 0
✔ #21  board_power_off        -> commit    BOARD_POWER_LOSS @ B.FIU P 0.980  checks 0
✔ #23  fan_failure            -> verified  FAN_FAILURE @ E P 0.987  checks 0

correct commits 9/12, wrong commits 0/12, truth in the 90% set 12/12
```

Marks:

| Mark | Meaning |
|---|---|
| `✔` | Correct commit (mode and location match the label) |
| `~` | No commit, but the truth is in the 90% set (honest IDK) |
| `✘` | Wrong commit (the agent must never produce these on the fixtures) |

The three soft bit-error faults (`b1_exc_high`, `b2_exc_high`, `fec_pre_cor_high`) share almost the same symptom set. The agent correctly refuses to commit and reports the 90% set. Options: `--db`, `--incidents 1,5,17`, `--warm` (use episodic memory), `--json FILE`.

## 7.3 Knowledge-base conformance

```bash
.venv/bin/python -m diag_agent kb conformance
.venv/bin/python -m diag_agent kb conformance --json /tmp/conf.json
```

For each labelled raw incident, the documented fault mode is propagated at the true location and compared, with every window closed, against what the substrate emitted. Findings are reported for a knowledge engineer; the KB is never written. A healthy install reports about `11/13 incidents conform`. The two soft findings (a missing Expected service-down under `ne_power_off`, and an unexplained OTU_DEG under attenuation) are documented open issues, not silent tailoring.

## 7.4 Fixtures

| Path | Content |
|---|---|
| `tests/diag_agent/fixtures/fiber_cut_oms3.db` | The fibre-cut demo: raw incident 42 and its suppressed twin 43 |
| `tests/diag_agent/fixtures/variants.db` | One raw/suppressed pair per simulator fault type (12 triggers) |
| `tests/diag_agent/fixtures/history.db` | 12 past incidents away from OMS3, used only to seed warm episodic memory |
| `tests/diag_agent/fixtures/episodes_warm.sqlite` | 11 verified episodes learned from `history.db` |
| `tests/diag_agent/cassettes/fiber_cut_oms3.sqlite` | Recorded LLM answers for the fibre-cut scenario |
| `tests/diag_agent/scenarios/fiber_cut_oms3.yaml` | The scenario used by the quick start and the acceptance test |

Rebuild the fixtures after changing a simulator generator or the topology:

```bash
.venv/bin/python tools/make_fixtures.py all          # fibre-cut + variants + history
.venv/bin/python tools/make_episodes.py              # history.db → episodes_warm.sqlite
```

`make_fixtures.py` calls the simulator's generators with a seeded RNG, so the result is reproducible. The agent itself never imports those generators.

## 7.5 Episodic memory

```bash
.venv/bin/python -m diag_agent episodes list
.venv/bin/python -m diag_agent episodes build --db tests/diag_agent/fixtures/history.db \
    --path data/diag_agent/episodes.sqlite
```

Each closed diagnosis with a commit can write an episode (unless you pass `--no-learn`). The verification weight is:

| Verification | Weight |
|---|---|
| `repair_confirmed` | 1.0 |
| `operator_confirmed` | 0.9 |
| `self` | 0.5 |
| `rejected` | 0.0 |

The analogy line retrieves the top-k episodes by weighted Jaccard similarity of the frame's role signature, and transfers their conclusions onto the current resources.
