"""Fold Algebra reducers (move 2). The four monoids + σ/δ helpers, checked against the real D0
demand (bike-spend SUM=$185, tanks DISTINCT-COUNT=3, acquisitions COUNT=3) and their monoid laws."""

from __future__ import annotations

import pytest

from menhir.domain.fold_algebra import (
    Event,
    coreference_candidates,
    count,
    datediff_days,
    dedup_events,
    distinct,
    distinct_count,
    exclude_folded,
    latest,
    sum_,
    timeline,
    window,
)


def _purchases():  # bike-spend, gold $185, spread across dated events (never stated as a total)
    return [
        Event(when="2026-01-10", kind="purchase", value=50, episode_uuid="e1"),
        Event(when="2026-03-02", kind="purchase", value=135, episode_uuid="e2"),
    ]


def _tanks():  # 'how many fish tanks' gold 3 — mentioned separately, with a duplicate mention
    return [
        Event(when="2026-01-01", kind="item", identity="5-gallon tank", episode_uuid="e1"),
        Event(when="2026-02-01", kind="item", identity="20-gallon tank", episode_uuid="e2"),
        Event(when="2026-02-01", kind="item", identity="5-gallon tank", episode_uuid="e3"),  # dup
        Event(when="2026-03-01", kind="item", identity="1-gallon tank", episode_uuid="e4"),
    ]


@pytest.mark.unit
def test_sum_matches_bike_spend():
    assert sum_(_purchases()) == 185.0
    assert sum_([]) == 0.0                     # identity


@pytest.mark.unit
def test_sum_is_associative_monoid():
    a = _purchases()[:1]
    b = _purchases()[1:]
    assert sum_(a) + sum_(b) == sum_(a + b)     # combine == +, over the partition


@pytest.mark.unit
def test_count_matches_acquisitions():
    acquisitions = [Event(when=f"2026-0{i}-01", kind="acquire", episode_uuid=f"e{i}") for i in (1, 2, 3)]
    assert count(acquisitions) == 3


@pytest.mark.unit
def test_distinct_count_dedups_items():
    assert distinct_count(_tanks()) == 3        # 4 mentions, 3 distinct identities
    assert distinct(_tanks()) == {"5-gallon tank", "20-gallon tank", "1-gallon tank"}


@pytest.mark.unit
def test_distinct_is_idempotent():
    once = distinct(_tanks())
    twice = distinct(_tanks() + _tanks())       # replay -> same set (idempotent monoid)
    assert once == twice


@pytest.mark.unit
def test_latest_is_max_by_when():
    lat = latest(_purchases())
    assert lat is not None and lat.when == "2026-03-02"


@pytest.mark.unit
def test_timeline_sorts_and_dedups():
    tl = timeline(_tanks())
    whens = [e["when"] for e in tl]
    assert whens == sorted(whens)               # ascending
    # the two 2026-02-01 entries have distinct identities -> both kept; a true dup would collapse
    assert len(tl) == 4


@pytest.mark.unit
def test_datediff_days_span():
    assert datediff_days(_purchases()) == 51     # 2026-01-10 -> 2026-03-02


@pytest.mark.unit
def test_window_filters_by_bounds():
    got = window(_purchases(), since="2026-02-01")
    assert [e.value for e in got] == [135]


@pytest.mark.unit
def test_exclude_folded_is_law2_incremental_guard():
    events = _purchases()
    # e1 already folded into the View -> only e2 is new; distinct events preserved
    new = exclude_folded(events, already_folded={"e1"})
    assert [e.episode_uuid for e in new] == ["e2"]
    assert sum_(new) == 135.0


@pytest.mark.unit
def test_dedup_events_collapses_one_occurrence_across_episodes():
    # the same $40 purchase narrated in two DIFFERENT episodes (same kind/value/item/day) -> one $40,
    # not $80. This is the cross-episode Law-2 hazard exclude_folded can't catch (both in one batch).
    evs = [Event(when="2023-05-05", kind="purchase", value=40.0, what="bike lights", episode_uuid="e1"),
           Event(when="2023-05-05", kind="purchase", value=40.0, what="bike lights", episode_uuid="e2")]
    deduped = dedup_events(evs)
    assert len(deduped) == 1 and sum_(deduped) == 40.0
    assert deduped[0].episode_uuid == "e1"                   # order-preserving, keeps first mention


@pytest.mark.unit
def test_dedup_events_does_not_merge_same_day_category_on_wording_alone():
    # §4d hazard (was encoded as a FEATURE): two same-day, same-value, same-category purchases with
    # DIFFERENT wording must NOT silently collapse — they might be one purchase re-quoted or two
    # distinct items ($40 lights + $40 pump), and merging on category alone is a directional wrong-low
    # bet. dedup keeps them; the ambiguity surfaces as a coreference candidate for the judge instead.
    evs = [Event(when="2024-06-01", kind="purchase", value=40.0, what="got new lights", category="biking", episode_uuid="e1"),
           Event(when="2024-06-01", kind="purchase", value=40.0, what="picked up a pump", category="biking", episode_uuid="e2")]
    assert len(dedup_events(evs)) == 2 and sum_(dedup_events(evs)) == 80.0
    clusters = coreference_candidates(evs)
    assert len(clusters) == 1 and len(clusters[0]) == 2   # routed to the semantic judge, not merged


@pytest.mark.unit
def test_dedup_events_keeps_recurring_purchases_on_different_days():
    # the safety boundary: same value + same category but DIFFERENT days = separate occurrences (a
    # recurring habit, e.g. daily coffee) — MUST NOT merge. This is why the day stays in the signature
    # and why same-purchase-different-inferred-date coreference is left to a semantic layer, not here.
    coffee = [Event(when=f"2024-06-0{d}", kind="purchase", value=5.0, category="coffee", episode_uuid=f"c{d}")
              for d in (1, 2, 3)]
    assert len(dedup_events(coffee)) == 3 and sum_(dedup_events(coffee)) == 15.0


@pytest.mark.unit
def test_coreference_candidates_flags_same_value_different_day():
    # C3: same category + same value + DIFFERENT days = the ambiguous cluster determinism can't
    # resolve (same-purchase-re-narrated vs recurring). Flagged as a candidate for the LLM judge.
    evs = [Event(when="2023-05-05", kind="purchase", value=40.0, category="biking", what="got lights", episode_uuid="e1"),
           Event(when="2023-04-20", kind="purchase", value=40.0, category="biking", what="also got lights", episode_uuid="e2"),
           Event(when="2023-05-05", kind="purchase", value=120.0, category="biking", what="helmet", episode_uuid="e3")]
    clusters = coreference_candidates(evs)
    assert len(clusters) == 1 and len(clusters[0]) == 2      # the two $40 lights; helmet ($120) excluded


@pytest.mark.unit
def test_coreference_candidates_ignores_identical_wording_and_singletons():
    # same day + IDENTICAL wording -> exact dedup's job, not a candidate; a lone event -> nothing to merge
    identical = [Event(when="2023-05-05", kind="purchase", value=40.0, category="biking", what="bike lights", episode_uuid="e1"),
                 Event(when="2023-05-05", kind="purchase", value=40.0, category="biking", what="bike lights", episode_uuid="e2")]
    assert coreference_candidates(identical) == []
    assert coreference_candidates(identical[:1]) == []


@pytest.mark.unit
def test_coreference_candidates_flags_same_day_different_wording():
    # same day + same value/category but DIFFERENT wording -> the case the narrowed signature no longer
    # merges; now an ambiguous candidate for the judge (one purchase re-quoted vs two distinct items).
    same_day = [Event(when="2023-05-05", kind="purchase", value=40.0, category="biking", what="new lights", episode_uuid="e1"),
                Event(when="2023-05-05", kind="purchase", value=40.0, category="biking", what="a pump", episode_uuid="e2")]
    clusters = coreference_candidates(same_day)
    assert len(clusters) == 1 and len(clusters[0]) == 2


@pytest.mark.unit
def test_dedup_events_keeps_genuinely_distinct_events():
    # different value, different day, or different item -> separate occurrences, all kept
    evs = [Event(when="2023-05-05", kind="purchase", value=40.0, what="lights", episode_uuid="e1"),
           Event(when="2023-05-06", kind="purchase", value=40.0, what="lights", episode_uuid="e2"),  # other day
           Event(when="2023-05-05", kind="purchase", value=25.0, what="lights", episode_uuid="e3"),  # other value
           Event(when="2023-05-05", kind="purchase", value=40.0, what="helmet", episode_uuid="e4")]  # other item
    assert len(dedup_events(evs)) == 4 and sum_(dedup_events(evs)) == 145.0
