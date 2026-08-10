"""Provenance-linked View suppression planner (Step 7b) -- pure, offline.

Covers the conservative intent mapping and the planner: only a provenance row whose source
episode is actually present as a candidate, on an explicit current-state query, with a gate PASS,
is suppressed. History/as-of/bare queries never suppress; a row not in the candidate set is ignored.
"""

from __future__ import annotations

import pytest

from menhir.domain.scalar_view_authority import QueryIntent
from menhir.domain.scalar_view_suppression import (
    authority_query_intent,
    plan_view_suppression,
)

NOW = "2026-07-20T15:00:00-05:00"
OLD = "2026-07-01T00:00:00-05:00"


def _row(**over):
    base = dict(
        candidate_uuid="ep-stale", subject_uuid="self", attribute="owned", scope="",
        value_kind="count", unit="", view_value=37, view_valid_at=NOW, evidence_tier="agent",
        view_current=True, is_sole_current=True, stale_value=20, stale_valid_at=OLD,
    )
    base.update(over)
    return base


# --------------------------------------------------------------- intent mapping
@pytest.mark.parametrize("q,expected", [
    ("How many coins do I own now?", QueryIntent.CURRENT_STATE),
    ("What is my current wake time?", QueryIntent.CURRENT_STATE),
    ("How many coins did I own, previously?", QueryIntent.PREVIOUS_VALUE),
    ("What did my day off used to be?", QueryIntent.PREVIOUS_VALUE),
    ("What did I own as of 2026-01-01?", QueryIntent.COMPARISON),
    ("How many coins do I own?", QueryIntent.AMBIGUOUS),   # no explicit cue -> advisory
    ("", QueryIntent.AMBIGUOUS),
])
def test_authority_query_intent(q, expected):
    assert authority_query_intent(q) == expected


# --------------------------------------------------------------- planner: happy path
def test_current_query_suppresses_provenance_linked_candidate():
    plan = plan_view_suppression("How many coins do I own now?", [_row()], {"ep-stale", "ep-other"})
    assert plan.intent is QueryIntent.CURRENT_STATE
    assert plan.suppressed_uuids == frozenset({"ep-stale"}) and plan.any_suppressed
    assert plan.decisions[0].outcome == "suppress" and plan.decisions[0].gate == "pass"


def test_historical_query_suppresses_nothing():
    plan = plan_view_suppression("How many coins did I used to own?", [_row()], {"ep-stale"})
    assert plan.intent is QueryIntent.PREVIOUS_VALUE
    assert not plan.any_suppressed and plan.decisions[0].gate == "query_intent"


def test_bare_query_is_ambiguous_and_suppresses_nothing():
    plan = plan_view_suppression("How many coins do I own?", [_row()], {"ep-stale"})
    assert plan.intent is QueryIntent.AMBIGUOUS and not plan.any_suppressed


# --------------------------------------------------------------- planner: provenance gating
def test_row_not_in_candidate_set_is_ignored():
    plan = plan_view_suppression("What do I own now?", [_row(candidate_uuid="ep-absent")], {"ep-stale"})
    assert not plan.any_suppressed and plan.decisions == ()


def test_same_value_row_not_suppressed():
    plan = plan_view_suppression("What do I own now?", [_row(stale_value=37)], {"ep-stale"})
    assert not plan.any_suppressed and plan.decisions[0].gate == "overlap"


def test_ungrounded_view_not_suppressed():
    plan = plan_view_suppression("What do I own now?", [_row(evidence_tier=None)], {"ep-stale"})
    assert not plan.any_suppressed and plan.decisions[0].gate == "evidence"


def test_not_sole_current_not_suppressed():
    plan = plan_view_suppression("What do I own now?", [_row(is_sole_current=False)], {"ep-stale"})
    assert not plan.any_suppressed and plan.decisions[0].gate == "uniqueness"


def test_multiple_rows_only_eligible_suppressed():
    rows = [
        _row(candidate_uuid="ep-a", attribute="owned", stale_value=20),           # suppress
        _row(candidate_uuid="ep-b", attribute="wake_time", value_kind="clock_time",
             view_value="07:30", stale_value="07:30"),                            # same value -> keep
        _row(candidate_uuid="ep-c", attribute="day_off", value_kind="weekday",
             view_value="Wednesday", stale_value="Monday"),                       # suppress
    ]
    plan = plan_view_suppression("What is true now?", rows, {"ep-a", "ep-b", "ep-c"})
    assert plan.suppressed_uuids == frozenset({"ep-a", "ep-c"})
