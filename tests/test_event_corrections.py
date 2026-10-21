"""A later correction replaces the one earlier event it restates (#170 item 5).

"I bought a bike." then "Actually, I bought the bike on March 3." used to leave two "acquired bike"
entries. Perception now stamps the turn's time (`stated_at`) and a correction cue in metadata, and
every reader (timeline rebuild, selector, recall) drops the single earlier same-object occurrence.
With two or more candidates nothing is dropped; nothing durable is changed.
"""

from __future__ import annotations

import itertools

import pytest

from menhir.domain.event_history import (
    EVENT_CORRECTION,
    EVENT_STATED_AT,
    EventLane,
    EventSelectionIntent,
    SelectionStatus,
    TypedEventAssertion,
    apply_event_corrections,
    select_event_assertion,
)
from menhir.services.event_history_authority import event_authority_for_query
from menhir.services.event_history_perception import (
    EventPerceptionProposal,
    build_event_assertion,
    is_event_correction,
)
from menhir.services.event_history_recall import (
    classify_event_query,
    select_event_recall,
)
from menhir.services.event_history_service import EventHistoryService

pytestmark = pytest.mark.unit

NS = "ns-1"
SUBJECT = "user-1"
_episodes = itertools.count()


def _ev(obj: str, valid_at: str, said: str | None, *, correction: bool = False,
        domain: str | None = None, time_basis: str = "explicit") -> TypedEventAssertion:
    meta: dict = {}
    if said is not None:
        meta[EVENT_STATED_AT] = said
    if correction:
        meta[EVENT_CORRECTION] = True
    return TypedEventAssertion(
        subject_uuid=SUBJECT, subject_display="user", predicate="acquired", object_key=obj,
        object_display=obj, valid_at=valid_at, learned_at="2026-08-01T00:00:00+00:00",
        stated_span=f"bought the {obj}", episode_uuid=f"ep-{next(_episodes)}", span_start=0,
        span_end=10, domain=domain, namespace=NS, time_basis=time_basis, metadata=meta,
    )


def _objects(events) -> list[tuple[str, str]]:
    return sorted((e.object_key, e.valid_at[:10]) for e in events)


def test_a_correction_replaces_the_one_earlier_entry() -> None:
    original = _ev("bike", "2026-07-06T10:00:00+00:00", "2026-07-06T10:00:00+00:00")
    other = _ev("lamp", "2026-07-05T10:00:00+00:00", "2026-07-05T10:00:00+00:00")
    fix = _ev("bike", "2026-03-03T00:00:00+00:00", "2026-07-08T10:00:00+00:00", correction=True)
    assert _objects(apply_event_corrections([original, other, fix])) == [
        ("bike", "2026-03-03"), ("lamp", "2026-07-05")]


def test_two_earlier_candidates_keep_everything() -> None:
    events = [_ev("bike", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
              _ev("bike", "2026-05-01T00:00:00+00:00", "2026-05-01T00:00:00+00:00"),
              _ev("bike", "2026-03-03T00:00:00+00:00", "2026-07-08T00:00:00+00:00", correction=True)]
    assert len(apply_event_corrections(events)) == 3


def test_a_correction_said_first_never_hides_a_later_purchase() -> None:
    fix = _ev("bike", "2026-03-03T00:00:00+00:00", "2026-07-01T00:00:00+00:00", correction=True)
    later = _ev("bike", "2026-07-09T00:00:00+00:00", "2026-07-09T00:00:00+00:00")
    assert len(apply_event_corrections([fix, later])) == 2


def test_a_correction_of_a_correction_leaves_the_last() -> None:
    a = _ev("bike", "2026-07-06T00:00:00+00:00", "2026-07-06T00:00:00+00:00")
    b = _ev("bike", "2026-03-03T00:00:00+00:00", "2026-07-07T00:00:00+00:00", correction=True)
    c = _ev("bike", "2026-03-04T00:00:00+00:00", "2026-07-08T00:00:00+00:00", correction=True)
    for order in itertools.permutations([a, b, c]):
        assert _objects(apply_event_corrections(order)) == [("bike", "2026-03-04")]


def test_other_objects_and_other_lanes_are_untouched() -> None:
    events = [_ev("bike", "2026-07-06T00:00:00+00:00", "2026-07-06T00:00:00+00:00", domain="sport"),
              _ev("lamp", "2026-07-06T00:00:00+00:00", "2026-07-06T00:00:00+00:00"),
              _ev("bike", "2026-03-03T00:00:00+00:00", "2026-07-08T00:00:00+00:00", correction=True)]
    assert len(apply_event_corrections(events)) == 3


def test_an_old_assertion_without_stated_at() -> None:
    fix = _ev("bike", "2026-03-03T00:00:00+00:00", "2026-07-08T00:00:00+00:00", correction=True)
    # Its valid_at is the turn time when it fell back to the episode reference ...
    by_reference = _ev("bike", "2026-07-06T00:00:00+00:00", None, time_basis="episode_reference")
    assert apply_event_corrections([by_reference, fix]) == [fix]
    # ... but an explicit date says nothing about when it was said, so it stays.
    explicit = _ev("bike", "2026-07-06T00:00:00+00:00", None)
    assert len(apply_event_corrections([explicit, fix])) == 2


def test_idempotent_and_replays_collapse_together() -> None:
    a = _ev("bike", "2026-07-06T00:00:00+00:00", "2026-07-06T00:00:00+00:00")
    fix = _ev("bike", "2026-03-03T00:00:00+00:00", "2026-07-08T00:00:00+00:00", correction=True)
    once = apply_event_corrections([a, a, fix, fix])
    assert once == [fix, fix]
    assert apply_event_corrections(once) == once


def test_selector_latest_follows_the_correction() -> None:
    a = _ev("bike", "2026-07-06T00:00:00+00:00", "2026-07-06T00:00:00+00:00")
    fix = _ev("bike", "2026-03-03T00:00:00+00:00", "2026-07-08T00:00:00+00:00", correction=True)
    lane = EventLane(subject_uuid=SUBJECT, predicate="acquired", namespace=NS)
    sel = select_event_assertion([a, fix], lane=lane, intent=EventSelectionIntent.LATEST)
    assert sel.status is SelectionStatus.UNIQUE
    assert sel.selected is fix


def test_recall_follows_the_correction() -> None:
    a = _ev("bike", "2026-07-06T00:00:00+00:00", "2026-07-06T00:00:00+00:00")
    fix = _ev("bike", "2026-03-03T00:00:00+00:00", "2026-07-08T00:00:00+00:00", correction=True)
    route = classify_event_query("which item did you purchase most recently?")
    sel = select_event_recall([a, fix], route=route, subject_uuid=SUBJECT, namespace=NS)
    assert sel.selected is fix, (route, sel)


def test_a_corrected_original_is_not_a_second_anchor() -> None:
    a = _ev("bike", "2026-07-06T00:00:00+00:00", "2026-07-06T00:00:00+00:00")
    fix = _ev("bike", "2026-03-03T00:00:00+00:00", "2026-07-08T00:00:00+00:00", correction=True)
    query = "Before I bought the bike, do I own any other bikes?"
    plain = _ev("bike", "2026-03-03T00:00:00+00:00", "2026-07-08T00:00:00+00:00")
    # Two uncorrected bikes are an ambiguous anchor; the correction leaves one.
    unresolved = event_authority_for_query(query, [a, plain], subject_uuid=SUBJECT, namespace=NS,
                                           foundation_verified=True)
    assert unresolved is not None and unresolved.kind == "anchor_guard"
    assert event_authority_for_query(query, [a, fix], subject_uuid=SUBJECT, namespace=NS,
                                     foundation_verified=True) is None


def test_timeline_rebuild_keeps_only_the_corrected_entry() -> None:
    service = EventHistoryService.__new__(EventHistoryService)
    a = _ev("bike", "2026-07-06T00:00:00+00:00", "2026-07-06T00:00:00+00:00")
    fix = _ev("bike", "2026-03-03T00:00:00+00:00", "2026-07-08T00:00:00+00:00", correction=True)
    entries, excluded, _ = service._build_entries([a, fix])
    assert excluded == 0
    assert [e["assertion_key"] for e in entries] == [fix.assertion_key]


@pytest.mark.parametrize("sentence,expected", [
    ("Actually, I bought the bike on March 3.", True),
    ("I meant that I bought the bike on March 3.", True),
    ("I bought the bike on Tuesday, not Monday, sorry.", True),
    ("I bought a bike on March 3.", False),
    ("Actually, I bought a new bike today.", False),
    ("Actually, I bought another bike today.", False),
    ("Actually I bought the bike again.", False),
])
def test_correction_cue(sentence, expected) -> None:
    assert is_event_correction(sentence) is expected


def _proposal(sentence: str) -> EventPerceptionProposal:
    start = sentence.index("bought")
    return EventPerceptionProposal(
        subject_text="user", predicate="acquired", object_key="bike", object_display="bike",
        stated_span=sentence[start:start + 15], episode_uuid="ep-1", span_start=start,
        span_end=start + 15, when_context=sentence,
    )


def test_builder_stamps_stated_at_and_the_cue() -> None:
    built = build_event_assertion(
        _proposal("Actually, I bought the bike on March 3."), subject_uuid=SUBJECT, namespace=NS,
        learned_at="2026-08-01T00:00:00+00:00", episode_reference_time="2026-07-08T10:00:00+00:00",
        turn_evidence_uuid="ep-1", perceiver_version="v1",
    ).assertion
    assert built is not None
    assert built.metadata == {EVENT_STATED_AT: "2026-07-08T10:00:00+00:00", EVENT_CORRECTION: True}


def test_builder_ordinary_turn_carries_only_stated_at() -> None:
    built = build_event_assertion(
        _proposal("I bought the bike yesterday."), subject_uuid=SUBJECT, namespace=NS,
        learned_at="2026-08-01T00:00:00+00:00", episode_reference_time="2026-07-08T10:00:00+00:00",
        turn_evidence_uuid="ep-1", perceiver_version="v1",
    ).assertion
    assert built is not None
    assert built.metadata == {EVENT_STATED_AT: "2026-07-08T10:00:00+00:00"}


def test_stamp_is_not_identity() -> None:
    a = _ev("bike", "2026-07-06T00:00:00+00:00", None)
    b = TypedEventAssertion(**{**a.__dict__, "metadata": {EVENT_STATED_AT: "2026-07-06T00:00:00Z",
                                                           EVENT_CORRECTION: True}})
    assert (a.assertion_key, a.source_key) == (b.assertion_key, b.source_key)
