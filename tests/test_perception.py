"""Perception boundary — the precision-first, abstaining LLM->Event gate.

Every test drives the boundary with a FAKE `llm_complete` (deterministic sampling) and a FAKE
`embed`, so the whole conjunctive veto-gate is exercised with no live model or graph. The invariant
under test: a wrong current-state View is worse than a missing one, so anything short of a
concentrated, triangulated, dedup-stable derivation must ABSTAIN (a no-op).
"""

from __future__ import annotations

import json

import pytest

from menhir.domain.fold_algebra import Event
from menhir.services.perception import (
    Episode,
    GateDecision,
    PerceivedGroup,
    extract_once,
    extract_stated_total,
    gate,
    perceive_and_fold,
)


# ----------------------------------------------------------------------------- fakes


class FakeLLM:
    """Returns a pre-baked JSON array per call, cycling through `responses` to simulate the
    dispersion of k temperature>0 samples. Records the calls for assertions."""

    def __init__(self, responses: list[list[dict]]):
        self._responses = [json.dumps(r) for r in responses]
        self.calls = 0

    def __call__(self, system: str, user: str) -> str:
        out = self._responses[self.calls % len(self._responses)]
        self.calls += 1
        return out


class FakeAdapter:
    def __init__(self):
        self.calls: list[dict] = []

    def record_counter(self, **kwargs):
        self.calls.append(kwargs)
        return {"uuid": "u", "view_key": "k", "created": True}


def fake_embed(text: str):
    """Keyword-space embeddings: the two '5-gallon' phrasings collapse; '20-gallon' is orthogonal."""
    t = text.lower()
    if "20" in t:
        return [0.0, 1.0, 0.0]
    if "5" in t:
        return [1.0, 0.0, 0.0]
    return [0.0, 0.0, 1.0]


def _purchase(ep, measure, when, value):
    return {"episode": ep, "subject": "user", "measure": measure, "kind": "purchase",
            "when": when, "value": value, "what": "buy"}


def _item(ep, measure, when, identity):
    return {"episode": ep, "subject": "user", "measure": measure, "kind": "item",
            "when": when, "identity": identity, "what": "own"}


def _assertion(ep, measure, when, value):
    return {"episode": ep, "subject": "user", "measure": measure, "kind": "assertion",
            "when": when, "value": value, "what": "stated total"}


BIKE = [_purchase(0, "bike_spend", "2026-01-10", 50), _purchase(1, "bike_spend", "2026-03-02", 135)]
EPISODES = [Episode(uuid=f"e{i}", content=f"episode {i}") for i in range(4)]


# ----------------------------------------------------------------------------- (1) extractor


@pytest.mark.unit
def test_extract_once_groups_and_infers_reducer():
    llm = FakeLLM([BIKE + [_assertion(2, "bike_spend", "2026-03-05", 185)]])
    groups = extract_once(EPISODES, llm)
    assert len(groups) == 1
    g = groups[0]
    assert (g.subject, g.measure, g.reducer) == ("user", "bike_spend", "sum")
    assert len(g.events) == 2                         # the assertion is peeled off, not folded
    assert g.stated_total == 185.0                    # ...into stated_total for triangulation
    assert g.events[0].episode_uuid == "e0"           # provenance attributed by episode number


@pytest.mark.unit
def test_extract_once_infers_distinct_count_for_items():
    llm = FakeLLM([[_item(0, "tanks", "2026-01-01", "5-gallon tank"),
                    _item(1, "tanks", "2026-02-01", "20-gallon tank")]])
    g = extract_once(EPISODES, llm)[0]
    assert g.reducer == "distinct_count"


# ----------------------------------------------------------------------------- (2) self-consistency gate


def _grp(events, reducer="sum", stated=None, measure="bike_spend"):
    return PerceivedGroup(subject="user", measure=measure, reducer=reducer,
                          events=events, stated_total=stated)


def _bike_events():
    return [Event(when="2026-01-10", kind="purchase", value=50, episode_uuid="e0"),
            Event(when="2026-03-02", kind="purchase", value=135, episode_uuid="e1")]


@pytest.mark.unit
def test_gate_commits_unanimous_value():
    samples = [[_grp(_bike_events())] for _ in range(3)]   # k=3, all -> $185
    [d] = gate(samples, threshold=1.0)
    assert d.committed and d.value == 185.0
    assert d.agreement == 1.0 and d.k == 3


@pytest.mark.unit
def test_gate_abstains_on_scattered_value():
    a = _grp([Event(when="2026-01-10", kind="purchase", value=185, episode_uuid="e0")])
    b = _grp([Event(when="2026-01-10", kind="purchase", value=200, episode_uuid="e0")])
    c = _grp([Event(when="2026-01-10", kind="purchase", value=50, episode_uuid="e0")])
    [d] = gate([[a], [b], [c]], threshold=1.0)
    assert not d.committed and d.value is None
    assert d.distribution == {"185.00": 1, "200.00": 1, "50.00": 1}


@pytest.mark.unit
def test_gate_absent_sample_counts_as_disagreement():
    # a minority of samples perceive the measure at all -> can't reach a unanimous threshold
    samples = [[_grp(_bike_events())], [], []]
    [d] = gate(samples, threshold=1.0)
    # the modal bucket is ABSENT (2/3); the value only one sample saw can never reach threshold
    assert not d.committed and d.distribution == {"185.00": 1, "__absent__": 2}


@pytest.mark.unit
def test_gate_triangulation_vetoes_disagreeing_stated_total():
    # unanimous SUM=185, but every sample also carries a STATED total of 200 -> abstain
    samples = [[_grp(_bike_events(), stated=200.0)] for _ in range(3)]
    [d] = gate(samples, threshold=1.0)
    assert not d.committed and d.triangulated is False
    assert "triangulation failed" in d.reason


def _anchor(measure, when, value, reducer="distinct_count", deltas=()):
    anchor = Event(when=when, kind="assertion", value=value, episode_uuid="a0")
    return PerceivedGroup(subject="user", measure=measure, reducer=reducer,
                          events=list(deltas), stated_total=value, stated_event=anchor)


@pytest.mark.unit
def test_gate_law3_anchor_plus_post_anchor_delta():
    # "I have 3 tanks" (anchor) then later "bought another" -> current = 3 + 1 = 4, not abstain.
    delta = Event(when="2023-03-01", kind="item", identity="new tank", episode_uuid="e1")
    g = _anchor("fish_tanks_owned", "2023-01-01", 3.0, deltas=[delta])
    [d] = gate([[g] for _ in range(3)], threshold=1.0)
    assert d.committed and d.value == 4.0 and d.triangulated is True     # reconcile IS corroboration


@pytest.mark.unit
def test_gate_law3_sum_balance_accrues_deltas():
    # savings balance: stated $5000, then two later deposits -> 5000 + 500 + 200 = 5700
    deltas = [Event(when="2023-02-01", kind="purchase", value=500.0, episode_uuid="e1"),
              Event(when="2023-03-01", kind="purchase", value=200.0, episode_uuid="e2")]
    g = _anchor("savings", "2023-01-01", 5000.0, reducer="sum", deltas=deltas)
    [d] = gate([[g] for _ in range(3)], threshold=1.0)
    assert d.committed and d.value == 5700.0


@pytest.mark.unit
def test_gate_law3_ignores_pre_anchor_events_as_redundant():
    # a purchase dated BEFORE the anchor is what the anchor summarizes -> NOT a delta; the redundant
    # triangulation path runs (sum=50 vs stated 185 -> disagree -> abstain, unchanged behaviour).
    # (sum reducer so the count-floor doesn't pre-empt the triangulation veto we're checking.)
    pre = Event(when="2022-06-01", kind="purchase", value=50.0, episode_uuid="e1")
    g = _anchor("bike_spend", "2023-01-01", 185.0, reducer="sum", deltas=[pre])
    [d] = gate([[g] for _ in range(3)], threshold=1.0)
    assert not d.committed and d.triangulated is False


@pytest.mark.unit
def test_gate_triangulation_passes_when_sum_matches_stated():
    samples = [[_grp(_bike_events(), stated=185.0)] for _ in range(3)]
    [d] = gate(samples, threshold=1.0)
    assert d.committed and d.triangulated is True and d.value == 185.0


@pytest.mark.unit
def test_gate_dedup_resolves_distinct_identity():
    # "5-gallon tank" and "the 5 gallon one" are one item -> DISTINCT-COUNT 2, not 3
    events = [Event(when="2026-01-01", kind="item", identity="5-gallon tank", episode_uuid="e0"),
              Event(when="2026-02-01", kind="item", identity="the 5 gallon one", episode_uuid="e1"),
              Event(when="2026-03-01", kind="item", identity="20-gallon tank", episode_uuid="e2")]
    samples = [[_grp(events, reducer="distinct_count", measure="tanks")] for _ in range(3)]
    [d] = gate(samples, threshold=1.0, embed=fake_embed)
    assert d.committed and d.value == 2.0


@pytest.mark.unit
def test_gate_dedup_without_embed_uses_exact_string():
    events = [Event(when="2026-01-01", kind="item", identity="5-gallon tank", episode_uuid="e0"),
              Event(when="2026-02-01", kind="item", identity="the 5 gallon one", episode_uuid="e1")]
    samples = [[_grp(events, reducer="distinct_count", measure="tanks")] for _ in range(3)]
    [d] = gate(samples, threshold=1.0, embed=None)
    assert d.committed and d.value == 2.0            # exact-string keeps them separate (safe default)


# ----------------------------------------------------------------------------- (1b/4) Lever B: holistic cross-check


@pytest.mark.unit
def test_extract_stated_total_parses_holistic_total():
    llm = FakeLLM([{"total": 185}])
    assert extract_stated_total(EPISODES, "bike_spend", llm) == 185.0
    assert llm.calls == 1                              # k=1 — holistic totals are stable (cost guard)


@pytest.mark.unit
def test_extract_stated_total_null_is_no_cross_check():
    # no basis for a total -> None (means "no cross-check available", never 0.0)
    assert extract_stated_total(EPISODES, "bike_spend", FakeLLM([{"total": None}])) is None
    assert extract_stated_total(EPISODES, "bike_spend", FakeLLM([{"nope": 1}])) is None
    assert extract_stated_total(EPISODES, "bike_spend", FakeLLM([{"total": "n/a"}])) is None


@pytest.mark.unit
def test_extract_stated_total_measure_is_interpolated_into_prompt():
    class _Capture(FakeLLM):
        def __call__(self, system, user):
            self.last_system = system
            return super().__call__(system, user)

    llm = _Capture([{"total": 42}])
    extract_stated_total(EPISODES, "playlists", llm)
    assert "playlists" in llm.last_system and "{measure}" not in llm.last_system


def _bike225_events():
    # an itemized SUM that a double-count inflated to 225 (gold total is 185)
    return [Event(when="2026-01-10", kind="purchase", value=90, episode_uuid="e0"),
            Event(when="2026-02-10", kind="purchase", value=90, episode_uuid="e1"),
            Event(when="2026-03-10", kind="purchase", value=45, episode_uuid="e2")]


@pytest.mark.unit
def test_gate_cross_check_vetoes_confident_bias():
    # the marquee case gpt4_d84a3211: unanimous itemized SUM=225, no USER total, holistic says 185.
    # self-consistency and count-floor both pass (it's confident + a SUM); only veto-4 stops it.
    samples = [[_grp(_bike225_events())] for _ in range(3)]
    [d] = gate(samples, threshold=1.0, cross_check=lambda m: 185.0)
    assert not d.committed and d.value is None
    assert d.cross_total == 185.0 and "cross-check failed" in d.reason


@pytest.mark.unit
def test_gate_cross_check_agreement_commits():
    # itemized SUM=225 and a holistic 225 are two independent derivations that agree -> commit
    samples = [[_grp(_bike225_events())] for _ in range(3)]
    [d] = gate(samples, threshold=1.0, cross_check=lambda m: 225.0)
    assert d.committed and d.value == 225.0
    assert d.triangulated is True and d.cross_total == 225.0


@pytest.mark.unit
def test_gate_cross_check_absent_total_does_not_veto():
    # no cross-check available (None) -> veto-4 is a no-op; precision identical to before Lever B
    samples = [[_grp(_bike225_events())] for _ in range(3)]
    [d] = gate(samples, threshold=1.0, cross_check=lambda m: None)
    assert d.committed and d.value == 225.0 and d.cross_total is None


@pytest.mark.unit
def test_gate_cross_check_skipped_when_user_stated_total_exists():
    # a USER stated total already triangulates (veto-3); veto-4 must NOT also fire (no double-derive).
    called = []
    samples = [[_grp(_bike_events(), stated=185.0)] for _ in range(3)]
    [d] = gate(samples, threshold=1.0, cross_check=lambda m: called.append(m) or 999.0)
    assert d.committed and called == []            # cross-check never consulted; veto-3 owned it


@pytest.mark.unit
def test_gate_cross_check_is_abstain_only_never_rescues():
    # a scattered value abstains at veto-1; the cross-check runs only on the commit path, so it can
    # never RESCUE a rejected value — and must not even be consulted here.
    called = []
    a = _grp([Event(when="2026-01-10", kind="purchase", value=185, episode_uuid="e0")])
    b = _grp([Event(when="2026-01-10", kind="purchase", value=200, episode_uuid="e0")])
    c = _grp([Event(when="2026-01-10", kind="purchase", value=50, episode_uuid="e0")])
    [d] = gate([[a], [b], [c]], threshold=1.0, cross_check=lambda m: called.append(m) or 185.0)
    assert not d.committed and called == []


@pytest.mark.unit
def test_perceive_and_fold_enable_cross_check_abstains_on_bias():
    # end-to-end: every sample extracts the same inflated SUM=225; the holistic cross-check says 185.
    inflated = [_purchase(0, "bike_spend", "2026-01-10", 90),
                _purchase(1, "bike_spend", "2026-02-10", 90),
                _purchase(2, "bike_spend", "2026-03-10", 45)]
    responses = [inflated, inflated, inflated,   # k=3 itemized extractions (unanimous 225)
                 {"total": 185}]                  # the single holistic cross-check call
    adapter = FakeAdapter()
    res = perceive_and_fold(episodes=EPISODES, llm_complete=FakeLLM(responses), graph_adapter=adapter,
                            k=3, enable_cross_check=True)
    assert not res.committed and len(res.abstained) == 1
    assert adapter.calls == []                     # dangerous write stopped: absence of View = fallback
    assert "cross-check failed" in res.abstained[0].reason


# ----------------------------------------------------------------------------- (3) perceive -> fold / abstain


@pytest.mark.unit
def test_gate_count_floor_vetoes_count_of_one():
    # a unanimously-perceived single possession (distinct_count=1) carries no aggregation -> abstain.
    # This is the dominant over-extraction the live tuning run surfaced; self-consistency can't catch
    # it (it's consistently perceived), so the deterministic floor is the backstop.
    one = [Event(when="2026-01-01", kind="item", identity="my only bike", episode_uuid="e0")]
    samples = [[_grp(one, reducer="distinct_count", measure="bikes_owned")] for _ in range(3)]
    [d] = gate(samples, threshold=1.0)
    assert not d.committed and d.value is None
    assert "count-floor" in d.reason
    # ...but SUM is exempt: a $1 total is still a meaningful aggregate, not a bare count
    money = [Event(when="2026-01-01", kind="purchase", value=1.0, episode_uuid="e0")]
    [s] = gate([[_grp(money, reducer="sum", measure="spend")] for _ in range(3)], threshold=1.0)
    assert s.committed and s.value == 1.0


@pytest.mark.unit
def test_gate_commits_move1_stated_total_without_fold_events():
    # 'I have 20 playlists' — a pure stated total, no itemized events. The gate commits the assertion
    # as the value (move 1), carrying its provenance; 20 clears the count-floor.
    assertion = Event(when="2026-01-01", kind="assertion", value=20.0, episode_uuid="e0")
    grp = PerceivedGroup(subject="user", measure="playlists", reducer="stated",
                         events=[], stated_total=20.0, stated_event=assertion)
    [d] = gate([[grp] for _ in range(3)], threshold=1.0)
    assert d.committed and d.value == 20.0 and d.reducer == "stated"
    assert d.events == [assertion]                    # provenance-bearing, for valid_at + MENTIONS


@pytest.mark.unit
def test_gate_count_floor_applies_to_stated_totals():
    # Lever-B live run: the extractor fabricated fish_tanks_owned=1 from "1-gallon tank" (a unit
    # misread as a stated count), unanimously — bias, invisible to self-consistency. A stated total
    # of 1 adds nothing over the raw episode, so the floor vetoes it (FP >> FN).
    assertion = Event(when="2026-01-01", kind="assertion", value=1.0, episode_uuid="e0")
    grp = PerceivedGroup(subject="user", measure="fish_tanks_owned", reducer="stated",
                         events=[], stated_total=1.0, stated_event=assertion)
    [d] = gate([[grp] for _ in range(3)], threshold=1.0)
    assert not d.committed and "count-floor" in d.reason


@pytest.mark.unit
def test_extract_once_keeps_pure_stated_total_group():
    llm = FakeLLM([[_assertion(0, "playlists", "2026-01-01", 20)]])  # assertion only, no items
    [g] = extract_once(EPISODES, llm)
    assert g.reducer == "stated" and g.stated_total == 20.0 and not g.events
    assert g.stated_event is not None and g.stated_event.episode_uuid == "e0"


@pytest.mark.unit
def test_perceive_and_fold_commits_confident_view():
    llm = FakeLLM([BIKE])                             # every sample identical -> unanimous
    adapter = FakeAdapter()
    res = perceive_and_fold(episodes=EPISODES, llm_complete=llm, graph_adapter=adapter, k=3)
    assert len(res.committed) == 1 and not res.abstained
    assert len(adapter.calls) == 1                    # exactly one counter View written
    assert adapter.calls[0]["value"] == 185.0
    assert llm.calls == 3                             # k independent samples
    audit = adapter.calls[0]["audit"]                 # gate evidence rides along as a receipt
    assert audit["view_audit_gate"] == "perception" and audit["view_audit_agreement"] == 1.0
    assert audit["view_audit_k"] == 3


@pytest.mark.unit
def test_perceive_and_fold_abstains_is_a_noop():
    # three genuinely different perceptions -> scattered -> abstain -> NO View written (the fallback)
    llm = FakeLLM([
        [_purchase(0, "bike_spend", "2026-01-10", 185)],
        [_purchase(0, "bike_spend", "2026-01-10", 200)],
        [_purchase(0, "bike_spend", "2026-01-10", 50)],
    ])
    adapter = FakeAdapter()
    res = perceive_and_fold(episodes=EPISODES, llm_complete=llm, graph_adapter=adapter, k=3)
    assert not res.committed and len(res.abstained) == 1
    assert adapter.calls == []                        # absence of a View IS the fallback


@pytest.mark.unit
def test_perceive_and_fold_records_abstention_counter_when_asked():
    llm = FakeLLM([
        [_purchase(0, "bike_spend", "2026-01-10", 185)],
        [_purchase(0, "bike_spend", "2026-01-10", 50)],
    ])
    adapter = FakeAdapter()
    res = perceive_and_fold(episodes=EPISODES, llm_complete=llm, graph_adapter=adapter, k=2,
                            record_abstentions=True)
    assert not res.committed
    counters = {c["counter"]: c["value"] for c in adapter.calls}
    assert counters["perception_abstained"] == 1.0
    # bucketed by firing veto (finding #4): the scattered $185/$50 disagreement is self-consistency
    assert counters["perception_abstained_self_consistency"] == 1.0
    assert all(c["name_embedding"] is None for c in adapter.calls)  # receipts stay out of recall


@pytest.mark.unit
def test_gate_decision_carries_structured_veto_label():
    from menhir.services.perception import (
        VETO_COMMIT, VETO_COUNT_FLOOR, VETO_SELF_CONSISTENCY,
    )
    # commit -> VETO_COMMIT; count-of-1 -> count_floor; scatter -> self_consistency
    ok = [_grp([Event(when="2026-01-10", kind="purchase", value=50, episode_uuid="e0"),
                Event(when="2026-03-02", kind="purchase", value=135, episode_uuid="e1")])]
    [d] = gate([ok for _ in range(3)], threshold=1.0)
    assert d.committed and d.veto == VETO_COMMIT
    one = _grp([Event(when="2026-01-01", kind="item", identity="only bike", episode_uuid="e0")],
               reducer="distinct_count", measure="bikes")
    [f] = gate([[one] for _ in range(3)], threshold=1.0)
    assert f.veto == VETO_COUNT_FLOOR
    a = _grp([Event(when="2026-01-10", kind="purchase", value=185, episode_uuid="e0")])
    b = _grp([Event(when="2026-01-10", kind="purchase", value=50, episode_uuid="e0")])
    [s] = gate([[a], [b], [a]], threshold=1.0)
    assert s.veto == VETO_SELF_CONSISTENCY


# ----------------------------------------------------------------------------- Part 2 tests: Law-3 coverage


@pytest.mark.unit
def test_gate_law3_cross_check_agreement_corroborates():
    # Law-3: anchor=4, post-anchor delta=1 (acquire a lens) -> reconciled value=5.
    # Cross-check (holistic) also says 5 -> agreement -> commit, triangulated=True from veto-4.
    from menhir.services.perception import VETO_COMMIT, VETO_CROSS_CHECK
    delta = Event(when="2026-02-15", kind="acquire", value=None,
                  identity="macro lens", episode_uuid="e1", category="photography")
    g = _anchor("lenses_owned", "2026-01-01", 4.0, deltas=[delta])
    # Cross-check returns 5 (agrees with reconcile)
    [d] = gate([[g] for _ in range(3)], threshold=1.0, cross_check=lambda m: 5.0)
    assert d.committed and d.value == 5.0
    assert d.triangulated is True  # earned by veto-4 agreement, not pre-assigned
    assert d.veto == VETO_COMMIT


@pytest.mark.unit
def test_gate_law3_cross_check_disagreement_abstains():
    # Law-3: anchor=4, delta=1 -> 5. Cross-check says 4 (disagrees) -> abstain with veto="cross_check".
    from menhir.services.perception import VETO_CROSS_CHECK
    delta = Event(when="2026-02-15", kind="acquire", value=None,
                  identity="macro lens", episode_uuid="e1", category="photography")
    g = _anchor("lenses_owned", "2026-01-01", 4.0, deltas=[delta])
    # Cross-check returns 4 (disagrees with reconciled 5)
    [d] = gate([[g] for _ in range(3)], threshold=1.0, cross_check=lambda m: 4.0)
    assert not d.committed and d.value is None
    assert d.veto == VETO_CROSS_CHECK
    assert "law-3 cross-check disagreed" in d.reason


@pytest.mark.unit
def test_gate_law3_no_cross_check_commits_with_triangulated_true():
    # Law-3 without cross-check injected → commits as before, triangulated=True (no second opinion,
    # so upfront True is unchanged; Part 1 experiment decides if this should be more conservative).
    from menhir.services.perception import VETO_COMMIT
    delta = Event(when="2026-02-15", kind="item", identity="macro lens", episode_uuid="e1")
    g = _anchor("lenses_owned", "2026-01-01", 4.0, deltas=[delta])
    [d] = gate([[g] for _ in range(3)], threshold=1.0, cross_check=None)
    assert d.committed and d.value == 5.0
    assert d.triangulated is True  # no cross-check injected; upfront True preserved
    assert d.veto == VETO_COMMIT


@pytest.mark.unit
def test_verify_candidate_law3_re_mention_rejection():
    # Re-mention scenario: anchor "I own 4 lenses" on 2026-01-01, then "my 50mm lens" re-mentioned
    # on 2026-02-01 (not a delta, already in the anchor). The post-anchor event is the re-mention.
    # A verifier that says "not correct" (you can't add a re-mention as a delta) -> abstain veto="verification".
    from menhir.services.perception import verify_candidate, VETO_VERIFICATION
    anchor = Event(when="2026-01-01", kind="assertion", value=4.0, episode_uuid="a0",
                   category="photography")
    # Re-mention of an already-owned lens (not a genuine delta)
    remention = Event(when="2026-02-01", kind="item", identity="50mm lens", episode_uuid="e1")
    events = [remention]
    # Fake judge that says "not correct" (re-mention should not be added as a delta)
    fake_judge = lambda system, user: '{"correct": false}'
    result = verify_candidate("lenses_owned", 5.0, events, fake_judge, k=1, anchor=anchor)
    assert result is False  # rejected by verifier


@pytest.mark.unit
def test_verify_candidate_law3_genuine_delta_acceptance():
    # Law-3: anchor=4, genuine delta "bought a macro lens" on 2026-02-15 -> 5.
    # Verifier says "correct" -> pass veto-5, commit proceeds.
    from menhir.services.perception import verify_candidate
    anchor = Event(when="2026-01-01", kind="assertion", value=4.0, episode_uuid="a0",
                   category="photography")
    delta = Event(when="2026-02-15", kind="acquire", value=None,
                  identity="macro lens", episode_uuid="e1", category="photography")
    events = [delta]
    fake_judge = lambda system, user: '{"correct": true}'
    result = verify_candidate("lenses_owned", 5.0, events, fake_judge, k=1, anchor=anchor)
    assert result is True  # approved by verifier


@pytest.mark.unit
def test_verify_candidate_prompt_shape_law3_vs_non_law3():
    # Verify that Law-3 prompts include "stated base:" and question (d),
    # while non-Law-3 prompts are byte-identical to the original.
    from menhir.services.perception import verify_candidate

    # Capture the prompts sent to the judge (judge is called with (prompt, ""))
    captured_prompts = []

    def capture_judge(prompt, _):
        captured_prompts.append(prompt)
        return '{"correct": true}'

    anchor = Event(when="2026-01-01", kind="assertion", value=4.0, episode_uuid="a0")
    delta = Event(when="2026-02-15", kind="item", identity="macro lens", episode_uuid="e1")

    # Test Law-3: with anchor
    verify_candidate("lenses", 5.0, [delta], capture_judge, k=1, anchor=anchor)
    law3_prompt = captured_prompts[-1]
    assert "stated base:" in law3_prompt
    assert "(d)" in law3_prompt
    assert "could any listed post-anchor item" in law3_prompt

    # Test non-Law-3: without anchor
    verify_candidate("lenses", 5.0, [delta], capture_judge, k=1, anchor=None)
    non_law3_prompt = captured_prompts[-1]
    assert "stated base:" not in non_law3_prompt
    assert "(d)" not in non_law3_prompt
    # Verify original prompt structure is preserved
    assert "(a)" in non_law3_prompt and "(b)" in non_law3_prompt and "(c)" in non_law3_prompt


@pytest.mark.unit
def test_gate_law3_with_verifier_integration():
    # Full Law-3 integration: cross-check agrees, verifier approves -> commit 5.
    # Photographer case (invented domain): anchor 4 lenses, buy a new one -> 5.
    from menhir.services.perception import VETO_COMMIT
    delta = Event(when="2026-02-15", kind="acquire", value=None,
                  identity="macro lens", episode_uuid="e1", category="photography")
    g = _anchor("lenses_owned", "2026-01-01", 4.0, deltas=[delta])

    # Both cross-check and verifier agree
    def fake_cross_check(m):
        return 5.0

    def fake_verifier(measure, value, events, anchor=None):
        # Just verify the anchor was passed for Law-3
        return anchor is not None and anchor.value == 4.0

    [d] = gate([[g] for _ in range(3)], threshold=1.0,
               cross_check=fake_cross_check, verifier=fake_verifier)
    assert d.committed and d.value == 5.0
    assert d.triangulated is True
    assert d.veto == VETO_COMMIT
