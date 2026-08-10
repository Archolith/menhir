"""A6 recall-time windowed-count resolver — intent detection + timeline match + windowed δ, all
against a fake ViewRepository (no graph). The 3a704032 shape: "how many plants did I acquire in the
last month?" -> the plants_acquired timeline -> 3 under the calendar window."""

from __future__ import annotations

import pytest

from menhir.services.windowed_recall import answer_windowed_count, detect_windowed_count


class _FakeViews:
    def __init__(self, timelines: dict[str, list[dict]]):
        self._tl = timelines  # subject -> entries

    def list_views(self, *, kind=None, namespace=None, limit=100):
        return [{"subject": s} for s in self._tl]

    def fetch_timeline(self, *, subject, namespace=None):
        if subject not in self._tl:
            return None
        return {"uuid": f"tl-{subject}", "subject": subject, "entries": self._tl[subject]}


_PLANTS = {"plants_acquired": [
    {"when": "2023-04-23", "what": "snake plant"},
    {"when": "2023-05-07", "what": "peace lily"},
    {"when": "2023-05-07", "what": "succulent"},
]}


@pytest.mark.unit
def test_detect_windowed_count_parses_noun_and_window():
    d = detect_windowed_count("How many plants did I acquire in the last month?")
    assert d == {"noun": "plants", "window": "last month"}
    assert detect_windowed_count("What is my bike mileage?") is None       # not a count intent
    assert detect_windowed_count("How many books do I own?")["window"] is None  # all-time count


@pytest.mark.unit
def test_answer_matches_gold_under_calendar_window():
    views = _FakeViews(_PLANTS)
    ans = answer_windowed_count(views, namespace="lme-3a704032",
                                query="How many plants did I acquire in the last month?",
                                reference_date="2023-05-28", calendar=True)
    assert ans["subject"] == "plants_acquired" and ans["count"] == 3          # snake plant included
    assert ans["since"] == "2023-04-01" and ans["n_entries"] == 3


@pytest.mark.unit
def test_answer_rolling_window_is_stricter():
    views = _FakeViews(_PLANTS)
    ans = answer_windowed_count(views, namespace="lme-3a704032",
                                query="How many plants did I acquire in the last month?",
                                reference_date="2023-05-28", calendar=False)
    assert ans["count"] == 2                                                  # snake plant 5 days early


@pytest.mark.unit
def test_noun_matches_subject_by_token_overlap():
    views = _FakeViews({"fish_tanks_owned": [{"when": "2023-05-01", "what": "5-gallon"}]})
    ans = answer_windowed_count(views, namespace="ns",
                                query="How many tanks do I have?", reference_date="2023-06-01")
    assert ans["subject"] == "fish_tanks_owned" and ans["count"] == 1 and ans["window"] is None


@pytest.mark.unit
def test_non_count_query_and_no_match_return_none():
    views = _FakeViews(_PLANTS)
    assert answer_windowed_count(views, namespace="ns", query="What did I buy?",
                                 reference_date="2023-05-28") is None          # not a count
    assert answer_windowed_count(views, namespace="ns", query="How many cars do I own?",
                                 reference_date="2023-05-28") is None          # no matching timeline
