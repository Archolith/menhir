"""Deterministic scalar-state fold + rebuild (ScalarStateView Piece C, commit 2).

Pure/offline. Proves invariant 2: durable assertions deterministically produce the correct View.
Covers latest-anchor selection, post-anchor delta application, ambiguous/no-anchor/delta-on-range
abstention, weakest-contributor authority tier, exact contributor ids, determinism, and
live-fold-vs-rebuild parity (same pure fold backs both paths)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from menhir.domain.scalar_state_fold import (
    AMBIGUOUS_ANCHOR,
    DELTA_ON_RANGE,
    NO_ANCHOR,
    fold_assertions,
)
from menhir.services.scalar_state_service import ScalarStateService


def _row(**over):
    base = dict(
        assertion_id="a?", subject_uuid="ent-coins", subject_display="my coins",
        attribute="owned", scope="pre-1920", value_kind="count", unit="", operation="absolute",
        value=37, valid_at="2026-07-01T00:00:00+00:00", learned_at="2026-07-01T00:00:00+00:00",
        evidence_tier="user", episode_uuid="ep-1", binding_pending=False,
    )
    base.update(over)
    return base


def _one(rows):
    r = fold_assertions(rows)
    assert len(r.states) == 1 and not r.abstentions, (r.states, r.abstentions)
    return r.states[0]


# --------------------------------------------------------------------- latest absolute anchor

@pytest.mark.unit
def test_latest_absolute_anchor_wins():
    s = _one([
        _row(assertion_id="a1", value=37, valid_at="2026-07-01T00:00:00+00:00"),
        _row(assertion_id="a2", value=40, valid_at="2026-08-01T00:00:00+00:00"),
    ])
    assert s.value == 40 and s.contributor_ids == ["a2"]
    assert s.anchor_value == 40 and s.delta_total == 0.0


@pytest.mark.unit
def test_neo4j_zoned_valid_at_orders_absolutes_not_epoch_tie():
    # Regression: valid_at read back via Neo4j toString() carries a "...Z[UTC]" zone suffix that
    # fromisoformat rejects. If _parse_dt drops it to None, every absolute collapses to the _EPOCH
    # fallback and two distinct values look tied at the max -> spurious AMBIGUOUS_ANCHOR that retires
    # the slot's View (cross-episode replacement silently broken). The later value must win instead.
    s = _one([
        _row(assertion_id="old", value=20, valid_at="2026-07-21T19:10:51.478Z[UTC]"),
        _row(assertion_id="new", value=37, valid_at="2026-07-21T19:13:04.572Z[UTC]"),
    ])
    assert s.value == 37 and s.contributor_ids == ["new"]


@pytest.mark.unit
def test_single_absolute_non_numeric_kind():
    s = _one([_row(assertion_id="c1", attribute="wake", scope="", value_kind="clock_time",
                   value="07:30")])
    assert s.value == "07:30" and s.contributor_ids == ["c1"]


# --------------------------------------------------------------------- delta fold

@pytest.mark.unit
def test_delta_after_anchor_is_applied():
    s = _one([
        _row(assertion_id="anc", value=37, valid_at="2026-07-01T00:00:00+00:00"),
        _row(assertion_id="d1", operation="delta", value=1, valid_at="2026-07-05T00:00:00+00:00"),
    ])
    assert s.value == 38 and s.delta_total == 1.0
    assert s.contributor_ids == ["anc", "d1"]  # anchor + applied delta


@pytest.mark.unit
def test_delta_before_anchor_is_ignored():
    # a stated absolute subsumes any earlier delta.
    s = _one([
        _row(assertion_id="d0", operation="delta", value=5, valid_at="2026-06-01T00:00:00+00:00"),
        _row(assertion_id="anc", value=40, valid_at="2026-07-01T00:00:00+00:00"),
    ])
    assert s.value == 40 and s.contributor_ids == ["anc"]


@pytest.mark.unit
def test_multiple_deltas_sum_and_can_decrement():
    s = _one([
        _row(assertion_id="anc", value=10, valid_at="2026-07-01T00:00:00+00:00"),
        _row(assertion_id="d1", operation="delta", value=3, valid_at="2026-07-02T00:00:00+00:00"),
        _row(assertion_id="d2", operation="delta", value=-1, valid_at="2026-07-03T00:00:00+00:00"),
    ])
    assert s.value == 12 and s.delta_total == 2.0
    assert s.contributor_ids == ["anc", "d1", "d2"]


@pytest.mark.unit
def test_money_deltas_fold_with_decimal_exactness():
    s = _one([
        _row(
            assertion_id="anc", value_kind="money", unit="usd", value=Decimal("0.10"),
            valid_at="2026-07-01T00:00:00+00:00",
        ),
        _row(
            assertion_id="d1", value_kind="money", unit="usd", operation="delta",
            value=Decimal("0.20"), valid_at="2026-07-02T00:00:00+00:00",
        ),
    ])

    assert s.value == Decimal("0.30")
    assert isinstance(s.value, Decimal)


# --------------------------------------------------------------------- abstention

@pytest.mark.unit
def test_no_anchor_abstains():
    r = fold_assertions([_row(assertion_id="d", operation="delta", value=1)])
    assert not r.states and r.abstentions[0].reason == NO_ANCHOR


@pytest.mark.unit
def test_ambiguous_anchor_abstains():
    # two DISTINCT absolutes at the SAME latest valid_at -> unbreakable tie.
    r = fold_assertions([
        _row(assertion_id="a1", value=37, valid_at="2026-08-01T00:00:00+00:00"),
        _row(assertion_id="a2", value=38, valid_at="2026-08-01T00:00:00+00:00"),
    ])
    assert not r.states and r.abstentions[0].reason == AMBIGUOUS_ANCHOR


@pytest.mark.unit
def test_same_value_tie_is_not_ambiguous():
    # two absolutes, same latest valid_at, SAME value -> not ambiguous; pick deterministically.
    s = _one([
        _row(assertion_id="a1", value=38, valid_at="2026-08-01T00:00:00+00:00",
             learned_at="2026-08-01T00:00:00+00:00"),
        _row(assertion_id="a2", value=38, valid_at="2026-08-01T00:00:00+00:00",
             learned_at="2026-08-02T00:00:00+00:00"),
    ])
    assert s.value == 38 and s.contributor_ids == ["a2"]  # last by learned_at


# --------------------------------------------------------------------- expiry ("used to X")

@pytest.mark.unit
def test_lone_expire_yields_expiry_not_state():
    # "I used to wake up at 9:00" alone -> the value ENDED with no replacement: Expiry, no View.
    r = fold_assertions([_row(
        assertion_id="e", attribute="wake_time", scope="", value_kind="clock_time", unit="",
        operation="expire", value="09:00", valid_at="2026-07-01T00:00:00+00:00")])
    assert not r.states and not r.abstentions
    assert len(r.expiries) == 1
    assert r.expiries[0].expired_value == "09:00"
    assert r.expiries[0].slot_key[1] == "wake_time"


@pytest.mark.unit
def test_expire_of_old_value_does_not_suppress_a_co_present_current_absolute():
    # "used to wake at 9:00" (expire) + "wake at 7:30" (absolute), SAME valid_at: the current absolute
    # wins -- an expire of the OLD value never suppresses a co-present current value. This is the case
    # that previously nondeterministically surfaced the stale 09:00.
    s = _one([
        _row(assertion_id="e", attribute="wake_time", scope="", value_kind="clock_time", unit="",
             operation="expire", value="09:00", valid_at="2026-07-01T00:00:00+00:00"),
        _row(assertion_id="a", attribute="wake_time", scope="", value_kind="clock_time", unit="",
             operation="absolute", value="07:30", valid_at="2026-07-01T00:00:00+00:00"),
    ])
    assert s.value == "07:30" and s.contributor_ids == ["a"]


@pytest.mark.unit
def test_later_absolute_supersedes_an_earlier_expire():
    # "used to live in Dallas" (expire) then "now Austin" (later absolute) -> Austin View.
    s = _one([
        _row(assertion_id="e", attribute="residence", scope="", value_kind="status", unit="",
             operation="expire", value="Dallas", valid_at="2026-06-01T00:00:00+00:00"),
        _row(assertion_id="a", attribute="residence", scope="", value_kind="status", unit="",
             operation="absolute", value="Austin", valid_at="2026-07-01T00:00:00+00:00"),
    ])
    assert s.value == "Austin"


@pytest.mark.unit
def test_expire_strictly_after_latest_absolute_expires_the_slot():
    # a value was set, then later explicitly ended ("I owned 37; I no longer collect coins").
    r = fold_assertions([
        _row(assertion_id="a", attribute="owned", scope="", value_kind="count", unit="",
             operation="absolute", value=37, valid_at="2026-06-01T00:00:00+00:00"),
        _row(assertion_id="e", attribute="owned", scope="", value_kind="count", unit="",
             operation="expire", value=37, valid_at="2026-07-01T00:00:00+00:00"),
    ])
    assert not r.states and len(r.expiries) == 1


@pytest.mark.unit
def test_expire_at_or_before_latest_absolute_is_subsumed():
    # an old expire cannot end a value a NEWER absolute already replaced.
    s = _one([
        _row(assertion_id="e", attribute="owned", scope="", value_kind="count", unit="",
             operation="expire", value=20, valid_at="2026-05-01T00:00:00+00:00"),
        _row(assertion_id="a", attribute="owned", scope="", value_kind="count", unit="",
             operation="absolute", value=37, valid_at="2026-06-01T00:00:00+00:00"),
    ])
    assert s.value == 37


@pytest.mark.unit
def test_expiry_fold_is_deterministic_regardless_of_input_order():
    rows = [
        _row(assertion_id="e", attribute="wake_time", scope="", value_kind="clock_time", unit="",
             operation="expire", value="09:00", valid_at="2026-07-01T00:00:00+00:00"),
        _row(assertion_id="a", attribute="wake_time", scope="", value_kind="clock_time", unit="",
             operation="absolute", value="07:30", valid_at="2026-07-01T00:00:00+00:00"),
    ]
    a = fold_assertions(rows)
    b = fold_assertions(list(reversed(rows)))
    assert [s.value for s in a.states] == [s.value for s in b.states] == ["07:30"]
    assert not a.expiries and not b.expiries


# --------------------------------------------------------------------- as-of activation (Phase B)

from datetime import datetime, timezone  # noqa: E402


def _dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@pytest.mark.unit
def test_future_absolute_does_not_affect_fold_before_its_valid_at():
    # acceptance 17: a scheduled value is not current before it activates.
    rows = [
        _row(assertion_id="cur", attribute="wake_time", value_kind="clock_time", unit="", value="07:30",
             valid_at="2026-07-20T00:00:00+00:00"),
        _row(assertion_id="fut", attribute="wake_time", value_kind="clock_time", unit="", value="06:30",
             valid_at="2026-07-27T00:00:00+00:00"),
    ]
    s = fold_assertions(rows, as_of=_dt("2026-07-25T00:00:00+00:00")).states[0]
    assert s.value == "07:30"


@pytest.mark.unit
def test_future_absolute_becomes_eligible_at_its_valid_at():
    # acceptance 18/21: the same log folded at/after activation yields the scheduled value.
    rows = [
        _row(assertion_id="cur", attribute="wake_time", value_kind="clock_time", unit="", value="07:30",
             valid_at="2026-07-20T00:00:00+00:00"),
        _row(assertion_id="fut", attribute="wake_time", value_kind="clock_time", unit="", value="06:30",
             valid_at="2026-07-27T00:00:00+00:00"),
    ]
    s = fold_assertions(rows, as_of=_dt("2026-07-27T00:00:00+00:00")).states[0]
    assert s.value == "06:30"


@pytest.mark.unit
def test_future_delta_does_not_apply_before_its_valid_at():
    # acceptance 19: a future delta must not move the current numeric value early.
    rows = [
        _row(assertion_id="anc", attribute="owned", value_kind="count", unit="", value=37,
             valid_at="2026-07-20T00:00:00+00:00"),
        _row(assertion_id="d", attribute="owned", value_kind="count", unit="", operation="delta", value=5,
             valid_at="2026-07-27T00:00:00+00:00"),
    ]
    at_25 = fold_assertions(rows, as_of=_dt("2026-07-25T00:00:00+00:00")).states[0]
    at_27 = fold_assertions(rows, as_of=_dt("2026-07-27T00:00:00+00:00")).states[0]
    assert at_25.value == 37 and at_27.value == 42


@pytest.mark.unit
def test_future_expire_does_not_remove_view_early():
    # acceptance 20: a future expiry must not retire the current value before it takes effect.
    rows = [
        _row(assertion_id="anc", attribute="wake_time", value_kind="clock_time", unit="", value="07:30",
             valid_at="2026-07-20T00:00:00+00:00"),
        _row(assertion_id="e", attribute="wake_time", value_kind="clock_time", unit="", operation="expire",
             value="07:30", valid_at="2026-07-27T00:00:00+00:00"),
    ]
    before = fold_assertions(rows, as_of=_dt("2026-07-25T00:00:00+00:00"))
    after = fold_assertions(rows, as_of=_dt("2026-07-27T00:00:00+00:00"))
    assert before.states and before.states[0].value == "07:30" and not before.expiries
    assert not after.states and len(after.expiries) == 1   # expiry activates -> no View


@pytest.mark.unit
def test_as_of_fold_is_pure_given_rows_and_as_of():
    # parity: same rows + same as_of -> identical result (live == rebuild). Order-independent too.
    rows = [
        _row(assertion_id="cur", attribute="owned", value_kind="count", unit="", value=37,
             valid_at="2026-07-20T00:00:00+00:00"),
        _row(assertion_id="fut", attribute="owned", value_kind="count", unit="", value=50,
             valid_at="2026-07-27T00:00:00+00:00"),
    ]
    as_of = _dt("2026-07-22T00:00:00+00:00")
    a = fold_assertions(rows, as_of=as_of).states[0].value
    b = fold_assertions(list(reversed(rows)), as_of=as_of).states[0].value
    assert a == b == 37


# --------------------------------------------------------------------- correction (equal-time tiebreak)

@pytest.mark.unit
def test_correction_wins_equal_time_conflict_over_ordinary():
    # acceptance 5/10: ordinary 180 + correction 182 at the SAME valid_at -> 182.
    s = _one([
        _row(assertion_id="a", attribute="weight", value_kind="measurement", unit="kg", value=180,
             valid_at="2026-07-20T00:00:00+00:00", absolute_semantics="ordinary"),
        _row(assertion_id="b", attribute="weight", value_kind="measurement", unit="kg", value=182,
             valid_at="2026-07-20T00:00:00+00:00", absolute_semantics="correction"),
    ])
    assert s.value == 182 and s.contributor_ids == ["b"]


@pytest.mark.unit
def test_two_corrections_same_value_are_one_group_and_win():
    # acceptance 4: two correction rows agreeing on 182 = ONE corrected value group (agreement).
    s = _one([
        _row(assertion_id="a", attribute="weight", value_kind="measurement", unit="kg", value=180,
             valid_at="2026-07-20T00:00:00+00:00", absolute_semantics="ordinary"),
        _row(assertion_id="b", attribute="weight", value_kind="measurement", unit="kg", value=182,
             valid_at="2026-07-20T00:00:00+00:00", learned_at="2026-07-20T01:00:00+00:00",
             absolute_semantics="correction"),
        _row(assertion_id="c", attribute="weight", value_kind="measurement", unit="kg", value=182,
             valid_at="2026-07-20T00:00:00+00:00", learned_at="2026-07-20T02:00:00+00:00",
             absolute_semantics="correction"),
    ])
    assert s.value == 182


@pytest.mark.unit
def test_two_distinct_corrected_values_remain_ambiguous():
    # acceptance 6/11: two DIFFERENT corrected value groups at equal time -> AMBIGUOUS_ANCHOR.
    r = fold_assertions([
        _row(assertion_id="a", attribute="weight", value_kind="measurement", unit="kg", value=182,
             valid_at="2026-07-20T00:00:00+00:00", absolute_semantics="correction"),
        _row(assertion_id="b", attribute="weight", value_kind="measurement", unit="kg", value=185,
             valid_at="2026-07-20T00:00:00+00:00", absolute_semantics="correction"),
    ])
    assert not r.states and r.abstentions[0].reason == AMBIGUOUS_ANCHOR


@pytest.mark.unit
def test_correction_never_overrides_a_later_ordinary():
    # acceptance 7/12: an OLDER correction must not beat a genuinely LATER ordinary value.
    s = _one([
        _row(assertion_id="corr", attribute="weight", value_kind="measurement", unit="kg", value=182,
             valid_at="2026-07-20T00:00:00+00:00", absolute_semantics="correction"),
        _row(assertion_id="new", attribute="weight", value_kind="measurement", unit="kg", value=190,
             valid_at="2026-07-25T00:00:00+00:00", absolute_semantics="ordinary"),
    ])
    assert s.value == 190  # latest valid_at wins; correction only breaks an EQUAL-time tie


@pytest.mark.unit
def test_no_correction_cue_leaves_ambiguous_anchor_unchanged():
    # acceptance 8: distinct-value ordinary tie with NO correction -> still AMBIGUOUS_ANCHOR.
    r = fold_assertions([
        _row(assertion_id="a", attribute="weight", value_kind="measurement", unit="kg", value=180,
             valid_at="2026-07-20T00:00:00+00:00", absolute_semantics="ordinary"),
        _row(assertion_id="b", attribute="weight", value_kind="measurement", unit="kg", value=182,
             valid_at="2026-07-20T00:00:00+00:00", absolute_semantics="ordinary"),
    ])
    assert not r.states and r.abstentions[0].reason == AMBIGUOUS_ANCHOR


@pytest.mark.unit
def test_correction_default_missing_semantics_is_ordinary():
    # a row with NO absolute_semantics key behaves as ordinary (legacy-safe) -> distinct-value tie ambiguous.
    r = fold_assertions([
        _row(assertion_id="a", attribute="weight", value_kind="measurement", unit="kg", value=180,
             valid_at="2026-07-20T00:00:00+00:00"),
        _row(assertion_id="b", attribute="weight", value_kind="measurement", unit="kg", value=182,
             valid_at="2026-07-20T00:00:00+00:00"),
    ])
    assert not r.states and r.abstentions[0].reason == AMBIGUOUS_ANCHOR


@pytest.mark.unit
def test_delta_on_range_anchor_abstains():
    r = fold_assertions([
        _row(assertion_id="anc", attribute="spent", scope="", value_kind="duration", unit="hours",
             value=[10, 12], valid_at="2026-07-01T00:00:00+00:00"),
        _row(assertion_id="d1", attribute="spent", scope="", value_kind="duration", unit="hours",
             operation="delta", value=2, valid_at="2026-07-05T00:00:00+00:00"),
    ])
    assert not r.states and r.abstentions[0].reason == DELTA_ON_RANGE


# --------------------------------------------------------------------- authority tier

@pytest.mark.unit
def test_effective_tier_is_weakest_contributor():
    # user anchor + agent delta -> derived value is agent-grade (weakest required input).
    s = _one([
        _row(assertion_id="anc", value=37, evidence_tier="user", valid_at="2026-07-01T00:00:00+00:00"),
        _row(assertion_id="d1", operation="delta", value=1, evidence_tier="agent",
             valid_at="2026-07-05T00:00:00+00:00"),
    ])
    assert s.value == 38 and s.effective_tier == "agent"


@pytest.mark.unit
def test_effective_tier_of_pure_absolute_is_its_own_tier():
    s = _one([_row(assertion_id="anc", value=40, evidence_tier="user")])
    assert s.effective_tier == "user"


# --------------------------------------------------------------------- slot isolation + determinism

@pytest.mark.unit
def test_distinct_slots_fold_independently():
    r = fold_assertions([
        _row(assertion_id="o", attribute="owned", value=38),
        _row(assertion_id="s", attribute="sold", value=5),
    ])
    assert len(r.states) == 2
    by_attr = {s.attribute: s.value for s in r.states}
    assert by_attr == {"owned": 38, "sold": 5}


@pytest.mark.unit
def test_fold_is_deterministic_regardless_of_input_order():
    rows = [
        _row(assertion_id="anc", value=37, valid_at="2026-07-01T00:00:00+00:00",
             subject_display="disp-anchor"),
        _row(assertion_id="d1", operation="delta", value=1, valid_at="2026-07-05T00:00:00+00:00",
             subject_display="disp-other"),
        _row(assertion_id="d2", operation="delta", value=2, valid_at="2026-07-06T00:00:00+00:00",
             subject_display="disp-other2"),
    ]
    a = fold_assertions(rows)
    b = fold_assertions(list(reversed(rows)))
    assert a.states[0].value == b.states[0].value == 40
    assert a.states[0].contributor_ids == b.states[0].contributor_ids
    # deterministic display: from the ANCHOR, not members[0] — same under any input order.
    assert a.states[0].subject_display == b.states[0].subject_display == "disp-anchor"


@pytest.mark.unit
def test_valid_at_is_latest_contributors_own_timestamp():
    # a later delta advances valid_at to the delta's timestamp (chronological, not lexical max).
    s = _one([
        _row(assertion_id="anc", value=37, valid_at="2026-07-01T00:00:00+00:00"),
        _row(assertion_id="d1", operation="delta", value=1, valid_at="2026-12-09T00:00:00+00:00"),
    ])
    assert s.valid_at == "2026-12-09T00:00:00+00:00"


@pytest.mark.unit
def test_episode_provenance_collected_from_contributors():
    s = _one([
        _row(assertion_id="anc", value=37, episode_uuid="ep-a"),
        _row(assertion_id="d1", operation="delta", value=1, episode_uuid="ep-b",
             valid_at="2026-07-05T00:00:00+00:00"),
    ])
    assert s.episode_uuids == ["ep-a", "ep-b"]


# --------------------------------------------------------------------- rebuild service + parity

class _FakeAssertions:
    def __init__(self, rows, repairs=None):
        self._rows = rows
        self._repairs = list(repairs or [])
        self.completed_repairs = []

    def materializable_assertions_for_entity(self, subject_uuid, *, namespace=None):
        # mirrors the repo: current + fully-bound only (drops binding_pending); optional namespace scope.
        return [r for r in self._rows
                if r["subject_uuid"] == subject_uuid and not r.get("binding_pending", False)
                and (namespace is None or r.get("namespace") == namespace)]

    def pending_projection_repairs(self, *, operation_id=None, namespaces=None, limit=200):
        rows = self._repairs
        if operation_id is not None:
            rows = [r for r in rows if r.get("operation_id") == operation_id]
        if namespaces is not None:
            rows = [r for r in rows if r.get("namespace") in namespaces]
        return rows[:limit]

    def mark_projection_repair_complete(self, repair_key):
        self.completed_repairs.append(repair_key)
        return True

    def claim_due_scalar_activations(self, *, as_of, namespaces=None, limit=200):
        return self._repairs[:limit]


class _FakeViews:
    def __init__(self, existing=None, stale_skip=False):
        self.calls = []
        self.retired = []
        self._existing = list(existing or [])  # current scalar Views before rebuild
        self._stale_skip = stale_skip          # simulate a (defensive) LWW rejection

    def record_scalar_state(self, **kwargs):
        self.calls.append(kwargs)
        vk = f"::{kwargs['subject_uuid']}::{kwargs['attribute']}:{kwargs['scope']}"
        if self._stale_skip:
            return {"view_key": vk, "stale_skipped": True}
        return {"view_key": vk}

    def list_scalar_state_views(self, *, subject_uuid, namespace=None):
        return [v for v in self._existing if v.get("subject_uuid") == subject_uuid]

    def retire_scalar_state(self, *, view_key):
        self.retired.append(view_key)
        return True


@pytest.mark.unit
def test_rebuild_writes_folded_views_with_contributors_tier_and_episodes():
    rows = [
        _row(assertion_id="anc", value=37, evidence_tier="user", episode_uuid="ep-a",
             valid_at="2026-07-01T00:00:00+00:00", namespace="ns"),
        _row(assertion_id="d1", operation="delta", value=1, evidence_tier="agent", episode_uuid="ep-b",
             valid_at="2026-07-05T00:00:00+00:00", namespace="ns"),
    ]
    views = _FakeViews()
    svc = ScalarStateService(_FakeAssertions(rows), views)
    # rebuild scoped to "ns" now folds only that silo's assertions (C.4.4 namespace-scoped read)
    out = svc.rebuild_scalar_state("ent-coins", namespace="ns")
    assert out["written"] == 1 and out["abstained"] == [] and out["retired"] == 0
    call = views.calls[0]
    assert call["value"] == 38 and call["subject_uuid"] == "ent-coins"
    # the fold's contributor ids + weakest tier ride on the audit receipt (authority signal for D)
    assert call["audit"]["scalar_contributors"] == ["anc", "d1"]
    assert call["audit"]["scalar_effective_tier"] == "agent"
    assert call["episode_uuids"] == ["ep-a", "ep-b"]  # View gets normal episode provenance
    assert out["results"][0]["contributor_ids"] == ["anc", "d1"]


@pytest.mark.unit
def test_rebuild_as_of_threads_through_and_excludes_future_assertions():
    # acceptance 29 (service level): rebuild captures as_of ONCE and threads it into the fold, so a
    # future-dated absolute is excluded pre-activation and written post-activation.
    rows = [
        _row(assertion_id="cur", subject_uuid="ent-coins", attribute="owned", value_kind="count",
             unit="", value=37, valid_at="2026-07-20T00:00:00+00:00", namespace="ns"),
        _row(assertion_id="fut", subject_uuid="ent-coins", attribute="owned", value_kind="count",
             unit="", value=50, valid_at="2026-07-27T00:00:00+00:00", namespace="ns"),
    ]
    svc = ScalarStateService(_FakeAssertions(rows), _FakeViews())
    before = svc.rebuild_scalar_state("ent-coins", namespace="ns", as_of=_dt("2026-07-22T00:00:00+00:00"))
    after = svc.rebuild_scalar_state("ent-coins", namespace="ns", as_of=_dt("2026-07-27T00:00:00+00:00"))
    assert before["results"][0]["value"] == 37   # future 50 excluded pre-activation
    assert after["results"][0]["value"] == 50     # activates at its valid_at


@pytest.mark.unit
def test_delete_repair_rebuilds_as_of_now_then_completes_receipt():
    assertions = _FakeAssertions([], repairs=[{
        "repair_key": "repair-1", "operation_id": "op-1",
        "subject_uuid": "ent-coins", "namespace": "ns",
    }])
    views = _FakeViews(existing=[{
        "subject_uuid": "ent-coins", "view_key": "vk-owned", "attribute": "owned",
        "scope": "pre-1920", "value_kind": "count", "unit": "",
    }])
    svc = ScalarStateService(assertions, views)
    evaluation = _dt("2026-07-22T12:00:00+00:00")

    out = svc.repair_pending_deletions(operation_id="op-1", as_of=evaluation)

    assert out == {"repaired": ["repair-1"], "failed": [], "examined": 1}
    assert views.retired == ["vk-owned"]
    assert assertions.completed_repairs == ["repair-1"]


@pytest.mark.unit
def test_binding_pending_assertions_never_materialize():
    # a pending (unbound) assertion must NOT produce a View, even if it would fold cleanly.
    rows = [_row(assertion_id="pend", value=5, binding_pending=True)]
    views = _FakeViews()
    out = ScalarStateService(_FakeAssertions(rows), views).rebuild_scalar_state("ent-coins")
    assert out["written"] == 0 and views.calls == []


@pytest.mark.unit
def test_rebuild_retires_the_view_for_an_expired_slot():
    # "I used to wake up at 9:00" (a lone expire) -> the slot expires: no View is written and any
    # existing wake_time View is retired (expired-with-current-unknown). The result surfaces the expiry.
    rows = [_row(
        assertion_id="e", attribute="wake_time", scope="", value_kind="clock_time", unit="",
        operation="expire", value="09:00", valid_at="2026-07-01T00:00:00+00:00")]
    existing = [{"subject_uuid": "ent-coins", "view_key": "vk-wake",
                 "attribute": "wake_time", "scope": "", "value_kind": "clock_time", "unit": ""}]
    views = _FakeViews(existing=existing)
    out = ScalarStateService(_FakeAssertions(rows), views).rebuild_scalar_state("ent-coins")
    assert out["written"] == 0 and out["retired"] == 1
    assert views.retired == ["vk-wake"]
    assert out["expired"] and out["expired"][0]["expired_value"] == "09:00"


@pytest.mark.unit
def test_rebuild_retires_stale_slot_after_semantic_correction():
    # C.1 correction moved the claim owned->sold; only `sold` is current. The old `owned` View must
    # be retired, not left recallable.
    rows = [_row(assertion_id="s1", attribute="sold", value=5, scope="pre-1920")]
    existing = [
        {"subject_uuid": "ent-coins", "view_key": "vk-owned",
         "attribute": "owned", "scope": "pre-1920", "value_kind": "count", "unit": ""},
        {"subject_uuid": "ent-coins", "view_key": "vk-sold",
         "attribute": "sold", "scope": "pre-1920", "value_kind": "count", "unit": ""},
    ]
    views = _FakeViews(existing=existing)
    out = ScalarStateService(_FakeAssertions(rows), views).rebuild_scalar_state("ent-coins")
    assert out["written"] == 1 and out["retired"] == 1
    assert views.retired == ["vk-owned"]  # the vanished slot retired; the desired `sold` kept


@pytest.mark.unit
def test_rebuild_retires_slot_that_now_abstains():
    # the slot still has assertions but folds to an abstention (ambiguous tie) -> retire its View.
    rows = [
        _row(assertion_id="a1", value=37, valid_at="2026-08-01T00:00:00+00:00"),
        _row(assertion_id="a2", value=38, valid_at="2026-08-01T00:00:00+00:00"),
    ]
    existing = [{"subject_uuid": "ent-coins", "view_key": "vk-owned",
                 "attribute": "owned", "scope": "pre-1920", "value_kind": "count", "unit": ""}]
    views = _FakeViews(existing=existing)
    out = ScalarStateService(_FakeAssertions(rows), views).rebuild_scalar_state("ent-coins")
    assert out["written"] == 0 and out["retired"] == 1 and views.retired == ["vk-owned"]


@pytest.mark.unit
def test_stale_skipped_write_is_not_counted_and_does_not_protect_stale_view():
    # Defense-in-depth: if a rebuild write is (unexpectedly) LWW-rejected, the slot must NOT count as
    # written and must NOT be added to desired_slots — so its stale View is still retired, not
    # concealed. Existing owned View + a stale-skipping sink -> written=0, retired=1, reported.
    rows = [_row(assertion_id="a", attribute="owned", value=37)]
    existing = [{"subject_uuid": "ent-coins", "view_key": "vk-owned",
                 "attribute": "owned", "scope": "pre-1920", "value_kind": "count", "unit": ""}]
    views = _FakeViews(existing=existing, stale_skip=True)
    out = ScalarStateService(_FakeAssertions(rows), views).rebuild_scalar_state("ent-coins")
    assert out["written"] == 0
    assert len(out["stale_skipped"]) == 1
    assert out["retired"] == 1 and views.retired == ["vk-owned"]  # not protected by the failed write


@pytest.mark.unit
def test_current_authority_reads_from_event_log_not_view_props():
    # authority SSOT: derived from the fold of current assertions (weakest contributor), per slot.
    rows = [
        _row(assertion_id="anc", value=37, evidence_tier="user", valid_at="2026-07-01T00:00:00+00:00"),
        _row(assertion_id="d1", operation="delta", value=1, evidence_tier="agent",
             valid_at="2026-07-05T00:00:00+00:00"),
    ]
    svc = ScalarStateService(_FakeAssertions(rows), _FakeViews())
    auth = svc.current_authority("ent-coins")
    assert auth[("owned", "pre-1920", "count", "")] == "agent"


@pytest.mark.unit
def test_current_authority_default_never_grants_future_value_early():
    rows = [
        _row(assertion_id="now", value=37, valid_at="2020-01-01T00:00:00+00:00"),
        _row(assertion_id="future", value=99, evidence_tier="agent",
             valid_at="2099-01-01T00:00:00+00:00"),
    ]
    svc = ScalarStateService(_FakeAssertions(rows), _FakeViews())

    authority = svc.current_authority("ent-coins")
    folded = svc.fold_entity("ent-coins", as_of=_dt("2026-07-22T00:00:00+00:00"))

    assert authority[("owned", "pre-1920", "count", "")] == "user"
    assert folded.states[0].value == 37


@pytest.mark.unit
def test_due_activation_claims_then_repairs_at_one_evaluation_time():
    assertions = _FakeAssertions([], repairs=[{
        "repair_key": "TIME_ACTIVATION\x1fa-future", "operation_id": "a-future",
        "subject_uuid": "ent-coins", "namespace": "ns",
    }])
    svc = ScalarStateService(assertions, _FakeViews())

    out = svc.activate_due_assertions(as_of=_dt("2026-07-22T12:00:00+00:00"), namespaces=["ns"])

    assert out["claimed"] == 1
    assert out["repaired"] == ["TIME_ACTIVATION\x1fa-future"]


@pytest.mark.unit
def test_rebuild_reports_abstentions_and_writes_nothing_for_them():
    rows = [
        _row(assertion_id="a1", value=37, valid_at="2026-08-01T00:00:00+00:00"),
        _row(assertion_id="a2", value=38, valid_at="2026-08-01T00:00:00+00:00"),  # ambiguous tie
    ]
    views = _FakeViews()
    out = ScalarStateService(_FakeAssertions(rows), views).rebuild_scalar_state("ent-coins")
    assert out["written"] == 0 and views.calls == []
    assert out["abstained"][0]["reason"] == AMBIGUOUS_ANCHOR


@pytest.mark.unit
def test_live_fold_and_rebuild_agree_by_construction():
    # "live" fold (direct) and rebuild (via the service) use the SAME pure fold over the same
    # assertion set, so the folded value + contributors are identical.
    rows = [
        _row(assertion_id="anc", value=10, valid_at="2026-07-01T00:00:00+00:00"),
        _row(assertion_id="d1", operation="delta", value=3, valid_at="2026-07-02T00:00:00+00:00"),
    ]
    live = fold_assertions(rows).states[0]
    views = _FakeViews()
    ScalarStateService(_FakeAssertions(rows), views).rebuild_scalar_state("ent-coins")
    rebuilt = views.calls[0]
    assert rebuilt["value"] == live.value
    assert rebuilt["audit"]["scalar_contributors"] == live.contributor_ids
