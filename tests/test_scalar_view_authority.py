"""Current-state-only View authority gate (Step 7 canary) -- pure, offline.

Each gate is exercised in isolation: the happy path SUPPRESSES, and flipping exactly one
precondition drops the outcome back to ADVISORY at the expected gate. History / previous /
comparison / ambiguous intents are ADVISORY even when every other precondition holds.
"""

from __future__ import annotations

import pytest

from menhir.domain.scalar_view_authority import (
    AuthorityOutcome,
    AuthorityQuery,
    OverlappingFact,
    QueryIntent,
    ViewCandidate,
    decide_view_authority,
)

VIEW_AT = "2026-07-20T15:00:00-05:00"
FACT_AT = "2026-07-01T00:00:00-05:00"   # strictly older than the view


def _query(**over):
    base = dict(intent=QueryIntent.CURRENT_STATE, subject_uuid="ent-1")
    base.update(over)
    return AuthorityQuery(**base)


def _view(**over):
    base = dict(
        subject_uuid="ent-1", attribute="owned", scope="", value_kind="count", unit="",
        value=37, evidence_tier="agent", view_current=True, is_sole_current=True,
        valid_at=VIEW_AT,
    )
    base.update(over)
    return ViewCandidate(**base)


def _fact(**over):
    base = dict(subject_uuid="ent-1", attribute="owned", scope="", value=20, valid_at=FACT_AT)
    base.update(over)
    return OverlappingFact(**base)


def test_happy_path_suppresses():
    d = decide_view_authority(_query(), _view(), _fact())
    assert d.outcome is AuthorityOutcome.SUPPRESS and d.gate == "pass" and d.suppress


# ------------------------------------------------------------------ gate 1: query intent
@pytest.mark.parametrize("intent", [
    QueryIntent.HISTORY, QueryIntent.PREVIOUS_VALUE, QueryIntent.COMPARISON, QueryIntent.AMBIGUOUS,
])
def test_non_current_intent_is_advisory(intent):
    d = decide_view_authority(_query(intent=intent), _view(), _fact())
    assert d.outcome is AuthorityOutcome.ADVISORY and d.gate == "query_intent" and not d.suppress


# ------------------------------------------------------------------ gate 2: subject
def test_unresolved_query_subject_is_advisory():
    d = decide_view_authority(_query(subject_uuid=None), _view(), _fact())
    assert d.gate == "subject" and not d.suppress


def test_subject_mismatch_view_vs_fact_is_advisory():
    d = decide_view_authority(_query(), _view(subject_uuid="ent-1"), _fact(subject_uuid="ent-2"))
    assert d.gate == "subject" and not d.suppress


def test_subject_mismatch_query_vs_view_is_advisory():
    d = decide_view_authority(_query(subject_uuid="ent-9"), _view(), _fact())
    assert d.gate == "subject" and not d.suppress


# ------------------------------------------------------------------ gate 3: slot
def test_different_attribute_is_advisory():
    d = decide_view_authority(_query(), _view(), _fact(attribute="needed"))
    assert d.gate == "slot" and not d.suppress


def test_different_scope_is_advisory():
    d = decide_view_authority(_query(), _view(scope="work"), _fact(scope="home"))
    assert d.gate == "slot" and not d.suppress


def test_incomplete_view_slot_is_advisory():
    d = decide_view_authority(_query(), _view(attribute=""), _fact(attribute=""))
    assert d.gate == "slot" and not d.suppress


# ------------------------------------------------------------------ gate 4: evidence
@pytest.mark.parametrize("tier", [None, "", "model", "guess"])
def test_ungrounded_tier_is_advisory(tier):
    d = decide_view_authority(_query(), _view(evidence_tier=tier), _fact())
    assert d.gate == "evidence" and not d.suppress


@pytest.mark.parametrize("tier", ["agent", "trusted_tool", "manual", "user"])
def test_all_grounded_tiers_suppress(tier):
    d = decide_view_authority(_query(), _view(evidence_tier=tier), _fact())
    assert d.outcome is AuthorityOutcome.SUPPRESS


# ------------------------------------------------------------------ gate 5: uniqueness
def test_not_current_view_is_advisory():
    d = decide_view_authority(_query(), _view(view_current=False), _fact())
    assert d.gate == "uniqueness" and not d.suppress


def test_not_sole_current_is_advisory():
    d = decide_view_authority(_query(), _view(is_sole_current=False), _fact())
    assert d.gate == "uniqueness" and not d.suppress


# ------------------------------------------------------------------ gate 6: overlap
def test_same_value_is_advisory():
    d = decide_view_authority(_query(), _view(value=37), _fact(value=37))
    assert d.gate == "overlap" and not d.suppress


def test_fact_missing_valid_at_is_advisory():
    d = decide_view_authority(_query(), _view(), _fact(valid_at=None))
    assert d.gate == "overlap" and not d.suppress


def test_view_missing_valid_at_is_advisory():
    d = decide_view_authority(_query(), _view(valid_at=None), _fact())
    assert d.gate == "overlap" and not d.suppress


def test_fact_newer_than_view_is_advisory():
    d = decide_view_authority(_query(), _view(valid_at=FACT_AT), _fact(valid_at=VIEW_AT))
    assert d.gate == "overlap" and not d.suppress


def test_fact_equal_time_is_advisory():
    d = decide_view_authority(_query(), _view(valid_at=VIEW_AT), _fact(valid_at=VIEW_AT))
    assert d.gate == "overlap" and not d.suppress


def test_value_compared_by_normalized_string():
    # 37 (int) vs "37" (str) are the same value -> nothing to suppress.
    d = decide_view_authority(_query(), _view(value=37), _fact(value="37"))
    assert d.gate == "overlap" and not d.suppress


def test_neo4j_zoned_timestamp_form_is_parsed():
    # Neo4j toString(valid_at) yields "...Z[UTC]"; the gate must parse it (else gate 6 fails wrongly).
    d = decide_view_authority(
        _query(),
        _view(valid_at="2026-07-21T14:31:30.331Z[UTC]"),
        _fact(valid_at="2026-07-21T14:31:26.850Z[UTC]"),
    )
    assert d.outcome is AuthorityOutcome.SUPPRESS and d.gate == "pass"
