"""Offline checks for the #283 probe: corpus shape, grading and the shared-port guard."""

from __future__ import annotations

import importlib.util
import json
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _probe():
    spec = importlib.util.spec_from_file_location(
        "probe_nonveridical", ROOT / "scripts/probe_nonveridical_extraction.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_corpus_loads_and_covers_every_category():
    cases = _probe().load_corpus()
    cats = Counter(c["category"] for c in cases)
    assert set(cats) == {"negated", "question", "counterfactual", "plan_modal", "reported", "control"}
    assert all(n >= 8 for n in cats.values())
    seeds = {c.get("seed") for c in cases} - {None}
    assert seeds == {"f-015", "f-016", "f-018", "f-020", "f-033", "f-050"}


def test_corpus_rejects_a_control_that_expects_absent(tmp_path):
    bad = {"cases": [{"id": "x", "category": "control", "speech_date": "2024-01-01",
                      "turn": "t", "proposition": "p", "expect": "absent"}]}
    path = tmp_path / "c.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="only controls"):
        _probe().load_corpus(path)


@pytest.mark.parametrize("uri", ["bolt://localhost:7688", "bolt://127.0.0.1:7687",
                                 "bolt://localhost", "neo4j://localhost:7474"])
def test_ingest_refuses_shared_instances(uri):
    with pytest.raises(SystemExit):
        _probe().check_neo4j_uri(uri)


def test_ingest_accepts_a_throwaway_port():
    _probe().check_neo4j_uri("bolt://127.0.0.1:17931")


def test_parse_verdict():
    p = _probe()
    assert p.parse_verdict("ASSERTED | abc-1") == (True, "abc-1")
    assert p.parse_verdict("NOT_ASSERTED | none\nbecause") == (False, None)
    with pytest.raises(ValueError):
        p.parse_verdict("maybe")


def test_judge_sees_facts_and_entity_summaries():
    p = _probe()
    case = {"speech_date": "2023-09-12", "turn": "I did not go.", "proposition": "User went"}
    row = {"facts": [{"uuid": "f1", "fact": "User did not attend the party"}],
           "entities": [{"uuid": "e1", "name": "User", "summary": "Skipped a party"},
                        {"uuid": "e2", "name": "Party", "summary": ""}]}
    user = p.judge_messages(case, row)[1]["content"]
    assert "f1: User did not attend the party" in user and "e1: (entity User)" in user
    assert "e2" not in user
    assert "(nothing stored)" in p.judge_messages(case, {})[1]["content"]


def test_summarize_scores_absent_and_present_expectations():
    p = _probe()
    cases = [{"id": "a", "category": "negated", "turn": "t", "proposition": "p", "expect": "absent"},
             {"id": "b", "category": "negated", "turn": "t", "proposition": "p", "expect": "absent"},
             {"id": "c", "category": "control", "turn": "t", "proposition": "p", "expect": "present"},
             {"id": "d", "category": "control", "turn": "t", "proposition": "p", "expect": "present"}]
    verdicts = {"a": {"asserted": True, "fact_ref": "f9"}, "b": {"asserted": False},
                "c": {"asserted": True}}
    out = p.summarize(cases, verdicts)
    assert out["by_category"]["negated"] == {"n": 2, "ok": 1, "wrong": 1}
    assert out["by_category"]["control"] == {"n": 1, "ok": 1, "ungraded": 1}
    assert [w["id"] for w in out["wrong"]] == ["a"] and out["wrong"][0]["fact"] == "f9"


def test_namespaces_are_per_case_and_run():
    p = _probe()
    assert p.namespace_for("R1", "neg-01") == "p283-r1-neg-01"
    assert p.namespace_for("r1", "neg-01") != p.namespace_for("r2", "neg-01")
