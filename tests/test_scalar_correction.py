"""Correction semantics (Phase A) -- deterministic span-local classifier + identity invariants.

The load-bearing contract: `correction` is a PURE function of the grounded stated_span (code, not the
LLM), so all k samples of one claim classify identically, and it stays OUT of assertion identity. These
tests pin that contract; the equal-time fold tiebreak lives in test_scalar_state_fold.py, and the
monotone ordinary->correction persistence is a live-Neo4j behavior asserted by the query shape here.
"""

from __future__ import annotations

import pytest

from menhir.domain.typed_assertion import ABSOLUTE_SEMANTICS, TypedAssertion
from menhir.infrastructure.typed_assertion_repository import _RECORD_CYPHER
from menhir.services.typed_scalar_perception import classify_absolute_semantics


# --------------------------------------------------------------------- classifier

@pytest.mark.unit
@pytest.mark.parametrize("span", [
    "Actually, I weigh 182", "actually i weigh 182", "correction: it is Thursday",
    "I meant 182", "I was wrong, it is 182", "not 180, 182", "I misspoke, it is 182",
])
def test_correction_cues_classify_as_correction(span):
    assert classify_absolute_semantics(span, "absolute") == "correction"


@pytest.mark.unit
@pytest.mark.parametrize("span", [
    "I weigh 182 pounds", "I have 37 coins", "I own another 5 coins",   # 'another' has no false 'not'
    "I cannot recall exactly", "My car is red", "I wake at 7:30",       # 'cannot' has no false 'not'
])
def test_ordinary_spans_classify_as_ordinary(span):
    assert classify_absolute_semantics(span, "absolute") == "ordinary"


@pytest.mark.unit
def test_classifier_is_deterministic_same_span_same_result():
    # acceptance 1: the same grounded span always receives the same classification.
    span = "Actually, I weigh 182"
    assert classify_absolute_semantics(span, "absolute") == classify_absolute_semantics(span, "absolute")


@pytest.mark.unit
def test_classifier_is_span_local_not_episode_wide():
    # acceptance 9: a correction cue OUTSIDE the grounded claim span is not seen (the classifier only
    # gets the span). A claim grounded on "I weigh 182" is ordinary even if the episode elsewhere said
    # "actually" about something else -- because that word is not in THIS claim's span.
    assert classify_absolute_semantics("I weigh 182", "absolute") == "ordinary"


@pytest.mark.unit
@pytest.mark.parametrize("op", ["delta", "expire"])
def test_only_absolute_can_be_a_correction(op):
    # correction framing is meaningful only for a stated absolute value.
    assert classify_absolute_semantics("Actually, +2", op) == "ordinary"


# --------------------------------------------------------------------- identity (must NOT fork)

def _assertion(semantics):
    return TypedAssertion(
        subject_uuid="ent-1", subject_display="user", attribute="weight", scope="",
        value_kind="measurement", unit="kg", operation="absolute", value=182,
        stated_span="Actually, I weigh 182", episode_uuid="ep-1", valid_at="2026-07-20T00:00:00+00:00",
        learned_at="2026-07-20T00:00:00+00:00", span_start=0, span_end=21,
        absolute_semantics=semantics,
    )


@pytest.mark.unit
def test_absolute_semantics_is_not_in_any_identity_key():
    # acceptance 2: ordinary vs correction (all else equal) must produce IDENTICAL source_key, claim_key,
    # and assertion_key -- so reprocessing never forks assertion identity on the framing modifier.
    ordinary, correction = _assertion("ordinary"), _assertion("correction")
    assert ordinary.source_key == correction.source_key
    assert ordinary.claim_key == correction.claim_key
    assert ordinary.assertion_key == correction.assertion_key


@pytest.mark.unit
def test_absolute_semantics_validated():
    assert ABSOLUTE_SEMANTICS == {"ordinary", "correction"}
    with pytest.raises(ValueError):
        _assertion("confirmation")   # dropped from Phase A -> rejected


@pytest.mark.unit
def test_record_cypher_has_monotone_correction_upgrade():
    # acceptance 3 (behavioral proof is live): the write path sets the initial value and UPGRADES
    # ordinary->correction on a re-write, mismatch-gated, never downgrading.
    assert "a.absolute_semantics = $absolute_semantics" in _RECORD_CYPHER
    assert "SET a.absolute_semantics = 'correction'" in _RECORD_CYPHER
    assert "$absolute_semantics = 'correction'" in _RECORD_CYPHER
    assert "NOT binding_mismatch" in _RECORD_CYPHER
