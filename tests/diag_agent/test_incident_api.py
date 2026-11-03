"""Incident Server HTTP API (doc 06 §7) over the stored no-LLM fibre-cut run."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from diag_agent.incident_server.api import create_app


@pytest.fixture(scope="module")
def client(no_llm_run):
    agent, _ = no_llm_run
    return TestClient(create_app(agent.incidents))


def test_incidents_both_modes(client, no_llm_run):
    _, st = no_llm_run
    for mode in ("strict", "extended"):
        r = client.get("/restconf/data/ietf-incident:incidents", params={"mode": mode})
        assert r.status_code == 200
        inc = r.json()["ietf-incident:incidents"]["incident"]
        assert inc[0]["incident-no"] == st["incident_no"]
        assert inc[0]["status"] == "cleared"


def test_incident_by_key(client):
    r = client.get("/restconf/data/ietf-incident:incidents/incident=Loss%20of%20signal%20on%20span_OMS3_BC,"
                   "ietf-incident:problem,")
    assert r.status_code == 200
    assert r.json()["ietf-incident:incident"][0]["name"] == "Loss of signal on span_OMS3_BC"


def test_rpc_errors(client, no_llm_run):
    _, st = no_llm_run
    r = client.post("/restconf/operations/ietf-incident:incident-acknowledge",
                    json={"ietf-incident:input": {"incident-no": [st["incident_no"]]}})
    assert r.status_code == 400
    assert "repeated-acknowledge" in r.text
    r = client.post("/restconf/operations/ietf-incident:incident-diagnose",
                    json={"ietf-incident:input": {"incident-no": ["9999"]}})
    assert r.status_code == 400 and "incident-not-found" in r.text


def test_tree_and_ledger(client, no_llm_run):
    _, st = no_llm_run
    no = st["incident_no"]
    r = client.get(f"/diag/v1/incidents/{no}/tree", params={"format": "ascii"})
    assert r.status_code == 200 and "COMMIT FIBRE_CUT" in r.text
    r = client.get(f"/diag/v1/incidents/{no}/tree", params={"format": "mermaid", "view": "trace"})
    assert r.text.startswith("flowchart TD")
    r = client.get(f"/diag/v1/incidents/{no}/ledger")
    assert r.status_code == 200 and r.json()["leader"]
