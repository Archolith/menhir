"""Generic synthetic tests for the first-person event-history authority layer (Phase 4).

Covers the pure, offline, fail-closed authority boundary: leading only for a unique non-generic
selection with a verified foundation and exact nonblank quote/evidence identity; advisory for generic
routes, unverified foundation, and ambiguous/empty selection; and ``None`` abstention for non-first
person queries and malformed routes. No benchmark IDs, answers, or fixture wording are used.
"""

from __future__ import annotations

import pytest

from menhir.domain.event_history import TypedEventAssertion
from menhir.services.event_history_authority import event_authority_for_query


def make(subject_uuid="subj-a", predicate="acquired", object_key="obj-x",
         domain=None, namespace=None, valid_at="2026-01-05T00:00:00Z",
         learned_at="2026-01-06T00:00:00Z", episode_uuid="ep-1",
         turn_evidence_uuid="ev-1", stated_span="an exact stated span", **kw) -> TypedEventAssertion:
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
        stated_span=stated_span,
        episode_uuid=episode_uuid,
        turn_evidence_uuid=turn_evidence_uuid,
        span_start=10,
        span_end=30,
        claim_ordinal=0,
    )
    base.update(kw)
    return TypedEventAssertion(**base)


def two_latest() -> list[TypedEventAssertion]:
    return [
        make(valid_at="2026-01-05T00:00:00Z", object_key="notebook-a",
             span_start=10, span_end=20),
        make(valid_at="2026-02-05T00:00:00Z", object_key="notebook-b",
             span_start=40, span_end=50),
    ]


# --------------------------------------------------------------------------- leading latest

@pytest.mark.unit
def test_leading_latest() -> None:
    verdict = event_authority_for_query(
        "which notebook did I buy most recently?", two_latest(),
        subject_uuid="subj-a", foundation_verified=True)
    assert verdict is not None
    assert verdict.status == "leads"
    assert verdict.gate == "pass"
    assert verdict.has_foundation is True
    assert verdict.object_key == "notebook-b"
    assert verdict.valid_at == "2026-02-05T00:00:00Z"
    assert verdict.stated_span
    assert verdict.assertion_key
    assert verdict.predicate == "acquired"
    assert verdict.subject_uuid == "subj-a"
    assert verdict.kind == "latest"


# --------------------------------------------------------------------------- predecessor

@pytest.mark.unit
def test_predecessor_leads() -> None:
    assertions = [
        make(valid_at="2026-01-05T00:00:00Z", object_key="kettle", span_start=10, span_end=20),
        make(valid_at="2026-02-05T00:00:00Z", object_key="spatula", span_start=40, span_end=50),
        make(valid_at="2026-03-05T00:00:00Z", object_key="blender", span_start=60, span_end=70),
    ]
    verdict = event_authority_for_query(
        "what did I buy before getting the blender?", assertions,
        subject_uuid="subj-a", foundation_verified=True)
    assert verdict is not None
    assert verdict.status == "leads"
    assert verdict.gate == "pass"
    assert verdict.object_key == "spatula"
    assert verdict.valid_at == "2026-02-05T00:00:00Z"
    assert verdict.kind == "predecessor"


# --------------------------------------------------------------------------- generic advisory

@pytest.mark.unit
def test_generic_advisory() -> None:
    assertions = [
        make(valid_at="2026-01-05T00:00:00Z", object_key="thing-a", span_start=10, span_end=20),
        make(valid_at="2026-02-05T00:00:00Z", object_key="thing-b", span_start=40, span_end=50),
    ]
    verdict = event_authority_for_query(
        "which item did I buy most recently?", assertions,
        subject_uuid="subj-a", foundation_verified=True)
    assert verdict is not None
    assert verdict.status == "advisory"
    assert verdict.gate == "route"
    assert verdict.object_key == "thing-b"
    assert verdict.kind == "generic_advisory"


# --------------------------------------------------------------------------- missing foundation

@pytest.mark.unit
def test_missing_foundation_advisory() -> None:
    verdict = event_authority_for_query(
        "which notebook did I buy most recently?", two_latest(),
        subject_uuid="subj-a", foundation_verified=False)
    assert verdict is not None
    assert verdict.status == "advisory"
    assert verdict.gate == "foundation"
    assert verdict.has_foundation is False
    assert verdict.object_key == "notebook-b"


# --------------------------------------------------------------------------- ambiguity / no selection

@pytest.mark.unit
def test_ambiguity_no_selection_advisory() -> None:
    tied = [
        make(valid_at="2026-02-05T00:00:00Z", object_key="notebook-a", span_start=10, span_end=20),
        make(valid_at="2026-02-05T00:00:00Z", object_key="notebook-b", span_start=40, span_end=50),
    ]
    verdict = event_authority_for_query(
        "which notebook did I buy most recently?", tied,
        subject_uuid="subj-a", foundation_verified=True)
    assert verdict is not None
    assert verdict.status == "advisory"
    assert verdict.gate == "ambiguity"
    assert verdict.has_foundation is False
    assert verdict.object_key is None
    assert verdict.assertion_key is None


@pytest.mark.unit
def test_no_candidate_advisory() -> None:
    verdict = event_authority_for_query(
        "which notebook did I buy most recently?", [make(object_key="kettle")],
        subject_uuid="subj-a", foundation_verified=True)
    assert verdict is not None
    assert verdict.status == "advisory"
    assert verdict.gate == "scope"
    assert verdict.has_foundation is False
    assert verdict.object_key is None


# --------------------------------------------------------------------------- broader explicit-anchor guard

@pytest.mark.unit
def test_explicit_acquisition_anchor_guard_fails_closed_when_anchor_is_absent() -> None:
    verdict = event_authority_for_query(
        "Before I purchased the touring bike, do I own any other bikes?",
        [make(object_key="road bike")],
        subject_uuid="subj-a", foundation_verified=True,
    )

    assert verdict is not None
    assert verdict.status == "advisory"
    assert verdict.gate == "anchor"
    assert verdict.kind == "anchor_guard"
    assert verdict.object_key is None
    assert "touring bike" in verdict.reason


@pytest.mark.unit
def test_explicit_acquisition_anchor_guard_is_silent_when_anchor_is_unique() -> None:
    verdict = event_authority_for_query(
        "Before I bought the touring bike, do I own any other bikes?",
        [make(object_key="touring bike")],
        subject_uuid="subj-a", foundation_verified=True,
    )

    assert verdict is None


@pytest.mark.unit
def test_explicit_anchor_guard_rejects_future_and_third_party_surfaces() -> None:
    assertions = [make(object_key="touring bike")]
    assert event_authority_for_query(
        "Before I buy the touring bike, should I sell another bike?", assertions,
        subject_uuid="subj-a", foundation_verified=True,
    ) is None
    assert event_authority_for_query(
        "Before she purchased the touring bike, did she own another bike?", assertions,
        subject_uuid="subj-a", foundation_verified=True,
    ) is None


# --------------------------------------------------------------------------- abstention

@pytest.mark.unit
def test_third_party_abstention() -> None:
    verdict = event_authority_for_query(
        "which notebook did you buy most recently?", two_latest(),
        subject_uuid="subj-a", foundation_verified=True)
    assert verdict is None


@pytest.mark.unit
def test_malformed_query_abstention() -> None:
    verdict = event_authority_for_query(
        "which notebook did I buy latst?", two_latest(),
        subject_uuid="subj-a", foundation_verified=True)
    assert verdict is None


@pytest.mark.unit
def test_friend_quote_not_first_person_abstention() -> None:
    verdict = event_authority_for_query(
        "Which notebook did my friend ask 'did I buy' most recently?", two_latest(),
        subject_uuid="subj-a", foundation_verified=True)
    assert verdict is None
