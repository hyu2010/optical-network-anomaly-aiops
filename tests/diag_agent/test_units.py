"""Unit tests of the reasoning pieces."""

from __future__ import annotations

import math

import pytest

from diag_agent.procedural_memory.reasoning.advisor import conditional, mutual_information
from diag_agent.procedural_memory.reasoning.ledger import NOTA, entropy_bits, synthesize
from diag_agent.procedural_memory.reasoning.lines.pooling import pool
from diag_agent.procedural_memory.reasoning.narrator import validate_text
from diag_agent.procedural_memory.reasoning.windows import answer_key, classify_trace
from diag_agent.render.tree import ascii_tree, view_nodes
from diag_agent.working_memory.tree import TreeBuilder


def _trace(*anoms, degraded=False):
    return {"och": "och_ch2", "anomalies": list(anoms), "degraded": degraded, "total_length_km": 160}


def test_classify_trace_order():
    step = {"kind": "step", "magnitude_db": -30.0, "span_id": "span_OMS3_BC", "offset_in_span_km": 7.0, "z_km": 107}
    slope = {"kind": "slope", "magnitude_db": 0.6, "span_id": "span_OMS3_BC", "offset_in_span_km": None, "z_km": 100}
    assert answer_key(classify_trace(_trace(step, slope))) == "step-hard@span_OMS3_BC"
    soft = dict(step, magnitude_db=-6.0)
    assert answer_key(classify_trace(_trace(soft))) == "step-soft@span_OMS3_BC"
    spurious = dict(slope, magnitude_db=0.05)
    assert answer_key(classify_trace(_trace(spurious))) == "clean"
    assert answer_key(classify_trace(_trace(degraded=True))) in ("degraded", "null")


def test_entropy_and_mutual_information():
    assert entropy_bits({"a": 0.5, "b": 0.5}) == pytest.approx(1.0)
    post = {"H1": 0.5, "H2": 0.5}
    bits, pa, classes = mutual_information(post, {"H1": "x", "H2": "y"})
    assert bits == pytest.approx(1.0)
    bits0, _, _ = mutual_information(post, {"H1": "x", "H2": "x"})
    assert bits0 == pytest.approx(0.0, abs=1e-9)
    cond = conditional(post, {"H1": "x", "H2": "y"}, "x", 2)
    assert cond["H1"] == pytest.approx(1.0)


def test_synthesize_clips_and_normalises():
    prior = {"H1": 0.6, "H2": 0.3, NOTA: 0.1}
    terms = [{"name": "T_doc", "tau": 0.7, "values": {"H1": 0.0, "H2": -100.0}, "nota": -2.0}]
    s = synthesize(prior, terms, clip_nats=8)
    assert math.isclose(sum(s["post"].values()), 1.0, rel_tol=1e-9)
    assert s["leader"] == "H1"
    assert s["terms"][0]["values"]["H2"] == -8.0
    assert s["post"]["H2"] > 0


def test_pool_linear_and_abstention():
    res = {"walk": {"status": "ok", "dist": {"FIBRE_CUT@s": 0.6, "AMP_FAIL@b": 0.3}, "residual": 0.1,
                    "hyps": {"FIBRE_CUT@s": {"mode": "FIBRE_CUT", "location": "s"},
                             "AMP_FAIL@b": {"mode": "AMP_FAIL", "location": "b"}}},
           "llm": {"status": "abstain"}}
    out = pool(res, {"walk": 1.0, "llm": 1.0}, existing={})
    assert set(out["hyps"]) == {"FIBRE_CUT@s", "AMP_FAIL@b"}
    assert out["abstained"] == ["llm"]
    assert out["prior"]["FIBRE_CUT@s"] == pytest.approx(0.6)
    assert sum(out["prior"].values()) == pytest.approx(1.0)
    again = pool({"walk": dict(res["walk"], dist={"FIBRE_CUT@s": 0.9}, residual=0.1)}, {"walk": 1.0},
                 existing=out["hyps"])
    assert "AMP_FAIL@b" in again["hyps"]


def test_narrator_validation_rejects_invented_numbers_and_slot_names():
    slots = {"leader": {"mode": "FIBRE_CUT", "location": "span_OMS3_BC", "p": "0.997"},
             "fit": {"position_km": 7.0, "tol_km": 0.4}}
    assert validate_text("FIBRE_CUT at span_OMS3_BC with P 0.997, 7.0 +/- 0.4 km.", slots)[0]
    assert not validate_text("FIBRE_CUT at span_OMS3_BC, 31.2 km.", slots)[0]
    assert not validate_text("FIBRE_CUT at span_OMS3_BC with tol_km 0.4.", slots)[0]


def test_tree_builder_and_decision_view():
    tree: list = []
    tb = TreeBuilder(tree, 1, 0.0)
    r = tb.add("root", "root", parent=None)
    f = tb.add("frame", "frame", parent=r)
    q = tb.add("question", "Q?", parent=f)
    tree = tb.new
    dv = view_nodes(tree, "decision")
    assert [n["kind"] for n in dv] == ["root", "question"]
    assert dv[1]["parent"] == r
    assert ascii_tree(tree).splitlines()[1].endswith("Q?")


def test_settings_redaction(settings):
    prof = settings.profile_for("proposer")
    red = prof.redacted()
    assert "api_key" not in red or not red["api_key"] or "set" in str(red["api_key"]) or "*" in str(red["api_key"])
