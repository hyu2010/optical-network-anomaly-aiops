"""Fibre-cut acceptance test (doc 07 §8, A1-A9)."""

from __future__ import annotations

import json

import pytest

from diag_agent.harness.scorer import score
from diag_agent.render.tree import render

from .conftest import FIBRE_DB, run_scenario

FORBIDDEN = {"fault_type", "fault_target", "t_offset_ms", "group_id", "is_root"}


def _kinds(tree):
    out = {}
    for n in tree:
        out[n["kind"]] = out.get(n["kind"], 0) + 1
    return out


@pytest.fixture(params=["no_llm_run", "cassette_run"])
def run(request):
    return request.getfixturevalue(request.param)


def test_a1_commit_fibre_cut_and_position(run):
    agent, st = run
    c = st["decision_info"]["committed"]
    assert (c["mode"], c["location"]) == ("FIBRE_CUT", "span_OMS3_BC")
    sc = score(agent.sem, st, str(FIBRE_DB), 42)
    assert sc["correct"]
    assert sc["position_error_km"] <= 2.0


def test_a2_thresholds_cycles_checks(run):
    _, st = run
    c = st["decision_info"]["committed"]
    assert c["p"] >= 0.80 and c["nota"] < 0.20
    assert st["cycle"] <= 2
    b = st["budget"]
    assert b["remote_checks"] == 1 and b["field_checks"] == 0
    assert b["checks_done"] == ["CHK_PPE_PROFILE(och_ch2)"]


def test_a3_open_windows_are_not_penalised(run):
    _, st = run
    tree = st["tree"]
    ev1 = next(n for n in tree if n["kind"] == "evaluate" and n["cycle"] == 1)
    assert "H1 6✓ 0✗ 2⧗" in ev1["label"]
    h1 = st["windows"][st["ledger"]["leader"]]["states"]
    odu4 = [s for s in h1.values() if s["resource"] == "C.OTU.client_ch4" and s["condition"] == "ServerSignalFail"]
    assert odu4 and all(s["state"] == "MATCHED" for s in odu4)
    assert not [s for s in h1.values() if s["state"] in ("MISSING", "CONTRADICTED") and s["strength"] == "Required"]


def test_a4_llm_hypotheses_have_walk_provenance(cassette_run):
    _, st = cassette_run
    gen = next(n for n in st["tree"] if n["kind"] == "generate" and "[llm]" in n["label"])
    assert gen["detail"]["status"] == "ok"
    for h in gen["detail"].get("hyps", []):
        assert h.get("validated", True)
    assert st["budget"]["llm_calls"] >= 1


def test_a5_tree_shape(run):
    _, st = run
    k = _kinds(st["tree"])
    for kind in ("root", "frame", "generate", "hypothesis", "pruned", "evaluate", "question", "check", "answer",
                 "synthesis", "decision", "verify"):
        assert k.get(kind, 0) >= 1, kind
    assert k["generate"] == 4
    assert k["pruned"] >= 4
    assert k["question"] >= 3


def test_a6_payloads(run):
    agent, st = run
    notes = agent.incidents.store.notifications(st["incident_no"])
    kinds = [n["kind"] for n in notes]
    assert kinds[0] == "raised" and kinds[-1] == "cleared"
    assert "task" in kinds
    keys = set()
    for n in notes:
        for mode, p in n["payload"].items():
            body = next(iter(p.values()))
            agent.incidents.validator.node("incident-notification" if n["kind"] != "task"
                                           else "diagnosis-task-notification", p, force=True)
            if n["kind"] == "task":
                continue
            assert body["incident-no"] == st["incident_no"]
            keys.add((body["name"], body["type"], body.get("incident-qualifier", "")))
            ev_ids = {e["event-id"] for e in body.get("events", {}).get("event", [])}
            for pe in body.get("probable-events", {}).get("probable-event", []):
                assert pe["event-id"] in ev_ids
    assert len(keys) == 1
    for mode in ("strict", "extended"):
        agent.incidents.validator.data_tree(agent.incidents.incidents(mode), force=True)


def test_a7_no_label_leakage(run):
    agent, _ = run
    for entry in agent.substrate.access_log():
        assert entry["component"] == "substrate"
        assert not (set(entry["columns"]) & FORBIDDEN), entry
        where = (entry["where"] or "").replace(" ", "")
        assert "is_root=1" not in where and ("is_root" not in where or "is_root=0" in where), entry
    for entry in agent.labels.access_log():
        assert entry["component"] == "field_oracle"
    assert agent.labels.access_log() == []


def test_a8_determinism_with_cassette(settings, tmp_path, cassette_run):
    _, st1 = cassette_run
    _, st2 = run_scenario(settings, tmp_path, no_llm=False, cache_mode="read_only")
    assert render(st1["tree"], "trace", "json") == render(st2["tree"], "trace", "json")
    h1 = [{k: v for k, v in h.items()} for h in st1["ledger"]["history"]]
    h2 = [{k: v for k, v in h.items()} for h in st2["ledger"]["history"]]
    assert json.dumps(h1, sort_keys=True) == json.dumps(h2, sort_keys=True)


def test_a9_no_llm_same_cause(no_llm_run, cassette_run):
    a = no_llm_run[1]["decision_info"]["committed"]
    b = cassette_run[1]["decision_info"]["committed"]
    assert (a["mode"], a["location"]) == (b["mode"], b["location"])


def test_closure_and_learning(no_llm_run):
    agent, st = no_llm_run
    cl = st["closure"]
    assert cl.get("cleared") is True
    assert cl.get("verification") == "repair_confirmed"
    assert cl.get("episode_id", "").startswith("ep-")
    assert agent.episodic.count() >= 1
    assert cl.get("incident_graph") == f"g:incident/{st['incident_no']}"
    assert any(g["graph"].endswith(f"incident/{st['incident_no']}") for g in agent.sem.graphs())


def test_renderers(no_llm_run):
    _, st = no_llm_run
    txt = render(st["tree"], "decision", "ascii")
    assert "COMMIT FIBRE_CUT @ span_OMS3_BC" in txt
    mm = render(st["tree"], "decision", "mermaid")
    assert mm.startswith("flowchart TD")
    nodes = json.loads(render(st["tree"], "trace", "json"))
    assert len(nodes) == len(st["tree"])
