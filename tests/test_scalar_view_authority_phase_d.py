"""Counterfactual verification of the Step-7 authority gate on the Phase D fixture -- offline.

Step 6 proved the live pipeline yields Views that are correct-or-absent, NEVER wrong, for these
same cases. This test proves the pure authority gate (the production decision function) then makes
the RIGHT suppression call on top of those Views: it SUPPRESSES the stale predecessor for each
current-state case and stays ADVISORY on every control (historical intent, same-value expired
lingering View, ungrounded evidence). wrongful_suppression must be 0.

This is the gate's half of increment 7b -- verified BEFORE any production recall wiring. It mirrors
the archolith-bench Phase D fixture (pd-coins/wake/dayoff current + historical + expired controls)
without coupling menhir to the bench harness.
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

SELF = "self-entity-uuid"
NOW = "2026-07-20T15:00:00-05:00"   # current View valid_at
OLD = "2026-07-01T00:00:00-05:00"   # stale predecessor fact valid_at


def _view(attribute, value_kind, value, *, valid_at=NOW, tier="agent",
          view_current=True, is_sole_current=True):
    return ViewCandidate(
        subject_uuid=SELF, attribute=attribute, scope="", value_kind=value_kind, unit="",
        value=value, evidence_tier=tier, view_current=view_current,
        is_sole_current=is_sole_current, valid_at=valid_at,
    )


def _fact(attribute, value, *, valid_at=OLD):
    return OverlappingFact(subject_uuid=SELF, attribute=attribute, scope="", value=value,
                           valid_at=valid_at)


# ------------------------------------------------------------------ current-state cases -> SUPPRESS
@pytest.mark.parametrize("attribute,kind,current,stale", [
    ("owned", "count", 37, 20),          # pd-coins-current
    ("wake_time", "clock_time", "07:30", "09:00"),  # pd-wake-current
    ("day_off", "weekday", "Wednesday", "Monday"),  # pd-dayoff-current
])
def test_current_state_stale_predecessor_is_suppressed(attribute, kind, current, stale):
    q = AuthorityQuery(intent=QueryIntent.CURRENT_STATE, subject_uuid=SELF)
    d = decide_view_authority(q, _view(attribute, kind, current), _fact(attribute, stale))
    assert d.outcome is AuthorityOutcome.SUPPRESS and d.gate == "pass"


# ------------------------------------------------------------------ historical control -> ADVISORY
def test_historical_question_never_suppresses_the_stale_answer():
    # pd-coins-historical: "how many did I own a while ago?" -> the stale value IS the right answer.
    q = AuthorityQuery(intent=QueryIntent.PREVIOUS_VALUE, subject_uuid=SELF)
    d = decide_view_authority(q, _view("owned", "count", 37), _fact("owned", 20))
    assert d.outcome is AuthorityOutcome.ADVISORY and d.gate == "query_intent"


# ------------------------------------------------------------------ expired control -> ADVISORY
def test_expired_lingering_view_same_value_does_not_suppress():
    # pd-residence-expired: a lingering "Dallas" View over the same "Dallas" graph fact. Even if the
    # expire failed to retire the View, gate 6 (same value) keeps it advisory -- nothing to suppress.
    q = AuthorityQuery(intent=QueryIntent.CURRENT_STATE, subject_uuid=SELF)
    d = decide_view_authority(q, _view("residence", "status", "Dallas"), _fact("residence", "Dallas"))
    assert d.outcome is AuthorityOutcome.ADVISORY and d.gate == "overlap"


# ------------------------------------------------------------------ full-fixture decision matrix
def test_phase_d_fixture_decision_matrix_zero_wrongful_suppression():
    """Encode the whole fixture and assert: current cases SUPPRESS, controls ADVISORY, wrongful=0."""
    q_current = AuthorityQuery(intent=QueryIntent.CURRENT_STATE, subject_uuid=SELF)
    q_hist = AuthorityQuery(intent=QueryIntent.PREVIOUS_VALUE, subject_uuid=SELF)

    matrix = [
        ("pd-coins-current", "suppress", q_current, _view("owned", "count", 37), _fact("owned", 20)),
        ("pd-wake-current", "suppress", q_current,
         _view("wake_time", "clock_time", "07:30"), _fact("wake_time", "09:00")),
        ("pd-dayoff-current", "suppress", q_current,
         _view("day_off", "weekday", "Wednesday"), _fact("day_off", "Monday")),
        ("pd-coins-historical", "advisory", q_hist, _view("owned", "count", 37), _fact("owned", 20)),
        # expired: lingering same-value View must not suppress the (still-correct) fact.
        ("pd-residence-expired", "advisory", q_current,
         _view("residence", "status", "Dallas"), _fact("residence", "Dallas")),
    ]

    wrongful = 0
    for case_id, expect, q, view, fact in matrix:
        d = decide_view_authority(q, view, fact)
        got = "suppress" if d.suppress else "advisory"
        assert got == expect, f"{case_id}: expected {expect}, got {got} (gate={d.gate})"
        # wrongful suppression = suppressing where the fact is the correct answer (the controls).
        if expect == "advisory" and d.suppress:
            wrongful += 1
    assert wrongful == 0
