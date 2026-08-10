"""Phase 3 measure-key canonicalization + stated-span guard + debug report.

These cover the narrow Phase 3 fix (see `.agent/plans/phase3-canonicalization-guards.md`): honest
facts must stop being rejected for measure-key scatter, obvious unsupported STATED aggregates must be
quarantined, fold-derived derivations (bike-spend SUM) must still pass, and the debug report must
make the absent user-turn capture metadata obvious. Every test uses fake LLM/adapter — no live model.
"""

from __future__ import annotations

import json

import pytest

from menhir.domain.fold_algebra import Event
from menhir.services.perception import (
    VETO_UNSUPPORTED_STATED,
    Episode,
    PerceivedGroup,
    _canonical_family_label,
    _collapse_measure_families,
    _measure_noun_sig,
    canonicalize_measure_key,
    canonicalize_samples,
    gate,
    perceive_and_fold,
)
from menhir.services.perception_report import build_phase3_report, format_phase3_report


# ----------------------------------------------------------------------------- fakes


class FakeLLM:
    def __init__(self, responses):
        self._responses = [json.dumps(r) for r in responses]
        self.calls = 0

    def __call__(self, system, user):
        out = self._responses[self.calls % len(self._responses)]
        self.calls += 1
        return out


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def record_counter(self, **kwargs):
        self.calls.append(kwargs)
        return {"uuid": "u", "view_key": "k", "created": True}


def _purchase(ep, measure, when, value):
    return {"episode": ep, "subject": "user", "measure": measure, "kind": "purchase",
            "when": when, "value": value, "what": "buy"}


def _buy(when, value, uuid):
    return Event(when=when, kind="purchase", value=value, episode_uuid=uuid)


def _stated_grp(measure, value):
    a = Event(when="2026-01-01", kind="assertion", value=value, episode_uuid="e0")
    return PerceivedGroup(subject="user", measure=measure, reducer="stated",
                          events=[], stated_total=value, stated_event=a)


# ----------------------------------------------------------------------------- canonicalization


@pytest.mark.unit
def test_canonicalize_key_idempotent_and_conservative():
    assert canonicalize_measure_key("cycling_parts_cost") == "cycling_spend"
    assert canonicalize_measure_key("cycling_spend") == "cycling_spend"          # idempotent
    assert canonicalize_measure_key("Cycling-Parts Cost") == "cycling_spend"     # case/sep normalized
    assert canonicalize_measure_key("item_number") == "item_count"               # generic cleanup
    # existing measures MUST pass through unchanged (guards current tests / View names)
    for m in ("bike_spend", "playlists", "bikes", "tanks", "spend", "fish_tanks_owned", "mugs"):
        assert canonicalize_measure_key(m) == m


@pytest.mark.unit
def test_scatter_collapses_before_consistency_gate():
    # each sample keys ONE cycling total under a different label; canonicalization collapses them so
    # the gate votes on a stable key and commits, instead of scattering to abstention.
    events = [_buy("2026-06-01", 40, "e0"), _buy("2026-06-06", 55, "e1"), _buy("2026-06-13", 30, "e2")]
    samples = [
        [PerceivedGroup("user", "cycling_spend", "sum", events=list(events))],
        [PerceivedGroup("user", "cycling_parts_cost", "sum", events=list(events))],
        [PerceivedGroup("user", "cycling_accessories_spend", "sum", events=list(events))],
    ]
    canon, mapping = canonicalize_samples(samples)
    assert all(g.measure == "cycling_spend" for s in canon for g in s)
    assert mapping["cycling_parts_cost"] == "cycling_spend"
    [d] = gate(canon, threshold=1.0)
    assert d.committed and d.measure == "cycling_spend" and d.value == 125.0


@pytest.mark.unit
def test_stated_total_recovers_when_key_scatters():
    samples = [[_stated_grp("to_watch_count", 25.0)],
               [_stated_grp("watchlist_items", 25.0)],
               [_stated_grp("movies_to_watch", 25.0)]]
    canon, _ = canonicalize_samples(samples)
    assert all(g.measure == "watchlist_item_count" for s in canon for g in s)
    [d] = gate(canon, threshold=1.0)  # no episodes -> span guard off
    assert d.committed and d.value == 25.0 and d.measure == "watchlist_item_count"


@pytest.mark.unit
def test_within_sample_collision_unions_without_double_count():
    # one sample emits BOTH the category total and an overlapping sub-measure that alias together;
    # union+provenance-dedup must keep 3 distinct events (SUM=125), never 5 (SUM=165/250).
    e = [_buy("2026-06-01", 40, "e0"), _buy("2026-06-06", 55, "e1"), _buy("2026-06-13", 30, "e2")]
    sample = [PerceivedGroup("user", "cycling_spend", "sum", events=[e[0], e[1], e[2]]),
              PerceivedGroup("user", "cycling_parts_spend", "sum", events=[e[0], e[1]])]
    canon, _ = canonicalize_samples([sample])
    assert len(canon[0]) == 1 and canon[0][0].measure == "cycling_spend"
    [d] = gate(canon * 3, threshold=1.0)
    assert d.committed and d.value == 125.0


# ----------------------------------------------------------------------------- stated-span guard


@pytest.mark.unit
def test_iphone_count_one_not_promoted():
    # "I bought an iPhone" -> bad iphone_count=1: never promoted (count-floor blocks value < 2).
    eps = [Episode(uuid="e0", content="I bought an iPhone yesterday.")]
    [d] = gate([[_stated_grp("iphone_count", 1.0)] for _ in range(3)], threshold=1.0, episodes=eps)
    assert not d.committed


@pytest.mark.unit
def test_span_guard_quarantines_ungrounded_stated_measure():
    # a stated count of 7 with no "7" anywhere in the span is a fabricated aggregate -> quarantine.
    eps = [Episode(uuid="e0", content="I have some gadgets lying around.")]
    grp = _stated_grp("gadget_count", 7.0)
    [d] = gate([[grp] for _ in range(3)], threshold=1.0, episodes=eps)
    assert not d.committed and d.veto == VETO_UNSUPPORTED_STATED
    # opt-in: without episodes the same stated measure commits (guard off by default)
    [d2] = gate([[grp] for _ in range(3)], threshold=1.0)
    assert d2.committed and d2.value == 7.0


@pytest.mark.unit
def test_span_guard_admits_grounded_stated_measure():
    eps = [Episode(uuid="e0", content="I have 20 playlists now.")]
    [d] = gate([[_stated_grp("playlists", 20.0)] for _ in range(3)], threshold=1.0, episodes=eps)
    assert d.committed and d.value == 20.0


# ----------------------------------------------------------------------------- fold-derived stays valid


@pytest.mark.unit
def test_bike_spend_sum_still_folds_end_to_end():
    buys = [_purchase(0, "bike_spend", "2026-06-01", 40),
            _purchase(1, "bike_spend", "2026-06-06", 55),
            _purchase(2, "bike_spend", "2026-06-13", 30)]
    eps = [Episode(uuid=f"e{i}", content=f"bought bike stuff {i}") for i in range(3)]
    res = perceive_and_fold(episodes=eps, llm_complete=FakeLLM([buys]), graph_adapter=FakeAdapter(),
                            k=3, enable_stated_span_guard=True)
    assert len(res.committed) == 1 and res.committed[0]["value"] == 125.0


@pytest.mark.unit
def test_span_guard_exempts_fold_derived_sum():
    # the guard must NOT reject a lawful fold just because the derived total isn't in any single span.
    buys = [_purchase(0, "bike_spend", "2026-06-01", 40),
            _purchase(1, "bike_spend", "2026-06-06", 55),
            _purchase(2, "bike_spend", "2026-06-13", 30)]
    eps = [Episode(uuid=f"e{i}", content=f"bought bike part number {i}") for i in range(3)]
    assert all("125" not in e.content for e in eps)
    res = perceive_and_fold(episodes=eps, llm_complete=FakeLLM([buys]), graph_adapter=FakeAdapter(),
                            k=3, enable_stated_span_guard=True)
    assert len(res.committed) == 1 and res.committed[0]["value"] == 125.0


# ----------------------------------------------------------------------------- debug report


@pytest.mark.unit
def test_phase3_report_flags_missing_capture_metadata():
    eps = [Episode(uuid="e0", content="bought bike tires $40")]
    llm = FakeLLM([[_purchase(0, "cycling_spend", "2026-06-01", 40)]])
    cap = {"total_episodes": 1718,
           "fields": {"role": 0, "speaker": 0, "source_kind": 0, "actor": 0, "declarant": 0},
           "source_distribution": {"claude-code": 970, "codex": 374},
           "turn_evidence": {"turn_evidence_table_exists": False, "total_turn_evidence": 0,
                             "user_evidence": 0, "claude_code_hook_evidence": 0}}
    report = build_phase3_report(eps, llm, k=1, capture_metadata=cap)
    assert report["input"]["user_turn_metadata_present"] is False
    assert report["input"]["role_field_counts"]["role"] == 0
    text = format_phase3_report(report)
    assert "user_turn_metadata_present = false" in text
    assert "No TurnEvidence records found" in text
    assert "TurnEvidence producer is required" in text


@pytest.mark.unit
def test_phase3_report_recognizes_captured_evidence() -> None:
    eps = [Episode(uuid="e0", content="bought bike tires $40")]
    llm = FakeLLM([[_purchase(0, "cycling_spend", "2026-06-01", 40)]])
    cap = {"total_episodes": 5, "fields": {"role": 0, "speaker": 0, "source_kind": 0,
           "actor": 0, "declarant": 0}, "source_distribution": {},
           "turn_evidence": {"turn_evidence_table_exists": True, "total_turn_evidence": 2,
                             "user_evidence": 2, "claude_code_hook_evidence": 2,
                             "triage_reason_counts": {"number": 2, "i_have": 1},
                             "triage_version_counts": {"claude-hook-v1": 2},
                             "latest_recorded_at": "2026-07-07T00:00:00Z"}}
    report = build_phase3_report(eps, llm, k=1, capture_metadata=cap)
    assert report["input"]["user_turn_metadata_present"] is True
    text = format_phase3_report(report)
    assert "phase3_records_selected: 2" in text
    assert "processing selectively captured user-authored evidence" in text
    assert "No TurnEvidence records found" not in text


# ----------------------------------------------------------------------------- F1: measure-family voting


def _sum_grp(measure, buys):
    return PerceivedGroup(subject="user", measure=measure, reducer="sum", events=list(buys))


@pytest.mark.unit
def test_noun_signature_unifies_morphological_variants():
    # the whole point of F1: differently-labelled variants of one SUM concept share a noun signature
    assert (_measure_noun_sig("bike_spend")
            == _measure_noun_sig("bikes_spend")
            == _measure_noun_sig("bikes_purchased")
            == frozenset({"bike"}))
    assert _measure_noun_sig("grocery_spend") != _measure_noun_sig("bike_spend")
    assert _measure_noun_sig("spend") == frozenset()   # all aggregation-words -> no noun -> stays solo


@pytest.mark.unit
def test_canonical_family_label_stable_across_variant_subsets():
    # a run may observe any subset of the variants; the canonical key must not depend on which subset
    assert _canonical_family_label(["bike_spend", "bikes_spend", "bikes_purchased"], "sum") == "bike_spend"
    assert _canonical_family_label(["bikes_spend", "bikes_purchased"], "sum") == "bike_spend"
    assert _canonical_family_label(["bikes"], "count") == "bike_count"   # noun-only -> reducer suffix


@pytest.mark.unit
def test_scattered_sum_family_commits_once_under_canonical_key():
    # F1 core: three samples each key the SAME $125 bike spend under a different label. Before the fix
    # each label was sub-unanimous (1/3) and all three abstained; after, they vote as one 3/3 cluster.
    buys = [_buy("2026-06-01", 50, "e0"), _buy("2026-06-02", 75, "e1")]  # sum = 125
    samples = [[_sum_grp("bike_spend", buys)],
               [_sum_grp("bikes_spend", buys)],
               [_sum_grp("bikes_purchased", buys)]]
    committed = [d for d in gate(samples, threshold=1.0) if d.committed]
    assert len(committed) == 1
    assert committed[0].measure == "bike_spend" and committed[0].value == 125.0
    assert committed[0].agreement == 1.0


@pytest.mark.unit
def test_family_keeps_distinct_nouns_separate():
    # different concepts must NOT be merged just because they share a reducer (over-merge = wrong write)
    b = [_buy("2026-06-01", 50, "e0"), _buy("2026-06-02", 75, "e1")]   # bike -> 125
    g = [_buy("2026-06-01", 20, "e2"), _buy("2026-06-02", 30, "e3")]   # grocery -> 50
    samples = [[_sum_grp("bike_spend", b), _sum_grp("grocery_spend", g)] for _ in range(3)]
    decs = {d.measure: d for d in gate(samples, threshold=1.0)}
    assert decs["bike_spend"].committed and decs["bike_spend"].value == 125.0
    assert decs["grocery_spend"].committed and decs["grocery_spend"].value == 50.0


@pytest.mark.unit
def test_family_keeps_distinct_reducers_separate():
    # same noun, different reducer (a SUM of dollars vs a COUNT of bikes) are DIFFERENT families
    by_key = {
        ("user", "bike_spend"): [_sum_grp("bike_spend", [_buy("2026-06-01", 50, "e0")])],
        ("user", "bikes"): [PerceivedGroup("user", "bikes", "count", events=[_buy("2026-06-01", None, "e2")])],
    }
    out = _collapse_measure_families(by_key)
    assert ("user", "bike_spend") in out
    assert ("user", "bikes") in out            # the count family is not folded into the sum family


@pytest.mark.unit
def test_solo_family_passes_through_unchanged():
    # a single-label family must be untouched (no rename of stable existing measures like `playlists`)
    by_key = {("user", "playlists"): [_stated_grp("playlists", 20.0)]}
    out = _collapse_measure_families(by_key)
    assert list(out.keys()) == [("user", "playlists")]


@pytest.mark.unit
def test_phase3_report_shows_raw_to_canonical_collapse():
    eps = [Episode(uuid="e0", content="bought parts")]
    # k=2 samples, same concept keyed two ways -> report should record the collapse
    llm = FakeLLM([[_purchase(0, "cycling_parts_cost", "2026-06-01", 40)],
                   [_purchase(0, "cycling_accessories_spend", "2026-06-01", 40)]])
    report = build_phase3_report(eps, llm, k=2)
    collapsed = dict(report["canonicalization"]["collapsed"])
    assert collapsed.get("cycling_parts_cost") == "cycling_spend"
    assert "cycling_spend" in report["canonicalization"]["canonical_measure_keys"]
