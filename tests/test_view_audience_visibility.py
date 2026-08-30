"""Focused lifecycle classification and recall-visibility invariants for Views."""

from __future__ import annotations

import pytest

from menhir.domain.recall_visibility import (
    default_recall_visibility_cypher,
    view_live_provenance_cypher,
)
from menhir.infrastructure.cypher import ENTITY_METADATA_FIELDS
from menhir.infrastructure.view_models import (
    AdmissionAuditKind,
    CounterKind,
    ScalarHistoryKind,
    ScalarStateKind,
    TimelineKind,
    ViewAudience,
    ViewClass,
)


@pytest.mark.unit
def test_payload_aware_view_lifecycle_stamps() -> None:
    assert CounterKind().view_stamps({}) == {
        "view_class": "FACT",
        "view_subtype": "counter",
        "view_audience": "RECALL",
    }
    assert ScalarStateKind().view_stamps({}) == {
        "view_class": "FACT",
        "view_subtype": "scalar_state",
        "view_audience": "RECALL",
    }
    assert AdmissionAuditKind().view_stamps({}) == {
        "view_class": "FACT",
        "view_subtype": "admission_audit",
        "view_audience": "OPERATOR",
    }


@pytest.mark.unit
def test_timeline_audience_distinguishes_legacy_from_event_lane() -> None:
    kind = TimelineKind()

    assert kind.view_subtype({"entries": []}) == "legacy_timeline"
    assert kind.view_audience({"entries": []}) is ViewAudience.OPERATOR

    event_payload = {"entries": [], "predicate": "purchased"}
    assert kind.view_subtype(event_payload) == "event_timeline"
    assert kind.view_audience(event_payload) is ViewAudience.RECALL


@pytest.mark.unit
def test_scalar_history_requires_explicit_boolean_recallable_flag() -> None:
    kind = ScalarHistoryKind()

    assert kind.view_audience({}) is ViewAudience.OPERATOR
    assert kind.view_audience({"recallable": False}) is ViewAudience.OPERATOR
    assert kind.view_audience({"recallable": "true"}) is ViewAudience.OPERATOR
    assert kind.view_audience({"recallable": True}) is ViewAudience.RECALL


@pytest.mark.unit
def test_metric_kind_is_always_operator_audience() -> None:
    assert CounterKind().view_stamps({}, view_class=ViewClass.METRIC) == {
        "view_class": "METRIC",
        "view_subtype": "counter",
        "view_audience": "OPERATOR",
    }


@pytest.mark.unit
def test_admission_turn_evidence_is_lifecycle_provenance() -> None:
    kind = AdmissionAuditKind()

    assert kind.episode_uuids({"turn_evidence_uuid": " turn-1 "}) == ["turn-1"]
    assert kind.episode_uuids({"turn_evidence_uuid": None}) == []


@pytest.mark.unit
def test_live_provenance_requires_exact_incoming_mentions_set() -> None:
    predicate = view_live_provenance_cypher("v")

    assert "MATCH (e)-[:MENTIONS]->(v)" in predicate
    assert "e:Episodic AND e.uuid = eid" in predicate
    assert "e:TurnEvidence AND e.turn_id = eid" in predicate
    assert "COUNT { MATCH ()-[:MENTIONS]->(v) }" in predicate
    assert "single(other IN coalesce(v.episode_uuids, []) WHERE other = eid)" in predicate
    assert "size(coalesce(v.episode_uuids, [])) > 0" in predicate
    assert "e.uuid IN" not in predicate
    assert "e.turn_id IN" not in predicate


@pytest.mark.unit
def test_live_provenance_requires_same_canonical_tenant() -> None:
    predicate = view_live_provenance_cypher("v")
    evidence_tenant = (
        "CASE WHEN coalesce(e.namespace, e.group_id, '') = '' THEN 'default' "
        "ELSE coalesce(e.namespace, e.group_id, '') END"
    )
    view_tenant = (
        "CASE WHEN coalesce(v.namespace, v.group_id, '') = '' THEN 'default' "
        "ELSE coalesce(v.namespace, v.group_id, '') END"
    )

    assert f"AND {evidence_tenant} = {view_tenant}" in predicate
    assert "WHERE ((e:Episodic AND e.uuid = eid)" in predicate


@pytest.mark.unit
def test_default_recall_requires_explicit_fact_recall_audience() -> None:
    predicate = default_recall_visibility_cypher("n")

    assert "NOT coalesce(n.is_view, false) OR" in predicate
    assert "n.view_class = 'FACT'" in predicate
    assert "n.view_audience = 'RECALL'" in predicate
    assert "coalesce(n.view_current, n.qs_current, false)" in predicate
    assert "NOT coalesce(n.retired, false)" in predicate
    assert view_live_provenance_cypher("n") in predicate


@pytest.mark.unit
def test_entity_metadata_exposes_classification_and_same_provenance_rule() -> None:
    fields = "\n".join(ENTITY_METADATA_FIELDS)

    assert "n.view_class AS view_class" in fields
    assert "n.view_subtype AS view_subtype" in fields
    assert "n.view_audience AS view_audience" in fields
    assert "coalesce(n.retired, false) AS retired" in fields
    assert view_live_provenance_cypher("n") in fields
    assert "CASE WHEN NOT coalesce(n.is_view, false) THEN true" in fields
