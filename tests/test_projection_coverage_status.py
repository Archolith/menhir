from __future__ import annotations

import pytest

from menhir.domain.projection_coverage import (
    AuditFailureClassification,
    ProjectionStatus,
    build_projection_coverage_report,
)
from menhir.domain.scalar_state_fold import fold_assertions
from menhir.domain.typed_assertion import normalize_scalar

pytestmark = pytest.mark.unit


def test_duplicate_current_views_mark_materializable_assertion_as_projection_error() -> None:
    row = {
        "assertion_id": "a",
        "assertion_key": "ak-a",
        "source_key": "sk-a",
        "subject_uuid": "entity-1",
        "subject_display": "Entity One",
        "attribute": "score",
        "scope": "",
        "value_kind": "count",
        "unit": "",
        "operation": "absolute",
        "value": 10,
        "valid_at": "2026-01-01T00:00:00+00:00",
        "learned_at": "2026-01-01T00:00:00+00:00",
        "episode_uuid": "ep-a",
        "evidence_tier": "user",
        "perceiver_version": "v1",
        "absolute_semantics": "ordinary",
        "superseded": False,
        "binding_pending": False,
        "projection_pending": False,
        "namespace": "tenant-a",
        "head_subject_uuid": "entity-1",
        "head_current_subject_uuid": "entity-1",
        "head_current_assertion_id": "a",
    }
    fold = fold_assertions([row])
    state = fold.states[0]
    view = {
        "uuid": "view-a",
        "view_key": "vk-a",
        "subject_uuid": state.subject_uuid,
        "namespace": "tenant-a",
        "attribute": state.attribute,
        "scope": state.scope,
        "value_kind": state.value_kind,
        "unit": state.unit,
        "view_value": normalize_scalar(state.value),
        "valid_at": state.valid_at,
        "scalar_contributors": list(state.contributor_ids),
        "scalar_effective_tier": state.effective_tier,
        "episode_uuids": list(state.episode_uuids),
        "view_current": True,
    }

    report = build_projection_coverage_report(
        subject_uuid="entity-1",
        namespace="tenant-a",
        assertions=[row],
        fold=fold,
        actual_views=[view, {**view, "uuid": "view-b", "view_key": "vk-b"}],
    )

    assert report.accounting[0].projection_status is ProjectionStatus.PROJECTION_ERROR
    assert AuditFailureClassification.CORRUPT_OR_BYPASSED_WRITE_PATH in {
        violation.classification for violation in report.coverage_violations
    }
