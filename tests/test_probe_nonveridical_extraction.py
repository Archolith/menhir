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
    assert set(cats) == _probe().CATEGORIES
    assert all(cats[k] >= 8 for k in ("negated", "question", "counterfactual", "plan_modal",
                                       "reported", "control"))
    seeds = {c.get("seed") for c in cases} - {None}
    assert seeds == {"f-015", "f-016", "f-018", "f-020", "f-033", "f-050"}


def test_pairs_are_mutual_and_controls_mirror_the_absent_claim():
    by_id = {c["id"]: c for c in _probe().load_corpus()}
    for c in by_id.values():
        if c["category"] != "control" or not c.get("pair"):
            continue
        other = by_id[c["pair"]]
        assert other.get("pair") == c["id"]
        absent = {p["text"] for p in other["propositions"] if p["expect"] == "absent"}
        present = {p["text"] for p in c["propositions"]}
        assert absent & present, f"{c['id']} does not mirror {other['id']}"


def test_every_noncontrol_case_has_an_absent_or_a_present_check():
    cases = _probe().load_corpus()
    # storing nothing must fail somewhere: most non-veridical cases carry a present proposition
    with_present = [c for c in cases if c["category"] != "control"
                    and any(p["expect"] == "present" for p in c["propositions"])]
    assert len(with_present) >= 30


def _write(tmp_path, case):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"cases": [case]}), encoding="utf-8")
    return path


def test_corpus_rejects_a_control_that_expects_absent(tmp_path):
    bad = {"id": "x", "category": "control", "speech_date": "2024-01-01", "turn": "t",
           "propositions": [{"text": "p", "expect": "absent"}]}
    with pytest.raises(ValueError, match="control"):
        _probe().load_corpus(_write(tmp_path, bad))


def test_corpus_rejects_a_dangling_pair(tmp_path):
    bad = {"id": "x", "category": "negated", "speech_date": "2024-01-01", "turn": "t",
           "propositions": [{"text": "p", "expect": "absent"}], "pair": "nope"}
    with pytest.raises(ValueError, match="pair"):
        _probe().load_corpus(_write(tmp_path, bad))


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


def test_judge_sees_fact_times_and_entity_summaries():
    p = _probe()
    case = {"speech_date": "2024-06-02", "turn": "I no longer work at Acme."}
    row = {"facts": [{"uuid": "f1", "fact": "User works at Acme", "valid_at": "2020-01-01",
                      "invalid_at": "2024-04-01", "expired_at": None}],
           "entities": [{"uuid": "e1", "name": "User", "summary": "Left Acme"},
                        {"uuid": "e2", "name": "Acme", "summary": ""}]}
    msgs = p.judge_messages(case, "As of 2024-06-02, the user works at Acme", row)
    user = msgs[1]["content"]
    assert "f1: User works at Acme [valid_at=2020-01-01, invalid_at=2024-04-01]" in user
    assert "e1: (entity User)" in user and "e2" not in user
    assert "Proposition: As of 2024-06-02" in user
    assert "invalid_at or expired_at" in msgs[0]["content"]
    assert "(nothing stored)" in p.judge_messages(case, "x", {})[1]["content"]


def test_summarize_counts_leaks_and_misses_per_proposition():
    p = _probe()
    cases = [{"id": "a", "category": "negated", "turn": "t",
              "propositions": [{"text": "x", "expect": "absent"}, {"text": "y", "expect": "present"}]},
             {"id": "c", "category": "control", "turn": "t",
              "propositions": [{"text": "z", "expect": "present"}, {"text": "w", "expect": "present"}]}]
    verdicts = {"a#0": {"asserted": True, "fact_ref": "f9"}, "a#1": {"asserted": True},
                "c#0": {"asserted": False}}
    out = p.summarize(cases, verdicts)
    assert out["by_category"]["negated"] == {"absent_n": 1, "present_n": 1, "leak": 1}
    assert out["by_category"]["control"] == {"present_n": 1, "miss": 1, "ungraded": 1}
    assert [(w["id"], w["kind"], w["fact"]) for w in out["wrong"]] == [
        ("a", "leak", "f9"), ("c", "miss", None)]


def test_namespaces_are_per_case_and_run():
    p = _probe()
    assert p.namespace_for("R1", "neg-01") == "p283-r1-neg-01"
    assert p.namespace_for("r1", "neg-01") != p.namespace_for("r2", "neg-01")
