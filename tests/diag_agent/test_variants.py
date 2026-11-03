"""Variants (doc 07 §9): one labelled incident per simulator fault type, no LLM."""

from __future__ import annotations

import pytest

from diag_agent.app import Agent, RunOptions
from diag_agent.environment.substrate.simulator_db import list_incidents
from diag_agent.harness import conformance
from diag_agent.harness.scorer import score

from .conftest import FIBRE_DB, VARIANTS_DB

RAW_IDS = [int(r["id"]) for r in list_incidents(VARIANTS_DB, suppressed=False)]


@pytest.fixture(scope="module")
def scores(settings, tmp_path_factory):
    store = tmp_path_factory.mktemp("variants") / "incidents.sqlite"
    out = {}
    for i in RAW_IDS:
        agent = Agent(settings, RunOptions(db=str(VARIANTS_DB), incident=i, no_llm=True, cold=True,
                                           incidents_store=str(store), validate="debug", learn=False))
        st = agent.run()
        out[i] = score(agent.sem, st, str(VARIANTS_DB), i)
        agent.close()
    return out


@pytest.mark.slow
def test_no_wrong_commit(scores):
    wrong = {i: s for i, s in scores.items() if s["committed"] and not s["correct"]}
    assert not wrong


@pytest.mark.slow
def test_accuracy_floor(scores):
    correct = sum(s["correct"] for s in scores.values())
    assert correct >= 9, {i: (s["truth"]["fault_type"], s["decision"]) for i, s in scores.items()}
    assert all(s["correct"] or s["truth_in_90_set"] for s in scores.values())


def test_conformance_fibre_cut_is_clean(sem):
    row = conformance.check_incident(sem, str(FIBRE_DB), 42)
    assert row["mode"] == "FIBRE_CUT" and row["ok"], row["findings"]
