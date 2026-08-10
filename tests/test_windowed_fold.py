"""σ WINDOW (Lever A) — the timeline sink + read-time windowed δ. Checked against the D0 demand
(`3a704032`: 3 plants acquired in the last month, among owned-but-not-acquired distractors), with no
graph or LLM: the sink uses a fake record_timeline adapter, the read side is pure fold_algebra."""

from __future__ import annotations

import pytest

from menhir.domain.fold_algebra import Event
from menhir.services.event_fold import fold_events_to_timeline
from menhir.services.windowed_fold import (
    count_in_relative_window,
    count_in_window,
    resolve_window,
)


class _FakeTimelineAdapter:
    def __init__(self):
        self.calls = []

    def record_timeline(self, **kwargs):
        self.calls.append(kwargs)
        return {"uuid": "u", "view_key": "k", "count": len(kwargs["entries"]), "created": True}


def _acquisitions():
    # three dated plant acquisitions (the gold-3 set), keyed to one measure via identity
    return [
        Event(when="2023-05-07", kind="acquire", identity="peace lily", episode_uuid="e0"),
        Event(when="2023-05-07", kind="acquire", identity="succulent", episode_uuid="e1"),
        Event(when="2023-05-20", kind="acquire", identity="snake plant", episode_uuid="e2"),
    ]


# ------------------------------------------------------------------- A2 timeline sink


@pytest.mark.unit
def test_fold_events_to_timeline_writes_dated_entries_keyed_on_item():
    adapter = _FakeTimelineAdapter()
    res = fold_events_to_timeline(graph_adapter=adapter, subject="plants_acquired",
                                  events=_acquisitions(), namespace="lme-3a704032")
    assert res["entries"] == 3 and len(adapter.calls) == 1
    entries = adapter.calls[0]["entries"]
    assert [e["what"] for e in entries] == ["peace lily", "succulent", "snake plant"]  # item in `what`
    assert adapter.calls[0]["subject"] == "plants_acquired"                            # aggregate key


@pytest.mark.unit
def test_fold_events_to_timeline_dedups_replayed_mention():
    adapter = _FakeTimelineAdapter()
    dup = _acquisitions() + [Event(when="2023-05-07", kind="acquire", identity="peace lily",
                                   episode_uuid="e9")]  # same item, same day, mentioned again
    res = fold_events_to_timeline(graph_adapter=adapter, subject="plants_acquired", events=dup)
    assert res["entries"] == 3                                   # (when, item) dedup — not 4


# ------------------------------------------------------------------- A4 window resolver


@pytest.mark.unit
def test_resolve_window_relative_phrases():
    ref = "2023-05-28"
    assert resolve_window("last month", ref) == ("2023-04-28", "2023-05-28")
    assert resolve_window("past 2 weeks", ref) == ("2023-05-14", "2023-05-28")
    assert resolve_window("last 10 days", ref) == ("2023-05-18", "2023-05-28")
    assert resolve_window("this year", ref) == ("2023-01-01", "2023-05-28")
    assert resolve_window("in total", ref) == (None, None)      # unbounded -> count everything


@pytest.mark.unit
def test_resolve_window_calendar_vs_rolling_month():
    ref = "2023-05-28"
    assert resolve_window("last month", ref) == ("2023-04-28", "2023-05-28")            # rolling (default)
    assert resolve_window("last month", ref, calendar=True) == ("2023-04-01", "2023-05-28")  # calendar
    # the D0 3a704032 payoff: an acquisition dated 5 days before the rolling boundary is IN under
    # the calendar reading (the natural sense of "in the last month") -> recovers gold 3
    entries = [{"when": "2023-04-23", "what": "snake plant"},
               {"when": "2023-05-07", "what": "peace lily"},
               {"when": "2023-05-07", "what": "succulent"}]
    since_c, until_c = resolve_window("last month", ref, calendar=True)
    assert count_in_window(entries, since=since_c, until=until_c) == 3
    since_r, until_r = resolve_window("last month", ref)
    assert count_in_window(entries, since=since_r, until=until_r) == 2


@pytest.mark.unit
def test_sub_months_clamps_day_and_year_rolls():
    # 2023-03-31 minus one month must clamp to Feb 28 (not error), and Jan->prev-year rolls
    assert resolve_window("last month", "2023-03-31") == ("2023-02-28", "2023-03-31")
    assert resolve_window("last month", "2023-01-15") == ("2022-12-15", "2023-01-15")


# ------------------------------------------------------------------- A3 read-time windowed count


@pytest.mark.unit
def test_count_in_window_bounds_are_inclusive_and_distinct():
    entries = [{"when": "2023-05-07", "what": "peace lily"},
               {"when": "2023-05-07", "what": "succulent"},
               {"when": "2023-05-20", "what": "snake plant"},
               {"when": "2023-03-01", "what": "old fern"}]          # out of window
    assert count_in_window(entries, since="2023-04-28", until="2023-05-28") == 3
    assert count_in_window(entries, since="2023-04-28") == 3        # fern still excluded (before since)
    assert count_in_window(entries) == 4                            # unbounded -> all


@pytest.mark.unit
def test_count_in_window_distinct_vs_occurrences():
    entries = [{"when": "2023-05-07", "what": "peace lily"},
               {"when": "2023-05-08", "what": "peace lily"}]        # same item, two mentions
    assert count_in_window(entries, distinct=True) == 1
    assert count_in_window(entries, distinct=False) == 2


@pytest.mark.unit
def test_count_in_window_tolerates_slash_dates():
    # LME episode dates (and LLM echoes) are slash-format; they must not silently drop from the window
    entries = [{"when": "2023/05/07", "what": "peace lily"},
               {"when": "2023/05/20", "what": "snake plant"}]
    assert count_in_window(entries, since="2023-04-28", until="2023-05-28") == 2


@pytest.mark.unit
def test_count_in_relative_window_end_to_end():
    # the 3a704032 shape: 3 acquired in the last month relative to the question date
    entries = [{"when": "2023-05-07", "what": "peace lily"},
               {"when": "2023-05-07", "what": "succulent"},
               {"when": "2023-05-20", "what": "snake plant"},
               {"when": "2023-02-10", "what": "fern (owned earlier)"}]
    assert count_in_relative_window(entries, "last month", "2023-05-28") == 3
