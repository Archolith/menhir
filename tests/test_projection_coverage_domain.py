from __future__ import annotations

import random

import pytest

from menhir.domain.projection_coverage import (
    AssertionLifecycle,
    AuditFailureClassification,
    BindingStatus,
    EligibilityDecision,
    EligibilityRole,
    FoldRole,
    ParityViolationKind,
    ProjectionStatus,
    build_projection_coverage_report,
    classify_lifecycle,
    enrich_fold,
)
from menhir.domain.scalar_state_fold import fold_assertions
from menhir.domain.typed_assertion import normalize_scalar

pytestmark = pytest.mark.unit

NS = "tenant-a"
SUBJECT = "entity-1"


def _row(
    assertion_id: str,
    *,
    operation: str = "absolute",
    value=10,
    valid_at: str = "2026-01-01T00:00:00+00:00",
    attribute: str = "score",
    scope: str = "",
    value_kind: str = "count",
    unit: str = "",
    superseded: bool = False,
    binding_pending: bool = False,
    projection_pending: bool = False,
    namespace: str | None = NS,
    evidence_tier: str = "user",
    episode_uuid: str | None = None,
) -> dict:
    return {
        "assertion_id": assertion_id,
        "assertion_key": f"ak-{assertion_id}",
        "source_key": f"sk-{assertion_id}",
        "subject_uuid": SUBJECT,
        "subject_display": "Entity One",
        "attribute": attribute,
        "scope": scope,
        "value_kind": value_kind,
        "unit": unit,
        "operation": operation,
        "value": value,
        "valid_at": valid_at,
        "learned_at": valid_at,
        "episode_uuid": episode_uuid or f"ep-{assertion_id}",
        "evidence_tier": evidence_tier,
        "perceiver_version": "v1",
        "absolute_semantics": "ordinary",
        "superseded": superseded,
        "binding_pending": binding_pending,
        "projection_pending": projection_pending,
        "namespace": namespace,
        "head_subject_uuid": SUBJECT,
        "head_current_subject_uuid": SUBJECT,
        "head_current_assertion_id": assertion_id if not superseded else "",
    }


def _view_for_state(state, *, namespace: str | None = NS, **overrides) -> dict:
    row = {
        "uuid": f"view-{state.attribute}-{state.scope}",
        "view_key": f"vk-{state.attribute}-{state.scope}",
        "subject_uuid": state.subject_uuid,
        "namespace": namespace,
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
    row.update(overrides)
    return row


def _report(rows: list[dict], views: list[dict] | None = None, *, eligibility_source=None):
    materializable = [
        row
        for row in rows
        if not row.get("superseded")
        and not row.get("binding_pending")
        and (
            eligibility_source is None
            or eligibility_source.eligibility_for(row).role is EligibilityRole.MATERIALIZABLE
        )
    ]
    fold = fold_assertions(materializable)
    return build_projection_coverage_report(
        subject_uuid=SUBJECT,
        namespace=NS,
        assertions=rows,
        fold=fold,
        actual_views=list(views or []),
        eligibility_source=eligibility_source,
    )


def test_same_version_noncurrent_is_superseded_without_superseded_by() -> None:
    row = _row("old", superseded=True)
    row.pop("superseded_by", None)
    assert classify_lifecycle(row) is AssertionLifecycle.SUPERSEDED
    report = _report([row])
    assert report.accounting[0].eligibility_role is None
    assert report.accounting[0].fold_role is None
    assert report.accounting[0].projection_status is ProjectionStatus.NOT_REQUIRED


def test_binding_pending_unbound_sentinel_is_advisory() -> None:
    row = _row("pending", binding_pending=True)
    row["subject_uuid"] = "unbound:source-1"
    report = build_projection_coverage_report(
        subject_uuid=row["subject_uuid"],
        namespace=NS,
        assertions=[row],
        fold=fold_assertions([]),
        actual_views=[],
    )
    record = report.accounting[0]
    assert record.binding_status is BindingStatus.BINDING_PENDING
    assert record.eligibility_role is EligibilityRole.BINDING_ADVISORY
    assert record.fold_role is None


def test_view_corruption_cannot_change_fold_roles() -> None:
    anchor = _row("anchor")
    old = _row("old", valid_at="2025-01-01T00:00:00+00:00")
    fold = fold_assertions([anchor, old])
    enriched = enrich_fold([anchor, old], fold)
    assert enriched.role_for("anchor") is FoldRole.CONTRIBUTOR
    assert enriched.role_for("old") is FoldRole.NON_CONTRIBUTING_MEMBER

    corrupt = _view_for_state(fold.states[0], scalar_contributors=["old"])
    report = build_projection_coverage_report(
        subject_uuid=SUBJECT,
        namespace=NS,
        assertions=[anchor, old],
        fold=fold,
        actual_views=[corrupt],
    )
    roles = {record.assertion_id: record.fold_role for record in report.accounting}
    assert roles == {
        "anchor": FoldRole.CONTRIBUTOR,
        "old": FoldRole.NON_CONTRIBUTING_MEMBER,
    }
    assert ParityViolationKind.CONTRIBUTOR_SET_MISMATCH in {
        violation.kind for violation in report.parity_violations
    }


@pytest.mark.parametrize(
    "rows",
    [
        [_row("delta", operation="delta", value=1)],
        [_row("a", value=1), _row("b", value=2)],
        [
            _row("range", value=[1, 2], value_kind="measurement"),
            _row(
                "delta",
                operation="delta",
                value=1,
                value_kind="measurement",
                valid_at="2026-01-02T00:00:00+00:00",
            ),
        ],
    ],
)
def test_abstention_slots_assign_abstention_roles(rows: list[dict]) -> None:
    fold = fold_assertions(rows)
    assert len(fold.abstentions) == 1
    enriched = enrich_fold(rows, fold)
    assert {role for _, role in enriched.roles} == {FoldRole.SLOT_ABSTENTION_MEMBER}


def test_view_over_abstained_slot_emits_invalid_combination_and_orphan() -> None:
    row = _row("delta", operation="delta", value=1)
    view = {
        "uuid": "orphan",
        "view_key": "vk-orphan",
        "subject_uuid": SUBJECT,
        "namespace": NS,
        "attribute": "score",
        "scope": "",
        "value_kind": "count",
        "unit": "",
        "view_value": "1",
        "valid_at": row["valid_at"],
        "scalar_contributors": ["delta"],
        "scalar_effective_tier": "user",
        "episode_uuids": ["ep-delta"],
    }
    report = _report([row], [view])
    assert AuditFailureClassification.INVALID_COMBINATION in {
        violation.classification for violation in report.coverage_violations
    }
    assert ParityViolationKind.ORPHANED_VIEW in {
        violation.kind for violation in report.parity_violations
    }


def test_missing_view() -> None:
    row = _row("a")
    report = _report([row])
    assert [v.kind for v in report.parity_violations] == [ParityViolationKind.MISSING_VIEW]
    assert report.accounting[0].projection_status is ProjectionStatus.PROJECTION_ERROR


def test_orphaned_view() -> None:
    report = _report(
        [],
        [
            {
                "uuid": "orphan",
                "view_key": "vk-orphan",
                "subject_uuid": SUBJECT,
                "namespace": NS,
                "attribute": "score",
                "scope": "",
                "value_kind": "count",
                "unit": "",
                "view_value": "10",
            }
        ],
    )
    assert [v.kind for v in report.parity_violations] == [ParityViolationKind.ORPHANED_VIEW]


def test_duplicate_current_views_are_write_path_corruption() -> None:
    row = _row("a")
    fold = fold_assertions([row])
    view = _view_for_state(fold.states[0])
    report = _report([row], [view, {**view, "uuid": "duplicate", "view_key": "vk-duplicate"}])
    assert AuditFailureClassification.CORRUPT_OR_BYPASSED_WRITE_PATH in {
        violation.classification for violation in report.coverage_violations
    }


@pytest.mark.parametrize(
    ("overrides", "kind"),
    [
        ({"view_value": "999"}, ParityViolationKind.VALUE_MISMATCH),
        ({"scalar_contributors": ["not-the-anchor"]}, ParityViolationKind.CONTRIBUTOR_SET_MISMATCH),
        ({"scalar_effective_tier": "agent"}, ParityViolationKind.TIER_MISMATCH),
        ({"episode_uuids": ["wrong-episode"]}, ParityViolationKind.PROVENANCE_MISMATCH),
    ],
)
def test_strict_parity_fields(overrides: dict, kind: ParityViolationKind) -> None:
    row = _row("a")
    fold = fold_assertions([row])
    report = _report([row], [_view_for_state(fold.states[0], **overrides)])
    assert kind in {violation.kind for violation in report.parity_violations}


def test_valid_at_parity_is_timestamp_normalized() -> None:
    row = _row("a", valid_at="2026-01-01T00:00:00+00:00")
    fold = fold_assertions([row])
    equal = _view_for_state(fold.states[0], valid_at="2025-12-31T18:00:00-06:00")
    report = _report([row], [equal])
    assert ParityViolationKind.VALID_AT_MISMATCH not in {
        violation.kind for violation in report.parity_violations
    }


def test_namespace_mismatch_is_never_satisfied_by_foreign_view() -> None:
    row = _row("a")
    fold = fold_assertions([row])
    report = _report([row], [_view_for_state(fold.states[0], namespace="tenant-b")])
    assert AuditFailureClassification.NAMESPACE_MISMATCH in {
        violation.classification for violation in report.coverage_violations
    }
    assert ParityViolationKind.MISSING_VIEW in {
        violation.kind for violation in report.parity_violations
    }


def test_report_order_is_deterministic_under_shuffled_input() -> None:
    rows = [
        _row("b", attribute="zeta"),
        _row("a", attribute="alpha"),
        _row("c", attribute="middle"),
    ]
    fold = fold_assertions(rows)
    views = [_view_for_state(state) for state in fold.states]
    expected = _report(rows, views)

    shuffled_rows = list(rows)
    shuffled_views = list(views)
    random.Random(7).shuffle(shuffled_rows)
    random.Random(11).shuffle(shuffled_views)
    actual = _report(shuffled_rows, shuffled_views)

    assert actual.accounting == expected.accounting
    assert actual.coverage_violations == expected.coverage_violations
    assert actual.parity_violations == expected.parity_violations


class _Eligibility:
    def eligibility_for(self, assertion: dict) -> EligibilityDecision:
        role = {
            "retracted": EligibilityRole.RETRACTED,
            "vetoed": EligibilityRole.OPERATOR_VETOED,
        }.get(assertion["assertion_id"], EligibilityRole.MATERIALIZABLE)
        return EligibilityDecision(role)


def test_fake_retraction_and_veto_are_nonmaterializable() -> None:
    rows = [_row("retracted"), _row("vetoed", attribute="other")]
    report = _report(rows, eligibility_source=_Eligibility())
    roles = {record.assertion_id: record.eligibility_role for record in report.accounting}
    assert roles == {
        "retracted": EligibilityRole.RETRACTED,
        "vetoed": EligibilityRole.OPERATOR_VETOED,
    }
    assert all(record.fold_role is None for record in report.accounting)
    assert all(record.projection_status is ProjectionStatus.NOT_REQUIRED for record in report.accounting)
