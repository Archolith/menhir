"""Counting occurrence rows whose subject merely echoes the counted thing bind to the user (offline)."""

from __future__ import annotations

import pytest

from menhir.services.counting_consolidation import COUNTING_CLAIM_ORDINAL
from menhir.services.typed_scalar_perception import (
    SELF_SUBJECT_DISPLAY,
    TypedScalarDecision,
    TypedScalarProposal,
    advisory_subject_uuid,
    bind_and_persist_typed_scalars,
    repair_pending_bindings,
)
from menhir.services.typed_scalar_rules import counting_subject_echoes_object

_NOW = "2026-07-10T12:00:00+00:00"
_SELF = "self-uuid"


def _echo(subject, obj, item="", attribute="", family=""):
    return counting_subject_echoes_object(
        subject, count_object=obj, count_item=item, count_family=family, attribute=attribute)


@pytest.mark.unit
@pytest.mark.parametrize("subject,obj,item,attribute", [
    ("the restaurant", "restaurant", "new Ethiopian restaurant in town", "restaurant_visit_count"),
    ("the 3-bedroom bungalow in the Oakwood neighborhood", "bungalow",
     "3-bedroom bungalow in the Oakwood neighborhood", "property_view_count"),
    ("the 3-bedroom townhouse in the Brookside neighborhood", "townhouse",
     "3-bedroom townhouse in the Brookside neighborhood", "property_view_count"),
    ("the offer", "home offer", "offer on a 2-bedroom condo", "offer_rejection_count"),
    ("the lecture", "lecture", "The Evolution of Street Art", "lecture_attendance_count"),
    ("the exhibition", "exhibition", "Women in Art", "exhibition_attendance_count"),
])
def test_real_stuck_rows_echo(subject, obj, item, attribute):
    assert _echo(subject, obj, item, attribute) is True


@pytest.mark.unit
@pytest.mark.parametrize("subject,obj,item,attribute", [
    ("my sister", "baby", "", "baby_count"),
    ("my dog", "dog vet visit", "", "dog_vet_visit_count"),
    ("David's baby", "baby", "", "baby_count"),
    ("user", "museum", "", "museum_visit_count"),
    ("Celeste", "video game", "", "video_game_count"),
])
def test_non_echo_subjects(subject, obj, item, attribute):
    assert _echo(subject, obj, item, attribute) is False


def _prop(**over) -> TypedScalarProposal:
    base = dict(
        subject_text="the museum", attribute="museum_visit_count", scope="", value_kind="count",
        unit="", operation="occurrence", value=1, stated_span="I visited the museum",
        episode_uuid="ep-1", span_start=0, span_end=20, when=_NOW,
        claim_ordinal=COUNTING_CLAIM_ORDINAL, count_object="museum", importance="notable",
    )
    base.update(over)
    return TypedScalarProposal(**base)


def _decision(p):
    return TypedScalarDecision(
        source_key=p.source_key, committed=True, reason="unanimous", veto="commit",
        agreement=1.0, k=1, distribution={}, proposal=p)


def _sink(binding_mismatch=False):
    recorded = []

    def record(a):
        recorded.append(a)
        return {"assertion_id": f"a{len(recorded)}",
                "binding_pending": a.subject_uuid.startswith("unbound:"),
                "binding_mismatch": binding_mismatch, "created": True}

    record.recorded = recorded
    return record


def _rebuild(u, ns=None):
    return {"subject_uuid": u, "written": 1}


def _seam(ns):
    return (_SELF, SELF_SUBJECT_DISPLAY) if ns else None


def _run(p, entities, seam=_seam, record=None):
    record = record or _sink()
    out = bind_and_persist_typed_scalars(
        [_decision(p)], linked_entities_for_episode=lambda e: entities, record_assertion=record,
        rebuild_scalar_state=_rebuild, now=lambda: _NOW, namespace="ns", resolve_self_subject=seam)
    return out, record


@pytest.mark.unit
def test_echo_binds_self_even_with_matching_entity():
    out, rec = _run(_prop(), [{"uuid": "ent-museum", "name": "museum"}])
    assert out["bound"] == 1
    assert rec.recorded[0].subject_uuid == _SELF
    assert rec.recorded[0].subject_display == SELF_SUBJECT_DISPLAY


@pytest.mark.unit
def test_third_party_unchanged():
    p = _prop(subject_text="my sister", attribute="baby_count", count_object="baby")
    out, rec = _run(p, [])
    assert out["advisory"] == 1 and rec.recorded[0].subject_uuid == advisory_subject_uuid(p.source_key)
    out, rec = _run(p, [{"uuid": "ent-sister", "name": "my sister"}])
    assert out["bound"] == 1 and rec.recorded[0].subject_uuid == "ent-sister"


@pytest.mark.unit
def test_scalar_row_unchanged():
    p = _prop(subject_text="the house", attribute="house_count", operation="absolute", value=3,
              claim_ordinal=0, count_object="", importance="")
    out, rec = _run(p, [])
    assert out["advisory"] == 1 and rec.recorded[0].subject_uuid == advisory_subject_uuid(p.source_key)
    out, rec = _run(p, [{"uuid": "ent-house", "name": "the house"}])
    assert out["bound"] == 1 and rec.recorded[0].subject_uuid == "ent-house"


@pytest.mark.unit
def test_repair_binds_pending_echo_row_to_self():
    rec = _sink()
    row = dict(
        assertion_id="a-1", source_key="sk-1", subject_uuid="unbound:sk-1",
        subject_display="the restaurant", binding_pending=True, projection_pending=False,
        attribute="restaurant_visit_count", scope="", value_kind="count", unit="",
        operation="occurrence", value=1, stated_span="I tried the restaurant", episode_uuid="ep-1",
        span_start=0, span_end=10, claim_ordinal=COUNTING_CLAIM_ORDINAL, valid_at=_NOW,
        learned_at=_NOW, time_basis="explicit", perceiver_version="v1", namespace="ns",
        importance="notable", count_object="restaurant", count_item="new Ethiopian restaurant in town", count_family="")
    out = repair_pending_bindings(
        [row], linked_entities_for_episode=lambda e: [{"uuid": "ent-r", "name": "the restaurant"}],
        record_assertion=rec, rebuild_scalar_state=_rebuild, resolve_self_subject=_seam)
    assert out["repaired"] == 1
    assert rec.recorded[0].subject_uuid == _SELF


@pytest.mark.unit
def test_seam_unavailable_fails_closed():
    out, rec = _run(_prop(), [{"uuid": "ent-museum", "name": "museum"}], seam=lambda ns: None)
    assert out["bound"] == 0 and out["advisory"] == 1
    assert rec.recorded[0].subject_uuid.startswith("unbound:")
    out, rec = _run(_prop(), [{"uuid": "ent-museum", "name": "museum"}], seam=None)
    assert out["bound"] == 0 and rec.recorded[0].subject_uuid.startswith("unbound:")


@pytest.mark.unit
def test_already_bound_elsewhere_still_refused():
    out, rec = _run(_prop(), [], record=_sink(binding_mismatch=True))
    assert out["mismatched"] == 1 and out["bound"] == 0
