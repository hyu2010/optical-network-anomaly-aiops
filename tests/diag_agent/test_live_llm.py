"""Live LLM smoke test. Runs only with DIAG_LIVE_LLM=1 and a configured key; never prints the key."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def live_settings(settings):
    if os.environ.get("DIAG_LIVE_LLM") != "1":
        pytest.skip("set DIAG_LIVE_LLM=1 to call the real endpoint")
    prof = settings.profile_for("proposer")
    if not (prof.base_url and prof.model and prof.api_key):
        pytest.skip("no LLM endpoint configured in .env")
    return settings


def test_live_endpoint_answers_json(live_settings):
    from diag_agent.app import build_llm
    llm = build_llm(live_settings)
    llm.cache = None
    r = llm.chat("orchestrator", [{"role": "user", "content": 'Reply with the JSON object {"ok": true}.'}], json_mode=True)
    assert '"ok"' in r["content"]
    key = live_settings.profile_for("proposer").api_key
    assert key not in repr(r)


def test_live_fibre_cut(live_settings, tmp_path):
    from .conftest import run_scenario
    agent, st = run_scenario(live_settings, tmp_path, no_llm=False, cache_mode="off")
    agent.close()
    c = st["decision_info"]["committed"]
    assert (c["mode"], c["location"]) == ("FIBRE_CUT", "span_OMS3_BC")
    assert st["budget"]["llm_calls"] >= 1
