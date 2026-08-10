"""When-discipline temporal matrix (deterministic resolver + as-of fold) -- offline.

Target: no temporal value may affect LWW ordering unless its timing is source-grounded and
deterministically resolved. Fixed anchors (owner spec):
    episode_reference = 2026-07-20T15:00:00-05:00
    as_of_before      = 2026-07-20T15:00:00-05:00
    as_of_future      = 2026-07-27T00:00:00-05:00
Each row measures the pieces SEPARATELY: temporal disposition (drop vs keep + trusted date), and the
fold result before/after an evaluation time.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from menhir.domain.scalar_state_fold import AMBIGUOUS_ANCHOR, fold_assertions
from menhir.services.typed_scalar_perception import resolve_temporal_disposition

EPISODE_REF = "2026-07-20T15:00:00-05:00"
AS_OF_BEFORE = datetime.fromisoformat("2026-07-20T15:00:00-05:00")
AS_OF_FUTURE = datetime.fromisoformat("2026-07-27T00:00:00-05:00")


# --------------------------------------------------------------------- disposition matrix

#: a fully-explicit source date resolves to this instant (episode-reference tz).
JULY_18 = "2026-07-18T00:00:00-05:00"


@pytest.mark.parametrize("span,model_when,op,disposition,keep,trusted", [
    ("I own 37 coins.", None, "absolute", "current_observation", True, None),
    # FULLY-EXPLICIT date -> persist the SOURCE-derived timestamp (not the model's).
    ("As of July 18, 2026 I own 37.", "2026-07-18", "absolute", "explicit_time", True, JULY_18),
    ("As of 2026-07-18 I own 37.", None, "absolute", "explicit_time", True, JULY_18),   # source date, no model
    # month/day WITHOUT a year -> a specific claim we refuse to year-infer -> abstain (P1 correction).
    ("As of July 18 I own 37.", "2026-07-18", "absolute", "unresolved_date", False, None),
    ("Yesterday I had 20 coins.", None, "absolute", "past_only", False, None),
    ("now I have 37 coins", None, "absolute", "current_observation", True, None),
    ("I used to own 20 coins.", None, "expire", "current_observation", True, None),   # dateless expire -> ep ref
    ("I recently moved to Austin.", None, "absolute", "current_observation", True, None),
    ("A while ago I lived in Dallas.", None, "absolute", "past_only", False, None),
    ("Starting next Monday I wake at 6:30.", None, "absolute", "future_unresolved", False, None),
    ("Starting July 27, 2026 I wake at 6:30.", "2026-07-27", "absolute", "explicit_time", True,
     "2026-07-27T00:00:00-05:00"),
    ("I own 37 coins.", "2026-01-01", "absolute", "unsupported_date", True, None),   # hallucinated date dropped
    ("I wake at 7:30 on Monday.", "2026-07-20", "absolute", "unsupported_date", True, None),  # weekday != date
    ("For this week I wake at 6:30.", None, "absolute", "bounded_unsupported", False, None),
    ("I'm in Dallas until Friday.", None, "absolute", "bounded_unsupported", False, None),
])
def test_temporal_disposition(span, model_when, op, disposition, keep, trusted):
    disp, k, tw = resolve_temporal_disposition(span, model_when, op, EPISODE_REF)
    assert (disp, k, tw) == (disposition, keep, trusted)


# --------------------------------------------------------------------- P1: explicit-timestamp integrity

def test_r1_explicit_iso_matching_model_persists_source_date():
    disp, keep, tw = resolve_temporal_disposition("As of 2026-07-18 I own 37.", "2026-07-18",
                                                  "absolute", EPISODE_REF)
    assert (disp, keep) == ("explicit_time", True) and tw == JULY_18


def test_r2_explicit_iso_conflicting_model_drops():
    disp, keep, tw = resolve_temporal_disposition("As of 2026-07-18 I own 37.", "2026-07-19",
                                                  "absolute", EPISODE_REF)
    assert (disp, keep, tw) == ("date_conflict", False, None)


def test_r3_explicit_source_missing_model_persists_source_date():
    disp, keep, tw = resolve_temporal_disposition("As of July 18, 2026 I own 37.", None,
                                                  "absolute", EPISODE_REF)
    assert (disp, keep) == ("explicit_time", True) and tw == JULY_18


def test_r4_month_day_without_year_abstains():
    disp, keep, tw = resolve_temporal_disposition("As of July 18 I own 37.", "2026-07-18",
                                                  "absolute", EPISODE_REF)
    assert (disp, keep, tw) == ("unresolved_date", False, None)


def test_r5_weekday_never_authorizes_a_model_date():
    disp, keep, tw = resolve_temporal_disposition("I wake at 7:30 on Monday.", "2026-07-20",
                                                  "absolute", EPISODE_REF)
    assert keep is True and tw is None   # model date IGNORED -> episode reference, never trusted


def test_r6_dateless_expire_ignores_model_date():
    disp, keep, tw = resolve_temporal_disposition("I used to own 20 coins.", "2020-01-01",
                                                  "expire", EPISODE_REF)
    assert (disp, keep, tw) == ("unsupported_date", True, None)   # -> bind uses episode reference


def test_r7_explicit_date_expire_uses_source_date():
    disp, keep, tw = resolve_temporal_disposition("I stopped owning them on July 18, 2026.",
                                                  "2026-07-18", "expire", EPISODE_REF)
    assert (disp, keep) == ("explicit_time", True) and tw == JULY_18


def test_r8_conflicting_explicit_date_expire_drops():
    disp, keep, tw = resolve_temporal_disposition("I stopped owning them on July 18, 2026.",
                                                  "2026-07-19", "expire", EPISODE_REF)
    assert (disp, keep, tw) == ("date_conflict", False, None)


def test_no_unsupported_model_date_survives():
    # acceptance: a model date the span does not anchor is neutralized (never reaches valid_at).
    _disp, keep, trusted = resolve_temporal_disposition("I weigh 180 kg.", "2020-01-01", "absolute")
    assert keep is True and trusted is None   # value kept, date dropped


def test_dateless_present_uses_no_trusted_date():
    _disp, keep, trusted = resolve_temporal_disposition("I have 37 coins.", None, "absolute")
    assert keep is True and trusted is None   # -> bind assigns episode reference


# --------------------------------------------------------------------- past/current no longer collide

def _row(**over):
    base = dict(assertion_id="a", subject_uuid="ent", subject_display="user", attribute="owned",
                scope="", value_kind="count", unit="", operation="absolute", value=37,
                valid_at=EPISODE_REF, learned_at=EPISODE_REF, evidence_tier="agent",
                episode_uuid="ep-1", binding_pending=False)
    base.update(over)
    return base


def test_past_only_never_reaches_the_fold_so_no_ambiguous_collision():
    # The historical/current collision that produced AMBIGUOUS_ANCHOR is prevented UPSTREAM: the past-only
    # claim is dropped at extraction, so only the current absolute reaches the fold. (Here we assert the
    # resolver drops it; the fold with just the current row yields the current value cleanly.)
    disp, keep, _ = resolve_temporal_disposition("Yesterday I had 20 coins.", None, "absolute")
    assert disp == "past_only" and keep is False
    folded = fold_assertions([_row(assertion_id="cur", value=37, valid_at=EPISODE_REF)])
    assert folded.states[0].value == 37 and not folded.abstentions


def test_two_current_absolutes_same_time_distinct_still_ambiguous():
    # control: when-discipline does NOT change the genuine equal-time distinct-value ambiguity.
    r = fold_assertions([
        _row(assertion_id="a1", value=37, valid_at=EPISODE_REF),
        _row(assertion_id="a2", value=38, valid_at=EPISODE_REF),
    ])
    assert not r.states and r.abstentions[0].reason == AMBIGUOUS_ANCHOR


# --------------------------------------------------------------------- future activation (as-of fold)

def test_future_absolute_inactive_before_then_active_after():
    rows = [
        _row(assertion_id="cur", attribute="wake_time", value_kind="clock_time", value="07:30",
             valid_at="2026-07-20T00:00:00-05:00"),
        _row(assertion_id="fut", attribute="wake_time", value_kind="clock_time", value="06:30",
             valid_at="2026-07-27T00:00:00-05:00"),
    ]
    before = fold_assertions(rows, as_of=AS_OF_BEFORE)
    after = fold_assertions(rows, as_of=AS_OF_FUTURE)
    assert before.states[0].value == "07:30"   # future not yet active
    assert after.states[0].value == "06:30"     # activates at its valid_at


def test_future_expire_does_not_retire_view_early():
    rows = [
        _row(assertion_id="cur", attribute="wake_time", value_kind="clock_time", value="07:30",
             valid_at="2026-07-20T00:00:00-05:00"),
        _row(assertion_id="e", attribute="wake_time", value_kind="clock_time", operation="expire",
             value="07:30", valid_at="2026-07-27T00:00:00-05:00"),
    ]
    before = fold_assertions(rows, as_of=AS_OF_BEFORE)
    after = fold_assertions(rows, as_of=AS_OF_FUTURE)
    assert before.states and before.states[0].value == "07:30" and not before.expiries
    assert not after.states and len(after.expiries) == 1
