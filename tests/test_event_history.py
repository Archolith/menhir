"""Generic synthetic tests for the event-history domain contract and selector (Phase 1).

Covers the fail-closed contract and the deterministic, ingestion-order-independent selector:
required-field + span validation, stable identity keys, lane isolation, out-of-order input, exact
replay dedup, as_of future exclusion, malformed valid_at exclusion, equal-time ambiguity, predecessor
by anchor (assertion key / explicit time), predecessor's required anchor + strictly-earlier rule,
and the guarantee that learned_at/input order never break a valid_at tie.

No benchmark-specific IDs, answers, fixture wording, or rules are used.
"""

from __future__ import annotations

import pytest

from menhir.domain.event_history import (
    EventLane,
    EventSelection,
    EventSelectionIntent,
    GATE_AMBIGUITY,
    GATE_ANCHOR,
    GATE_INVALID,
    GATE_NO_CANDIDATE,
    GATE_PASS,
    GATE_TIME,
    SelectionStatus,
    TypedEventAssertion,
    select_event_assertion,
)


def make(subject_uuid="subj-a", predicate="purchased", object_key="obj-x",
         domain=None, namespace=None, valid_at="2026-01-05T00:00:00Z",
         learned_at="2026-01-06T00:00:00Z", episode_uuid="ep-1", **kw) -> TypedEventAssertion:
    base = dict(
        subject_uuid=subject_uuid,
        subject_display="Alpha",
        predicate=predicate,
        object_key=object_key,
        object_display="Object X",
        domain=domain,
        namespace=namespace,
        valid_at=valid_at,
        learned_at=learned_at,
        stated_span="an exact stated span",
        episode_uuid=episode_uuid,
        span_start=10,
        span_end=30,
        claim_ordinal=0,
    )
    base.update(kw)
    return TypedEventAssertion(**base)


L = EventSelectionIntent.LATEST
P = EventSelectionIntent.PREDECESSOR
LANE = EventLane(subject_uuid="subj-a", predicate="purchased", domain=None, namespace=None)


# --------------------------------------------------------------------------- validation

@pytest.mark.unit
def test_required_identity_and_grounding_fields_fail_closed() -> None:
    with pytest.raises(ValueError):
        make(subject_uuid="  ")
    with pytest.raises(ValueError):
        make(predicate="")
    with pytest.raises(ValueError):
        make(object_key="  ")
    with pytest.raises(ValueError):
        make(stated_span="")
    with pytest.raises(ValueError):
        make(episode_uuid=" ")
    with pytest.raises(ValueError):
        make(valid_at="")
    with pytest.raises(ValueError):
        make(learned_at=" ")


@pytest.mark.unit
def test_invalid_span_pairs_fail_closed() -> None:
    with pytest.raises(ValueError):
        make(span_start=-1, span_end=5)   # half-known
    with pytest.raises(ValueError):
        make(span_start=20, span_end=10)  # end < start
    with pytest.raises(ValueError):
        make(span_start=-2, span_end=-1)  # neither -1 nor both known
    with pytest.raises(ValueError):
        make(claim_ordinal=-1)


@pytest.mark.unit
def test_invalid_tier_and_time_basis_fail_closed() -> None:
    with pytest.raises(ValueError):
        make(evidence_tier="omnipotent")
    with pytest.raises(ValueError):
        make(time_basis="crystal_ball")


@pytest.mark.unit
def test_required_fields_tolerate_none_and_raise_valueerror() -> None:
    with pytest.raises(ValueError):
        make(subject_uuid=None)
    with pytest.raises(ValueError):
        make(predicate=None)
    with pytest.raises(ValueError):
        make(object_key=None)
    with pytest.raises(ValueError):
        make(valid_at=None)
    with pytest.raises(ValueError):
        make(learned_at=None)


@pytest.mark.unit
def test_event_lane_requires_identity_fields() -> None:
    with pytest.raises(ValueError):
        EventLane(subject_uuid="  ", predicate="purchased")
    with pytest.raises(ValueError):
        EventLane(subject_uuid="subj-a", predicate="   ")
    with pytest.raises(ValueError):
        EventLane(subject_uuid=None, predicate="purchased")
    # optional namespace/domain may be blank.
    EventLane(subject_uuid="subj-a", predicate="purchased", namespace="", domain=None)


# --------------------------------------------------------------------------- identity keys

@pytest.mark.unit
def test_stable_keys_are_deterministic_and_source_grounded() -> None:
    a = make()
    b = make()
    assert a.source_key == b.source_key
    assert a.assertion_key == b.assertion_key
    # source_key is grounded in episode/span/ordinal, not in semantics or ingest time:
    # same span+episode -> same source_key regardless of interpretation (object_key).
    assert a.source_key == make(object_key="other").source_key
    assert a.source_key != make(episode_uuid="ep-2").source_key
    assert a.source_key != make(span_start=0).source_key
    assert a.source_key == make(learned_at="2099-01-01T00:00:00Z").source_key  # learned_at ignored


@pytest.mark.unit
def test_assertion_key_uses_interpretation_not_learned_at() -> None:
    a = make(object_key="obj-x")
    different = make(object_key="obj-y")
    same_key_diff_learned = make(learned_at="2099-01-01T00:00:00Z")
    assert a.assertion_key != different.assertion_key
    assert a.assertion_key == same_key_diff_learned.assertion_key


@pytest.mark.unit
def test_lane_isolation_by_namespace_predicate_and_domain() -> None:
    ev = [
        make(subject_uuid="subj-a", predicate="purchased", domain=None),
        make(subject_uuid="subj-a", predicate="purchased", domain="other-dom"),
        make(subject_uuid="subj-a", predicate="sold", domain=None),
        make(subject_uuid="subj-b", predicate="purchased", domain=None),
        make(subject_uuid="subj-a", predicate="purchased", domain=None, namespace="other-ns"),
    ]
    res = select_event_assertion(ev, lane=LANE, intent=L)
    assert res.status is SelectionStatus.UNIQUE
    assert res.selected is not None
    assert res.selected.subject_uuid == "subj-a"
    assert res.selected.predicate == "purchased"
    assert res.selected.domain is None
    assert res.selected.namespace is None


@pytest.mark.unit
def test_event_lane_key_matches_assertion_lane_and_normalizes() -> None:
    lane = EventLane(subject_uuid=" Subj-A ", predicate="  Purchased ", namespace="", domain="  ")
    a = make(subject_uuid="subj-a", predicate="purchased")
    assert lane.key == a.lane_key


# --------------------------------------------------------------------------- selection: LATEST

@pytest.mark.unit
def test_latest_is_order_independent() -> None:
    early = make(valid_at="2026-01-05T00:00:00Z")
    mid = make(valid_at="2026-02-05T00:00:00Z", span_start=40, span_end=50, claim_ordinal=1)
    late = make(valid_at="2026-03-05T00:00:00Z", span_start=60, span_end=70, claim_ordinal=2)
    for order in (([late, early, mid]), ([early, late, mid]), ([mid, late, early])):
        res = select_event_assertion(order, lane=LANE, intent=L)
        assert res.status is SelectionStatus.UNIQUE
        assert res.selected.valid_at == "2026-03-05T00:00:00Z"


@pytest.mark.unit
def test_as_of_excludes_future_events() -> None:
    early = make(valid_at="2026-01-05T00:00:00Z")
    late = make(valid_at="2026-05-05T00:00:00Z", span_start=40, span_end=50, claim_ordinal=1)
    res = select_event_assertion([early, late], lane=LANE, intent=L, as_of="2026-03-01T00:00:00Z")
    assert res.status is SelectionStatus.UNIQUE
    assert res.selected.valid_at == "2026-01-05T00:00:00Z"
    # at exactly the bound, the bound itself is eligible (at/before).
    res2 = select_event_assertion([early], lane=LANE, intent=L, as_of="2026-01-05T00:00:00Z")
    assert res2.status is SelectionStatus.UNIQUE


@pytest.mark.unit
def test_malformed_valid_at_cannot_lead() -> None:
    good = make(valid_at="2026-01-05T00:00:00Z")
    bad = make(valid_at="not-a-time", span_start=40, span_end=50, claim_ordinal=1)
    bad2 = make(valid_at="2026-13-99T00:00:00Z", span_start=60, span_end=70, claim_ordinal=2)
    res = select_event_assertion([bad, bad2, good], lane=LANE, intent=L)
    assert res.status is SelectionStatus.UNIQUE
    assert res.selected is good
    # if every candidate time is malformed, nothing can win.
    res2 = select_event_assertion([bad, bad2], lane=LANE, intent=L)
    assert res2.status is SelectionStatus.NONE


@pytest.mark.unit
def test_empty_lane_yields_no_candidate() -> None:
    res = select_event_assertion([], lane=LANE, intent=L)
    assert res.status is SelectionStatus.NONE
    assert res.gate == GATE_NO_CANDIDATE
    assert not res.has_unique_selection


@pytest.mark.unit
def test_malformed_as_of_is_invalid() -> None:
    ev = [make()]
    res = select_event_assertion(ev, lane=LANE, intent=L, as_of="garbage")
    assert res.status is SelectionStatus.INVALID
    assert res.gate == GATE_TIME


# --------------------------------------------------------------------------- ambiguity

@pytest.mark.unit
def test_equal_time_distinct_winner_is_ambiguous() -> None:
    a = make(valid_at="2026-01-05T00:00:00Z", object_key="obj-x")
    b = make(valid_at="2026-01-05T00:00:00Z", object_key="obj-y", span_start=40, span_end=50,
             claim_ordinal=1)
    res = select_event_assertion([a, b], lane=LANE, intent=L)
    assert res.status is SelectionStatus.AMBIGUOUS
    assert res.gate == GATE_AMBIGUITY
    assert res.selected is None
    assert not res.has_unique_selection


@pytest.mark.unit
def test_learned_at_and_input_order_do_not_break_valid_at_tie() -> None:
    a = make(valid_at="2026-01-05T00:00:00Z", object_key="obj-x", learned_at="2026-01-01T00:00:00Z")
    b = make(valid_at="2026-01-05T00:00:00Z", object_key="obj-y", learned_at="2026-01-09T00:00:00Z",
             span_start=40, span_end=50, claim_ordinal=1)
    # whichever arrives later/earlier, the tie stays ambiguous.
    for order in (([a, b]), ([b, a])):
        res = select_event_assertion(order, lane=LANE, intent=L)
        assert res.status is SelectionStatus.AMBIGUOUS
        assert res.selected is None


# --------------------------------------------------------------------------- exact replay dedup

@pytest.mark.unit
def test_exact_replay_dedup_is_deterministic() -> None:
    ev = [
        make(valid_at="2026-01-05T00:00:00Z", learned_at="2026-01-06T00:00:00Z"),
        make(valid_at="2026-01-05T00:00:00Z", learned_at="2026-01-07T00:00:00Z"),  # replay of same
    ]
    res = select_event_assertion(ev, lane=LANE, intent=L)
    assert res.status is SelectionStatus.UNIQUE
    assert res.selected is not None
    # a genuine replay must NOT be mistaken for two distinct candidates (would be ambiguous).
    assert res.selected.learned_at == "2026-01-06T00:00:00Z"


@pytest.mark.unit
def test_exact_replay_representative_is_order_independent() -> None:
    early = make(valid_at="2026-01-05T00:00:00Z", learned_at="2026-01-06T00:00:00Z")
    replay = make(valid_at="2026-01-05T00:00:00Z", learned_at="2026-01-07T00:00:00Z")
    # same assertion_key (replay differs only in learned_at); representative is the earliest ingest.
    for order in (([early, replay]), ([replay, early])):
        res = select_event_assertion(order, lane=LANE, intent=L)
        assert res.status is SelectionStatus.UNIQUE
        assert res.selected is not None
        assert res.selected.learned_at == "2026-01-06T00:00:00Z"


@pytest.mark.unit
def test_corrected_valid_at_is_new_assertion_not_replay() -> None:
    original = make(valid_at="2026-01-05T00:00:00Z")
    corrected = make(valid_at="2026-01-06T00:00:00Z")  # same span, corrected world time
    assert original.assertion_key != corrected.assertion_key
    res = select_event_assertion([original, corrected], lane=LANE, intent=L)
    assert res.status is SelectionStatus.UNIQUE
    assert res.selected is corrected


@pytest.mark.unit
def test_exact_replay_of_predecessor_anchor_dedups() -> None:
    anchor = make(valid_at="2026-03-05T00:00:00Z", span_start=60, span_end=70, claim_ordinal=2)
    replay = make(valid_at="2026-03-05T00:00:00Z", span_start=60, span_end=70, claim_ordinal=2,
                  learned_at="2099-01-01T00:00:00Z")
    res = select_event_assertion(
        [anchor, replay], lane=LANE, intent=P, anchor_assertion_key=anchor.assertion_key)
    assert res.status is SelectionStatus.NONE  # nothing strictly before the anchor
    assert res.gate == GATE_NO_CANDIDATE


# --------------------------------------------------------------------------- predecessor

@pytest.mark.unit
def test_predecessor_by_anchor_assertion_key() -> None:
    early = make(valid_at="2026-01-05T00:00:00Z")
    mid = make(valid_at="2026-02-05T00:00:00Z", span_start=40, span_end=50, claim_ordinal=1)
    anchor = make(valid_at="2026-03-05T00:00:00Z", span_start=60, span_end=70, claim_ordinal=2)
    res = select_event_assertion(
        [early, mid, anchor], lane=LANE, intent=P, anchor_assertion_key=anchor.assertion_key)
    assert res.status is SelectionStatus.UNIQUE
    assert res.selected.valid_at == "2026-02-05T00:00:00Z"


@pytest.mark.unit
def test_predecessor_by_explicit_anchor_time() -> None:
    early = make(valid_at="2026-01-05T00:00:00Z")
    late = make(valid_at="2026-05-05T00:00:00Z", span_start=40, span_end=50, claim_ordinal=1)
    res = select_event_assertion(
        [early, late], lane=LANE, intent=P, anchor_time="2026-03-01T00:00:00Z")
    assert res.status is SelectionStatus.UNIQUE
    assert res.selected.valid_at == "2026-01-05T00:00:00Z"


@pytest.mark.unit
def test_predecessor_is_strictly_earlier_than_anchor() -> None:
    at = make(valid_at="2026-02-05T00:00:00Z", span_start=40, span_end=50, claim_ordinal=1)
    anchor = make(valid_at="2026-02-05T00:00:00Z", span_start=60, span_end=70, claim_ordinal=2)
    res = select_event_assertion(
        [at, anchor], lane=LANE, intent=P, anchor_assertion_key=anchor.assertion_key)
    # same-time event is NOT a predecessor (strictly before); nothing qualifies.
    assert res.status is SelectionStatus.NONE
    assert res.gate == GATE_NO_CANDIDATE


@pytest.mark.unit
def test_predecessor_requires_an_anchor() -> None:
    ev = [make()]
    res = select_event_assertion(ev, lane=LANE, intent=P)
    assert res.status is SelectionStatus.INVALID
    assert res.gate == GATE_ANCHOR
    # malformed explicit anchor time is a fail-closed time error.
    res2 = select_event_assertion(ev, lane=LANE, intent=P, anchor_time="not-a-time")
    assert res2.status is SelectionStatus.INVALID
    assert res2.gate == GATE_TIME


@pytest.mark.unit
def test_predecessor_anchor_assertion_missing_is_invalid() -> None:
    ev = [make(valid_at="2026-01-05T00:00:00Z")]
    res = select_event_assertion(
        ev, lane=LANE, intent=P, anchor_assertion_key="does-not-exist")
    assert res.status is SelectionStatus.INVALID
    assert res.gate == GATE_ANCHOR
    assert res.selected is None


# --------------------------------------------------------------------------- result type

@pytest.mark.unit
def test_selection_result_shape_and_gate_strings() -> None:
    ev = [make()]
    res = select_event_assertion(ev, lane=LANE, intent=L)
    assert isinstance(res, EventSelection)
    assert isinstance(res.gate, str)
    assert res.gate == GATE_PASS
    assert res.status is SelectionStatus.UNIQUE
    assert res.has_unique_selection is True
    assert res.selected is not None


# --------------------------------------------------------------------------- param contract

@pytest.mark.unit
def test_object_uuid_rebinding_does_not_fork_assertion_key() -> None:
    a = make(object_key="obj-x", object_uuid="uuid-1")
    rebound = make(object_key="obj-x", object_uuid="uuid-2")  # merge re-resolved the object UUID
    assert a.assertion_key == rebound.assertion_key


@pytest.mark.unit
def test_mutually_exclusive_anchors_are_invalid() -> None:
    ev = [make()]
    res = select_event_assertion(
        ev, lane=LANE, intent=P, anchor_assertion_key="k", anchor_time="2026-01-01T00:00:00Z")
    assert res.status is SelectionStatus.INVALID
    assert res.gate == GATE_ANCHOR
    assert res.selected is None


@pytest.mark.unit
def test_anchor_params_on_latest_are_invalid() -> None:
    ev = [make()]
    by_key = select_event_assertion(ev, lane=LANE, intent=L, anchor_assertion_key="k")
    assert by_key.status is SelectionStatus.INVALID
    assert by_key.gate == GATE_ANCHOR
    by_time = select_event_assertion(ev, lane=LANE, intent=L, anchor_time="2026-01-01T00:00:00Z")
    assert by_time.status is SelectionStatus.INVALID
    assert by_time.gate == GATE_ANCHOR


@pytest.mark.unit
def test_as_of_on_predecessor_is_invalid() -> None:
    ev = [make()]
    res = select_event_assertion(ev, lane=LANE, intent=P, as_of="2026-01-01T00:00:00Z")
    assert res.status is SelectionStatus.INVALID
    assert res.gate == GATE_ANCHOR


@pytest.mark.unit
def test_unsupported_intent_is_invalid() -> None:
    ev = [make()]
    res = select_event_assertion(ev, lane=LANE, intent="rewind")  # type: ignore[arg-type]
    assert res.status is SelectionStatus.INVALID
    assert res.gate == GATE_INVALID
    assert res.selected is None


@pytest.mark.unit
def test_replay_representative_order_independent_when_learned_at_equal_but_metadata_differs() -> None:
    a = make(valid_at="2026-01-05T00:00:00Z", learned_at="2026-01-06T00:00:00Z",
             subject_display="Alpha", metadata={"via": "k1"})
    b = make(valid_at="2026-01-05T00:00:00Z", learned_at="2026-01-06T00:00:00Z",
             subject_display="AlphaPrime", metadata={"via": "k2"})
    # same assertion_key (replay differing only in display/metadata), equal learned_at: the chosen
    # representative must be identical regardless of input order.
    ra = select_event_assertion([a, b], lane=LANE, intent=L)
    rb = select_event_assertion([b, a], lane=LANE, intent=L)
    assert ra.status is SelectionStatus.UNIQUE
    assert rb.status is SelectionStatus.UNIQUE
    assert ra.selected.assertion_key == rb.selected.assertion_key
    assert ra.selected.metadata == rb.selected.metadata
    assert ra.selected.subject_display == rb.selected.subject_display

