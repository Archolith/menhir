from __future__ import annotations

from datetime import datetime, timezone

import pytest

from menhir.domain.projection import (
    ProjectionAbstention,
    ProjectionMaterialization,
    ProjectionRetirement,
    ProjectionTarget,
    evaluate_projection,
)
from menhir.domain.scalar_state_fold import (
    AMBIGUOUS_ANCHOR,
    fold_assertions,
)
from menhir.services.scalar_projection_definition import SCALAR_STATE_PROJECTION


def _row(
    assertion_id: str,
    *,
    operation: str,
    value,
    valid_at: str,
    evidence_tier: str = "user",
    subject_uuid: str = "entity-1",
    attribute: str = "owned",
    scope: str = "",
    value_kind: str = "number",
    unit: str = "count",
    namespace: str | None = "default",
):
    return {
        "assertion_id": assertion_id,
        "subject_uuid": subject_uuid,
        "subject_display": "Alice",
        "attribute": attribute,
        "scope": scope,
        "value_kind": value_kind,
        "unit": unit,
        "operation": operation,
        "value": value,
        "valid_at": valid_at,
        "learned_at": valid_at,
        "evidence_tier": evidence_tier,
        "episode_uuid": f"episode-{assertion_id}",
        "absolute_semantics": "ordinary",
        "namespace": namespace,
    }


@pytest.mark.unit
def test_scalar_projection_materialization_matches_existing_fold_exactly():
    rows = [
        _row(
            "anchor",
            operation="absolute",
            value=10,
            valid_at="2026-01-01T00:00:00+00:00",
            evidence_tier="user",
        ),
        _row(
            "delta",
            operation="delta",
            value=2,
            valid_at="2026-01-02T00:00:00+00:00",
            evidence_tier="trusted_tool",
        ),
    ]

    legacy = fold_assertions(rows)
    outcomes = evaluate_projection(SCALAR_STATE_PROJECTION, list(reversed(rows)))

    assert len(legacy.states) == 1
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert isinstance(outcome, ProjectionMaterialization)
    assert outcome.payload == legacy.states[0]
    assert outcome.contributor_ids == tuple(legacy.states[0].contributor_ids)
    assert outcome.payload.value == 12
    assert outcome.payload.effective_tier == "trusted_tool"
    assert outcome.target == ProjectionTarget(
        subject_id="entity-1",
        namespace="default",
        key=("owned", "", "number", "count"),
    )


@pytest.mark.unit
def test_scalar_projection_preserves_ambiguous_anchor_abstention():
    rows = [
        _row(
            "a",
            operation="absolute",
            value=10,
            valid_at="2026-01-01T00:00:00+00:00",
        ),
        _row(
            "b",
            operation="absolute",
            value=11,
            valid_at="2026-01-01T00:00:00+00:00",
        ),
    ]

    legacy = fold_assertions(rows)
    outcome = evaluate_projection(SCALAR_STATE_PROJECTION, rows)[0]

    assert len(legacy.abstentions) == 1
    assert isinstance(outcome, ProjectionAbstention)
    assert outcome.reason == legacy.abstentions[0].reason == AMBIGUOUS_ANCHOR


@pytest.mark.unit
def test_scalar_projection_maps_expiry_to_retirement():
    rows = [
        _row(
            "anchor",
            operation="absolute",
            value=10,
            valid_at="2026-01-01T00:00:00+00:00",
        ),
        _row(
            "expiry",
            operation="expire",
            value=10,
            valid_at="2026-02-01T00:00:00+00:00",
        ),
    ]

    legacy = fold_assertions(rows)
    outcome = evaluate_projection(SCALAR_STATE_PROJECTION, rows)[0]

    assert len(legacy.expiries) == 1
    assert isinstance(outcome, ProjectionRetirement)
    assert outcome.reason == "expired"
    assert outcome.contributor_ids == tuple(legacy.expiries[0].contributor_ids)


@pytest.mark.unit
def test_future_only_scalar_target_is_explicitly_non_current():
    rows = [
        _row(
            "future",
            operation="absolute",
            value=10,
            valid_at="2026-02-01T00:00:00+00:00",
        )
    ]
    as_of = datetime(2026, 1, 1, tzinfo=timezone.utc)

    legacy = fold_assertions(rows, as_of=as_of)
    outcome = evaluate_projection(SCALAR_STATE_PROJECTION, rows, as_of=as_of)[0]

    assert legacy.states == []
    assert legacy.abstentions == []
    assert legacy.expiries == []
    assert isinstance(outcome, ProjectionRetirement)
    assert outcome.reason == "no_active_assertions"
    assert outcome.contributor_ids == ()


@pytest.mark.unit
def test_scalar_definition_accepts_all_existing_operation_families_without_core_vocabulary():
    assert SCALAR_STATE_PROJECTION.input_assertion_types == frozenset(
        {
            "typed_scalar.absolute",
            "typed_scalar.delta",
            "typed_scalar.expire",
        }
    )
    assert SCALAR_STATE_PROJECTION.output_view_kind == "scalar_state"
    assert SCALAR_STATE_PROJECTION.definition_id == "typed_scalar.current_state"
