"""Decoupling proof: the perception gate on personal-memory scenarios with ZERO benchmark provenance.

Every other perception test uses LME-derived fixtures ($185 bike-spend, 3 plants, 20 playlists) — so
they can't distinguish "the mechanism is general" from "we fitted the benchmark". These scenarios are
invented fresh (a photographer's lenses, a reader's book tally, a renter's balance, gym visits, coffee
spend) with arbitrary values that appear in no LME question. If each mechanism — count-floor, move-1,
Lever-B cross-check, Law-3 anchor+delta, σ WINDOW — fires correctly here, it is solving the general
personal-memory problem, not the benchmark.

No graph, no live LLM: a fake extractor emits typed groups directly; the gate + folds are pure."""

from __future__ import annotations

import pytest

from menhir.domain.fold_algebra import Event, sum_
from menhir.services.perception import (
    PerceivedGroup, VETO_UNRESOLVED_COREFERENCE, _category_spend_groups, gate, resolve_coreference,
    verify_candidate,
)
from menhir.services.windowed_fold import count_in_relative_window


def _grp(subject, measure, reducer, events, stated=None, anchor=None):
    return PerceivedGroup(subject=subject, measure=measure, reducer=reducer,
                          events=list(events), stated_total=stated, stated_event=anchor)


# --------------------------------------------------------------- move-1: a stated total, novel domain


@pytest.mark.unit
def test_move1_photographer_lens_count():
    # "I own 4 camera lenses" — a stated count in a domain LME never touches. Commits 4.
    anchor = Event(when="2024-02-10", kind="assertion", value=4.0, episode_uuid="p1")
    g = _grp("user", "camera_lenses", "stated", [], stated=4.0, anchor=anchor)
    [d] = gate([[g] for _ in range(3)], threshold=1.0)
    assert d.committed and d.value == 4.0


# --------------------------------------------------------------- count-floor: a lone possession


@pytest.mark.unit
def test_count_floor_single_drone_abstains():
    # "I have a drone" -> drone_owned=1 carries no aggregation; the raw episode already has it.
    one = [Event(when="2024-03-01", kind="item", identity="my DJI drone", episode_uuid="d1")]
    g = _grp("user", "drones_owned", "distinct_count", one)
    [d] = gate([[g] for _ in range(3)], threshold=1.0)
    assert not d.committed and "count-floor" in d.reason


# --------------------------------------------------------------- Lever B: independent derivations disagree


@pytest.mark.unit
def test_cross_check_vetoes_confident_coffee_overcount():
    # itemized coffee spend sums to a unanimous $50, but a holistic read says $30 -> abstain.
    # (No user-stated total, so only the cross-check can catch this bias — the general precision guard.)
    evs = [Event(when="2024-01-05", kind="purchase", value=30.0, episode_uuid="c1"),
           Event(when="2024-01-06", kind="purchase", value=20.0, episode_uuid="c2")]
    g = _grp("user", "coffee_spend", "sum", evs)
    [d] = gate([[g] for _ in range(3)], threshold=1.0, cross_check=lambda m: 30.0)
    assert not d.committed and "cross-check failed" in d.reason
    # ...and when the holistic read AGREES, it commits (corroborated)
    [ok] = gate([[g] for _ in range(3)], threshold=1.0, cross_check=lambda m: 50.0)
    assert ok.committed and ok.value == 50.0 and ok.triangulated is True


# --------------------------------------------------------------- Law-3: anchor re-bases, deltas accrue


@pytest.mark.unit
def test_law3_reading_tally_anchor_plus_deltas():
    # "I've read 12 books this year" (anchor) then two later "finished another" -> 12 + 1 + 1 = 14.
    anchor = Event(when="2024-06-01", kind="assertion", value=12.0, episode_uuid="b0")
    deltas = [Event(when="2024-07-01", kind="acquire", episode_uuid="b1"),
              Event(when="2024-08-01", kind="acquire", episode_uuid="b2")]
    g = _grp("user", "books_read", "count", deltas, stated=12.0, anchor=anchor)
    [d] = gate([[g] for _ in range(3)], threshold=1.0)
    assert d.committed and d.value == 14.0


@pytest.mark.unit
def test_law3_rent_balance_accrues_payments():
    # a stated savings-like balance of $2000, then two later deposits -> 2000 + 300 + 150 = 2450.
    anchor = Event(when="2024-01-01", kind="assertion", value=2000.0, episode_uuid="s0")
    deltas = [Event(when="2024-02-01", kind="purchase", value=300.0, episode_uuid="s1"),
              Event(when="2024-03-01", kind="purchase", value=150.0, episode_uuid="s2")]
    g = _grp("user", "savings", "sum", deltas, stated=2000.0, anchor=anchor)
    [d] = gate([[g] for _ in range(3)], threshold=1.0)
    assert d.committed and d.value == 2450.0


# --------------------------------------------------------------- σ WINDOW: a windowed count over a timeline


@pytest.mark.unit
def test_window_gym_visits_last_week():
    # gym visits as a timeline; "how many times did I go to the gym last week" is a read-time δ.
    visits = [{"when": "2024-05-06", "what": "gym"}, {"when": "2024-05-08", "what": "gym"},
              {"when": "2024-05-10", "what": "gym"}, {"when": "2024-04-20", "what": "gym"}]  # older
    # distinct=False: occurrences, not distinct items (each visit counts)
    n = count_in_relative_window(visits, "last week", "2024-05-12", distinct=False)
    assert n == 3            # the three May visits; the April one is outside the window


# --------------------------------------------------------------- scattered perception self-abstains


@pytest.mark.unit
def test_cross_episode_dedup_prevents_sum_inflation():
    # Lever C2: the same $12 magazine subscription narrated in two episodes must not sum to $24.
    # (A domain LME never touches; proves the dedup is a general SUM-precision guard.)
    evs = [Event(when="2024-04-01", kind="purchase", value=12.0, what="magazine subscription", episode_uuid="m1"),
           Event(when="2024-04-01", kind="purchase", value=12.0, what="magazine subscription", episode_uuid="m2"),
           Event(when="2024-04-15", kind="purchase", value=8.0, what="newspaper", episode_uuid="m3")]
    g = _grp("user", "reading_spend", "sum", evs)
    [d] = gate([[g] for _ in range(3)], threshold=1.0, cross_check=lambda m: 20.0)
    assert d.committed and d.value == 20.0            # 12 + 8, not 12 + 12 + 8 = 32


@pytest.mark.unit
def test_category_grouping_sums_heterogeneous_items():
    # Lever C1: a tent, sleeping bag, and stove each tagged category='camping' -> one camping_spend
    # total, where the per-item names never group. (Non-LME domain; proves the mechanism is general.)
    items = _grp("user", "tent_spend", "sum",
                 [Event(when="2024-06-01", kind="purchase", value=200.0, identity="tent", category="camping", episode_uuid="c1")])
    bag = _grp("user", "sleeping_bag_spend", "sum",
               [Event(when="2024-06-02", kind="purchase", value=90.0, identity="sleeping bag", category="camping", episode_uuid="c2")])
    stove = _grp("user", "stove_spend", "sum",
                 [Event(when="2024-06-03", kind="purchase", value=60.0, identity="camp stove", category="camping", episode_uuid="c3")])
    synth = _category_spend_groups([items, bag, stove])
    assert len(synth) == 1
    cat = synth[0]
    assert cat.measure == "camping_spend" and cat.reducer == "sum"
    from menhir.domain.fold_algebra import sum_
    assert sum_(cat.events) == 350.0                 # 200 + 90 + 60


@pytest.mark.unit
def test_category_grouping_skips_single_item_category():
    # a lone categorized purchase is already its own measure -> no redundant category group synthesized
    solo = _grp("user", "drone_spend", "sum",
                [Event(when="2024-06-01", kind="purchase", value=500.0, identity="drone", category="photography", episode_uuid="d1")])
    assert _category_spend_groups([solo]) == []


def _judge(verdict: bool):
    # a fake coreference judge that always returns the given yes/no (bypasses the LLM)
    import json
    return lambda system, user: json.dumps({"same_purchase": verdict})


@pytest.mark.unit
def test_coreference_merges_when_judge_confident_same():
    # a $40 gadget re-narrated on two different dates (determinism can't tell from recurrence) ->
    # judge says SAME -> merged to one $40. (Non-LME; proves the determinism->LLM->confidence design.)
    evs = [Event(when="2024-05-05", kind="purchase", value=40.0, category="cycling", what="got lights", episode_uuid="e1"),
           Event(when="2024-04-20", kind="purchase", value=40.0, category="cycling", what="picked up lights", episode_uuid="e2"),
           Event(when="2024-05-05", kind="purchase", value=90.0, category="cycling", what="jersey", episode_uuid="e3")]
    merged = resolve_coreference(evs, _judge(True), k=3, threshold=1.0)
    assert sum_(merged) == 130.0                     # 40 (once) + 90, not 40 + 40 + 90
    assert merged[0].when == "2024-04-20"            # keeps the earliest mention as representative


@pytest.mark.unit
def test_coreference_leaves_recurring_purchases_when_judge_says_separate():
    # the SAME deterministic candidate shape (daily $5 coffee: same value, different days) -> judge
    # says SEPARATE -> untouched. This is the case determinism alone would wrongly merge.
    coffee = [Event(when=f"2024-06-0{d}", kind="purchase", value=5.0, category="coffee", episode_uuid=f"c{d}")
              for d in (1, 2, 3)]
    assert sum_(resolve_coreference(coffee, _judge(False), k=3, threshold=1.0)) == 15.0


@pytest.mark.unit
def test_coreference_abstains_from_merge_when_confidence_below_threshold():
    # a split judge (2 same / 1 different across k=3) is below the unanimity threshold -> NO merge
    # (precision-first: only act when confident; the SUM cross-check backstop covers the inflation).
    import json
    seq = iter([True, True, False])
    judge = lambda system, user: json.dumps({"same_purchase": next(seq)})
    evs = [Event(when="2024-05-05", kind="purchase", value=40.0, category="cycling", episode_uuid="e1"),
           Event(when="2024-04-20", kind="purchase", value=40.0, category="cycling", episode_uuid="e2")]
    assert sum_(resolve_coreference(evs, judge, k=3, threshold=1.0)) == 80.0   # not merged


@pytest.mark.unit
def test_verify_candidate_confirms_clean_itemization():
    import json
    evs = [Event(when="2024-05-05", kind="purchase", value=40.0, category="cycling", what="lights", episode_uuid="e1"),
           Event(when="2024-05-05", kind="purchase", value=90.0, category="cycling", what="jersey", episode_uuid="e2")]
    judge = lambda system, user: json.dumps({"correct": True})
    assert verify_candidate("cycling_spend", 130.0, evs, judge, k=3, threshold=1.0) is True


@pytest.mark.unit
def test_unresolved_coreference_veto_abstains_and_judge_resolves():
    # invented domain (aquarium supplies): two same-day, same-value, same-category purchases with
    # DIFFERENT wording — the ambiguous cluster the narrowed signature no longer merges. Might be one
    # $30 purchase re-quoted, or two distinct $30 items; determinism can't tell.
    def mk():
        return [Event(when="2024-03-01", kind="purchase", value=30.0, what="a heater", category="aquarium", episode_uuid="a1"),
                Event(when="2024-03-01", kind="purchase", value=30.0, what="a filter", category="aquarium", episode_uuid="a2")]

    # (a) coref disabled (memo None): the ambiguity is never judged -> ABSTAIN, not a silent fold.
    [d] = gate([[_grp("user", "aquarium_spend", "sum", mk())] for _ in range(3)], threshold=1.0)
    assert not d.committed and d.veto == VETO_UNRESOLVED_COREFERENCE

    # (b) judge confidently SEPARATE -> both kept, resolved -> commits the full $60.
    sep = {("aquarium", "30.00"): "separate"}
    [c] = gate([[_grp("user", "aquarium_spend", "sum", mk())] for _ in range(3)],
               threshold=1.0, coref_resolved=sep)
    assert c.committed and c.value == 60.0

    # (c) judge confidently MERGE (real resolve_coreference path): collapses to one, commits $30.
    memo: dict = {}
    merge_judge = lambda s, u: '{"same_purchase": true}'
    samples = [[_grp("user", "aquarium_spend", "sum", resolve_coreference(mk(), merge_judge, k=3, memo=memo))]
               for _ in range(3)]
    [m] = gate(samples, threshold=1.0, coref_resolved=memo)
    assert m.committed and m.value == 30.0

    # (d) a SPLIT judge -> unsure -> still abstains (ambiguity not settled).
    votes = iter([True, False, True] * 3)
    split_judge = lambda s, u: '{"same_purchase": true}' if next(votes) else '{"same_purchase": false}'
    memo2: dict = {}
    samples2 = [[_grp("user", "aquarium_spend", "sum", resolve_coreference(mk(), split_judge, k=3, memo=memo2))]]
    [u] = gate(samples2, threshold=1.0, coref_resolved=memo2)
    assert not u.committed and u.veto == VETO_UNRESOLVED_COREFERENCE


@pytest.mark.unit
def test_verify_candidate_gate_vetoes_when_audit_fails():
    # the final gate: even a self-consistent SUM is abstained if the audit over its items says wrong
    # (e.g. it spots a double-count) — a focused second opinion, no blind re-derivation.
    import json
    # distinct values + items so the pair is NOT a coreference candidate (that veto is exercised
    # separately) — this test isolates the verification (audit) veto.
    evs = [Event(when="2024-05-05", kind="purchase", value=40.0, identity="helmet", category="cycling", episode_uuid="e1"),
           Event(when="2024-05-06", kind="purchase", value=35.0, identity="lights", category="cycling", episode_uuid="e2")]
    g = _grp("user", "cycling_spend", "sum", evs)
    veto = lambda measure, value, events: False       # audit says "not correct"
    [d] = gate([[g] for _ in range(3)], threshold=1.0, verifier=veto)
    assert not d.committed and "verification failed" in d.reason
    # ...and a passing audit commits
    ok = lambda measure, value, events: True
    [c] = gate([[g] for _ in range(3)], threshold=1.0, verifier=ok)
    assert c.committed and c.value == 75.0


@pytest.mark.unit
def test_verify_candidate_skips_bare_stated_total():
    # a move-1 'stated' commit has no itemization to audit -> verifier not consulted, still commits
    anchor = Event(when="2024-01-01", kind="assertion", value=4.0, episode_uuid="p1")
    g = _grp("user", "camera_lenses", "stated", [], stated=4.0, anchor=anchor)
    called = {"n": 0}
    def veto(measure, value, events):
        called["n"] += 1; return False
    [d] = gate([[g] for _ in range(3)], threshold=1.0, verifier=veto)
    assert d.committed and d.value == 4.0 and called["n"] == 0


@pytest.mark.unit
def test_scattered_extraction_abstains_regardless_of_domain():
    # a subject the samples disagree on (a plant collection guessed 5/6/7) -> abstain, any domain.
    a = _grp("user", "houseplants", "distinct_count",
             [Event(when="2024-01-01", kind="item", identity=f"plant{i}", episode_uuid=f"h{i}") for i in range(5)])
    b = _grp("user", "houseplants", "distinct_count",
             [Event(when="2024-01-01", kind="item", identity=f"plant{i}", episode_uuid=f"h{i}") for i in range(7)])
    [d] = gate([[a], [b], [a]], threshold=1.0)
    assert not d.committed and "scattered" in d.reason
