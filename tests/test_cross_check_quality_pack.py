"""Phase 3 cross-check-quality pack v1 — deterministic tests for the precision-preserving SUM
arithmetic grounding (the "arithmetic is not a belief" cross-check adjustment).

Governing invariant: reduce FALSE abstentions on fold-SUM WITHOUT increasing wrong current-state
Views. The safety tests come first: a hallucinated price, an in-span double-count, or a mis-sum must
NOT ground (so they fall through to the unchanged holistic cross-check and never wrongly commit). Only
the clean case ('$50 and $75' -> 125, distinct source-price tokens) grounds and skips the noisy
holistic veto — while the veto-5 verifier still runs as the membership backstop.

All deterministic: fake LLM cross-check / verifier + fake graph, no live model or Neo4j.
"""

from __future__ import annotations

import pytest

from menhir.domain.fold_algebra import Event
from menhir.services.perception import (
    Episode,
    PerceivedGroup,
    _price_token_count,
    _sum_arithmetic_grounded,
    gate,
)


# ----------------------------------------------------------------------------- helpers


def _ev(value, uuid):
    return Event(when="2026-01-10", kind="purchase", value=float(value), episode_uuid=uuid)


def _eps(*pairs):
    return [Episode(uuid=u, content=c) for u, c in pairs]


def _sum_group(events, measure="bike_spend"):
    return PerceivedGroup(subject="user", measure=measure, reducer="sum", events=list(events))


def _always_disagree_crosscheck(measure):
    return 999.0  # holistic returns a wildly different total -> would veto if it ran


# ----------------------------------------------------------------------------- _price_token_count


@pytest.mark.unit
@pytest.mark.parametrize("span,amount,expected", [
    ("I bought a bike for $50 and another for $75.", 50, 1),
    ("I bought a bike for $50 and another for $75.", 75, 1),
    ("spent 50 dollars and 75 dollars", 50, 1),
    ("two bikes: $50.00 and $75.00", 50, 1),
    ("bought it for $150", 50, 0),          # 50 embedded in 150 -> NOT a standalone price
    ("the 2.50 coffee", 50, 0),             # 50 embedded in 2.50 -> not standalone
    ("it cost $50.5", 50, 0),               # 50.5 is not 50
    ("$40 lights and $40 lights", 40, 2),   # two distinct $40 mentions
    ("", 50, 0),
])
def test_price_token_count_is_boundary_guarded(span, amount, expected):
    assert _price_token_count(span, amount) == expected


# ----------------------------------------------------------------------------- grounding (SAFETY FIRST)


@pytest.mark.unit
def test_grounded_clean_two_prices_one_episode():
    eps = _eps(("e0", "I bought a bike for $50 and another for $75."))
    events = [_ev(50, "e0"), _ev(75, "e0")]
    assert _sum_arithmetic_grounded(125.0, events, eps) is True


@pytest.mark.unit
def test_grounded_two_prices_across_episodes():
    eps = _eps(("e0", "bike for $50"), ("e1", "another bike for $75"))
    events = [_ev(50, "e0"), _ev(75, "e1")]
    assert _sum_arithmetic_grounded(125.0, events, eps) is True


@pytest.mark.unit
def test_NOT_grounded_hallucinated_price_not_in_span():
    # extractor invented a $75 the source never mentions -> must NOT ground.
    eps = _eps(("e0", "I bought a bike for $50."))
    events = [_ev(50, "e0"), _ev(75, "e0")]
    assert _sum_arithmetic_grounded(125.0, events, eps) is False


@pytest.mark.unit
def test_NOT_grounded_in_span_double_count():
    # two $40 events but the span mentions $40 only once (double-count) -> must NOT ground.
    eps = _eps(("e0", "I bought $40 bike lights."))
    events = [_ev(40, "e0"), _ev(40, "e0")]
    assert _sum_arithmetic_grounded(80.0, events, eps) is False


@pytest.mark.unit
def test_NOT_grounded_when_arithmetic_mismatches_value():
    # spans have $50 and $75 but the candidate value is wrong -> must NOT ground.
    eps = _eps(("e0", "bike for $50 and $75"))
    events = [_ev(50, "e0"), _ev(75, "e0")]
    assert _sum_arithmetic_grounded(999.0, events, eps) is False


@pytest.mark.unit
def test_NOT_grounded_event_without_value_or_span():
    eps = _eps(("e0", "bike for $50"))
    assert _sum_arithmetic_grounded(50.0, [Event(when="x", kind="purchase", value=None,
                                                 episode_uuid="e0")], eps) is False
    # event whose episode_uuid isn't among the episodes -> no known span -> not grounded.
    assert _sum_arithmetic_grounded(50.0, [_ev(50, "unknown")], eps) is False
    assert _sum_arithmetic_grounded(50.0, [_ev(50, "e0")], None) is False


# ----------------------------------------------------------------------------- gate integration


@pytest.mark.unit
def test_gate_grounded_sum_skips_holistic_and_commits():
    """A grounded SUM skips the (disagreeing) holistic cross-check and commits; the decision records
    sum_grounded=True. Without grounding it would have vetoed on cross_check."""
    eps = _eps(("e0", "I bought a bike for $50 and another for $75."))
    samples = [[_sum_group([_ev(50, "e0"), _ev(75, "e0")])] for _ in range(3)]
    [d] = gate(samples, threshold=1.0, cross_check=_always_disagree_crosscheck,
               episodes=eps, enable_sum_grounding=True)
    assert d.committed is True and d.value == 125.0
    assert d.sum_grounded is True


@pytest.mark.unit
def test_gate_grounded_sum_still_subject_to_verifier():
    """Grounding skips the holistic veto but NOT the verifier (membership backstop): a verifier that
    rejects still abstains, so the wrong-write envelope is preserved."""
    eps = _eps(("e0", "I bought a bike for $50 and another for $75."))
    samples = [[_sum_group([_ev(50, "e0"), _ev(75, "e0")])] for _ in range(3)]
    [d] = gate(samples, threshold=1.0, cross_check=_always_disagree_crosscheck,
               verifier=lambda m, v, e: False, episodes=eps, enable_sum_grounding=True)
    assert d.committed is False and d.veto == "verification"


@pytest.mark.unit
def test_gate_ungrounded_sum_still_hits_holistic_veto():
    """A hallucinated-price SUM does NOT ground, so the holistic cross-check still vetoes it (behaviour
    unchanged for the unsafe case). Records the instrumentation (abstained_value + cross_margin)."""
    eps = _eps(("e0", "I bought a bike for $50."))  # no $75 in the span
    samples = [[_sum_group([_ev(50, "e0"), _ev(75, "e0")])] for _ in range(3)]
    [d] = gate(samples, threshold=1.0, cross_check=_always_disagree_crosscheck,
               episodes=eps, enable_sum_grounding=True)
    assert d.committed is False and d.veto == "cross_check"
    assert d.sum_grounded is False
    assert d.abstained_value == 125.0 and d.cross_margin == pytest.approx(874.0)


@pytest.mark.unit
def test_gate_sum_grounding_off_by_default_preserves_holistic_veto():
    """With the flag OFF (default), a grounded-looking SUM still hits the holistic veto — proving the
    change is opt-in and precision is unchanged until explicitly enabled."""
    eps = _eps(("e0", "I bought a bike for $50 and another for $75."))
    samples = [[_sum_group([_ev(50, "e0"), _ev(75, "e0")])] for _ in range(3)]
    [d] = gate(samples, threshold=1.0, cross_check=_always_disagree_crosscheck,
               episodes=eps, enable_sum_grounding=False)
    assert d.committed is False and d.veto == "cross_check"


@pytest.mark.unit
def test_gate_grounding_does_not_apply_to_count_reducer():
    """Grounding is SUM-only: a COUNT group never takes the deterministic path (its 'values' aren't
    prices)."""
    eps = _eps(("e0", "I bought bikes on three days"))
    # distinct days so count=3 (>= min_count) and it reaches the cross-check, rather than count-floor.
    g = PerceivedGroup(subject="user", measure="bikes_acquired", reducer="count",
                       events=[Event(when=f"2026-01-1{d}", kind="acquire", identity=f"bike{d}",
                                     episode_uuid="e0") for d in range(3)])
    samples = [[g] for _ in range(3)]
    [d] = gate(samples, threshold=1.0, cross_check=_always_disagree_crosscheck,
               episodes=eps, enable_sum_grounding=True)
    # count of 3 >= min_count, but the holistic cross-check disagrees (999) and count is NOT grounded,
    # so it still vetoes on cross_check (grounding never rescued it).
    assert d.committed is False and d.veto == "cross_check" and d.sum_grounded is False
