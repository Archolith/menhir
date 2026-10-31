"""Related-count matching (unit 4R): the pure related_sets / conservative_decisions rules."""

from __future__ import annotations

import pytest

from menhir.domain.count_match import (
    RELATED_PER_CANDIDATE,
    RELATED_PER_PAGE,
    conservative_decisions,
    related_sets,
    relation_tokens,
)


def _r(aid, day=1, **over):
    base = dict(
        assertion_id=aid, subject_uuid="ent-self", value_kind="count", attribute="model_kit_count",
        count_object="model kit", count_item="", valid_at=f"2026-07-{day:02d}T00:00:00+00:00",
        episode_uuid=f"ep-{aid}",
    )
    base.update(over)
    return base


def _decide(cands, pool=(), **kw):
    sets = related_sets(list(cands), list(pool), **kw)
    return {d["assertion_id"]: d for d in conservative_decisions(sets)}, sets


@pytest.mark.unit
@pytest.mark.parametrize("reverse", [False, True])
def test_two_retellings_in_one_page_earlier_counts_later_uncertain(reverse):
    # Fails if candidates are not related to EARLIER page candidates, or order ignores valid_at.
    early, late = _r("a", 1), _r("b", 5)
    cands = [late, early] if reverse else [early, late]
    dec, sets = _decide(cands)
    assert [s.candidate["assertion_id"] for s in sets] == ["a", "b"]
    assert dec["a"]["status"] == "counted" and dec["a"]["reason"] == "no_related"
    assert dec["b"]["status"] == "uncertain" and dec["b"]["reason"] == "related_undecided"


@pytest.mark.unit
def test_related_to_a_pool_row_is_uncertain():
    dec, _ = _decide([_r("n", 5)], pool=[_r("old", 1, count_status="counted")])
    assert dec["n"]["status"] == "uncertain" and dec["n"]["reason"] == "related_undecided"


@pytest.mark.unit
def test_no_related_is_counted_with_normalized_family():
    dec, sets = _decide([_r("a", count_object="Model Kits")], pool=[_r("x", subject_uuid="other")])
    d = dec["a"]
    assert d["status"] == "counted" and d["truncated"] is False
    assert d["family"] == "model kit" and d["target_id"] == ""
    assert sets[0].related == ()


@pytest.mark.unit
def test_truncation_one_past_the_per_candidate_bound_but_not_at_it():
    # Fails if the per-candidate bound is off by one.
    cap = RELATED_PER_CANDIDATE
    pool10 = [_r(f"p{i:02d}", 1) for i in range(cap)]
    dec10, sets10 = _decide([_r("n", 9)], pool=pool10)
    assert len(sets10[0].related) == cap and sets10[0].truncated is False
    assert dec10["n"]["status"] == "uncertain" and dec10["n"]["reason"] == "related_undecided"

    dec11, sets11 = _decide([_r("n", 9)], pool=pool10 + [_r("p99", 1)])
    assert len(sets11[0].related) == cap and sets11[0].truncated is True
    assert dec11["n"]["status"] == "uncertain" and dec11["n"]["reason"] == "truncated"
    assert dec11["n"]["truncated"] is True


@pytest.mark.unit
def test_related_rows_are_ranked_by_shared_tokens_then_id():
    pool = [_r("z-two", count_object="model kit"), _r("a-one", count_object="kit"),
            _r("b-two", count_object="model kit")]
    _, sets = _decide([_r("n", 9, count_object="model kit")], pool=pool)
    assert [r["assertion_id"] for r in sets[0].related] == ["b-two", "z-two", "a-one"]


@pytest.mark.unit
def test_page_budget_truncates_later_candidates():
    # per_candidate 2, per_page 3: first candidate sends 2, second only has 1 left (it has 2 related).
    pool = [_r("p1", 1), _r("p2", 1)]
    cands = [_r("c1", 2), _r("c2", 3)]
    sets = related_sets(cands, pool, per_candidate=2, per_page=3)
    c1, c2 = sets
    assert len(c1.related) == 2 and c1.truncated is False
    # c2 relates to p1, p2 and c1 (3 > per_candidate -> truncated anyway); budget leaves 1
    assert len(c2.related) == 1 and c2.truncated is True


@pytest.mark.unit
def test_page_budget_cuts_a_candidate_that_was_within_per_candidate():
    pool = [_r("p1", 1), _r("p2", 1)]
    # c1 sees p1,p2 (2); c2 sees p1,p2 (2) but only 1 budget remains.
    sets = related_sets([_r("c1", 2, count_object="kit"), _r("c2", 3, subject_uuid="x"),
                         _r("c3", 4, count_object="kit")], pool, per_candidate=5, per_page=3)
    c1, c2, c3 = sets
    assert len(c1.related) == 2 and not c1.truncated
    assert c2.related == () and not c2.truncated      # other subject, spends nothing
    assert len(c3.related) == 1 and c3.truncated      # wanted p1,p2,c1 = 3, budget 1


@pytest.mark.unit
def test_page_budget_with_real_defaults():
    # Five candidates (own subject each), each related to a full set of own pool rows. The page
    # budget holds four full sets, so the fifth has nothing left: truncated, never counted.
    cap = RELATED_PER_CANDIDATE
    assert RELATED_PER_PAGE == 4 * cap
    pool = [_r(f"p{s}-{i:02d}", 1, subject_uuid=f"s{s}") for s in range(5) for i in range(cap)]
    cands = [_r(f"c{s}", 2 + s, subject_uuid=f"s{s}") for s in range(5)]
    sets = related_sets(cands, pool)
    assert [len(s.related) for s in sets] == [cap, cap, cap, cap, 0]
    assert [s.truncated for s in sets] == [False, False, False, False, True]
    dec = conservative_decisions(sets)
    assert [d["status"] for d in dec] == ["uncertain"] * 4 + ["uncertain"]
    assert [d["reason"] for d in dec] == ["related_undecided"] * 4 + ["truncated"]


@pytest.mark.unit
def test_pool_truncated_makes_every_candidate_uncertain():
    dec, _ = _decide([_r("a", 1), _r("b", 2, subject_uuid="other")], pool_truncated=True)
    assert all(d["status"] == "uncertain" and d["reason"] == "truncated" for d in dec.values())


@pytest.mark.unit
def test_same_item_different_subject_is_not_related():
    a = _r("a", 1, count_item="Spitfire", subject_uuid="alice")
    b = _r("b", 2, count_item="Spitfire", subject_uuid="bob")
    dec, _ = _decide([a, b])
    assert dec["a"]["status"] == "counted" and dec["b"]["status"] == "counted"


@pytest.mark.unit
def test_different_value_kind_is_not_related():
    pool = [_r("m", 1, value_kind="money", count_object="model kit")]
    dec, _ = _decide([_r("n", 5)], pool=pool)
    assert dec["n"]["status"] == "counted"
    # value_kind compare is case-insensitive
    dec2, _ = _decide([_r("n", 5)], pool=[_r("m", 1, value_kind="COUNT")])
    assert dec2["n"]["status"] == "uncertain"


@pytest.mark.unit
def test_no_object_falls_back_to_attribute_minus_suffix_and_drops_action_words():
    no_obj = _r("n", 5, count_object="", attribute="gym_visit_count")
    assert relation_tokens(no_obj) == ("gym",)  # 'visit' is an action word
    dec, sets = _decide([no_obj], pool=[_r("g", 1, count_object="gym", attribute="x_count")])
    assert dec["n"]["status"] == "uncertain" and dec["n"]["reason"] == "related_undecided"
    assert [r["assertion_id"] for r in sets[0].related] == ["g"]
    # and the spend suffix is stripped too
    assert relation_tokens(_r("s", count_object="", attribute="concert_spend")) == ("concert",)


@pytest.mark.unit
def test_candidate_without_family_tokens_is_uncertain_no_family():
    cand = _r("n", count_object="", attribute="visit_count")  # only an action word remains
    assert relation_tokens(cand) == ()
    dec, _ = _decide([cand])
    assert dec["n"]["status"] == "uncertain" and dec["n"]["reason"] == "no_family"
    assert dec["n"]["family"] == ""


@pytest.mark.unit
def test_candidate_never_relates_to_itself_and_shared_ids_count_once():
    a = _r("a", 1)
    dec, sets = _decide([a], pool=[a])
    assert sets[0].related == () and dec["a"]["status"] == "counted"

    # an id in both pool and candidates is one related row for the later candidate, not two
    b = _r("b", 5)
    _, sets2 = _decide([a, b], pool=[a])
    by_id = {s.candidate["assertion_id"]: s for s in sets2}
    assert by_id["a"].related == ()
    assert [r["assertion_id"] for r in by_id["b"].related] == ["a"]


@pytest.mark.unit
def test_ties_in_valid_at_order_by_episode_then_id():
    x = _r("x", 1, episode_uuid="ep-2")
    y = _r("y", 1, episode_uuid="ep-1")
    dec, sets = _decide([x, y])
    assert [s.candidate["assertion_id"] for s in sets] == ["y", "x"]
    assert dec["y"]["status"] == "counted" and dec["x"]["status"] == "uncertain"


@pytest.mark.unit
def test_relation_needles_are_a_superset_of_token_relation():
    # Fails if a row sharing a relation token with the candidate is missed by every needle.
    from menhir.domain.count_match import relation_needles

    cand = _r("c", count_object="model kit")
    needles = relation_needles(cand)
    assert needles == ("model", "kit")
    rows = [
        _r("r1", count_object="model kits"),
        _r("r2", count_object="", attribute="model_kit_count"),
        _r("r3", count_object="Museum", attribute="museum_visit_count"),
    ]
    for r in rows:
        shares = bool(set(relation_tokens(cand)) & set(relation_tokens(r)))
        blob = " ".join(str(r.get(f) or "").lower() for f in ("count_family", "count_object", "attribute"))
        assert shares == any(n in blob for n in needles) or not shares
        if shares:
            assert any(n in blob for n in needles)
    kitten = _r("k", count_object="kitten", attribute="kitten_count")
    assert not set(relation_tokens(cand)) & set(relation_tokens(kitten))
    # "kit" is a substring of "kitten": a superset may over-match, never under-match.
    assert relation_needles(_r("b", count_object="batteries")) == ("batter",)
    assert any(n in "battery" for n in relation_needles(_r("b", count_object="batteries")))
    assert relation_needles(_r("e", count_object="", attribute="")) == ()


@pytest.mark.unit
def test_under_the_cap_decisions_are_unchanged():
    # Fails if the needle-filtered pool relates or truncates any candidate differently from the full pool.
    from menhir.domain.count_match import relation_needles

    objs = ["model kit", "model train", "kit car", "batteries", "battery", "museum", "kitten"]
    pool = []
    for i in range(30):
        obj = objs[i % len(objs)]
        pool.append(_r(f"p{i}", day=1 + i % 9, subject_uuid=f"s{i % 2}",
                       value_kind="count" if i % 3 else "money", count_object=obj,
                       attribute=obj.replace(" ", "_") + "_count"))
    cands = [_r("c1", 20, subject_uuid="s0", count_object="model kit"),
             _r("c2", 21, subject_uuid="s1", count_object="batteries", value_kind="money"),
             _r("c3", 22, subject_uuid="s0", count_object="kit car")]
    cosine = {("c1", "p5"): 0.9, ("c3", "p2"): 0.95}
    cos_ids = {"p5", "p2"}

    def keep(c):
        needles = relation_needles(c)

        def hit(r):
            blob = " ".join(str(r.get(f) or "").lower() for f in ("count_family", "count_object", "attribute"))
            return any(n in blob for n in needles)
        return [r for r in pool if r["subject_uuid"] == c["subject_uuid"]
                and r["value_kind"] == c["value_kind"] and (hit(r) or r["assertion_id"] in cos_ids)]

    filtered = {}
    for c in cands:
        for r in keep(c):
            filtered[r["assertion_id"]] = r
    full = related_sets(cands, pool, cosine=cosine)
    part = related_sets(cands, list(filtered.values()), cosine=cosine)
    for a, b in zip(full, part):
        assert [r["assertion_id"] for r in a.related] == [r["assertion_id"] for r in b.related]
        assert a.truncated == b.truncated
