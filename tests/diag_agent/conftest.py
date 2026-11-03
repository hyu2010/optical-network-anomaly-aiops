from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
SCENARIO = HERE / "scenarios" / "fiber_cut_oms3.yaml"
FIBRE_DB = HERE / "fixtures" / "fiber_cut_oms3.db"
VARIANTS_DB = HERE / "fixtures" / "variants.db"


@pytest.fixture(scope="session")
def settings():
    from diag_agent.app import settings_from
    return settings_from()


@pytest.fixture(scope="session")
def sem(settings):
    from diag_agent.app import semantic_cached
    return semantic_cached(settings)


def run_scenario(settings, tmp: Path, *, no_llm: bool, cache_mode: str | None = None, cold: bool = False,
                 patch=None):
    from diag_agent.app import Agent, RunOptions, load_scenario
    sc = load_scenario(SCENARIO)
    opts = RunOptions(db=sc["incident_db"], incident=sc["raw_incident_id"], scenario=sc, no_llm=no_llm,
                      cache_mode=cache_mode, cold=cold, incidents_store=str(tmp / "incidents.sqlite"),
                      validate="always", learn=True)
    agent = Agent(settings, opts)
    state = agent.run()
    return agent, state


@pytest.fixture(scope="session")
def no_llm_run(settings, tmp_path_factory):
    agent, state = run_scenario(settings, tmp_path_factory.mktemp("nollm"), no_llm=True)
    yield agent, state
    agent.close()


@pytest.fixture(scope="session")
def cassette_run(settings, tmp_path_factory):
    agent, state = run_scenario(settings, tmp_path_factory.mktemp("cassette"), no_llm=False, cache_mode="read_only")
    yield agent, state
    agent.close()
