from __future__ import annotations

from collections import Counter

import pytest

from menhir.domain.projection_coverage import (
    EligibilityDecision,
    EligibilityRole,
    ParityViolationKind,
    ProjectionStatus,
)
from menhir.domain.scalar_state_fold import fold_assertions
from menhir.domain.typed_assertion import normalize_scalar
from menhir.services.projection_coverage_service import ProjectionCoverageService

pytestmark = pytest.mark.unit

NS = "tenant-a"
SUBJECT = "entity-1"


def _row(
    assertion_id: str,
    *,
    subject_uuid: str = SUBJECT,
    attribute: str = "score",
    value=10,
    superseded: bool = False,
    binding_pending: bool = False,
    projection_pending: bool = False,
) -> dict:
    return {
        "assertion_id": assertion_id,
        "assertion_key": f"ak-{assertion_id}",
        "source_key": f"sk-{assertion_id}",
        "subject_uuid": subject_uuid,
        "subject_display": "Entity One",
        "namespace": NS,
        "attribute": attribute,
        "scope": "",
        "value_kind": "count",
        "unit": "",
        "operation": "absolute",
        "value": value,
        "valid_at": "2026-01-01T00:00:00+00:00",
        "learned_at": "2026-01-01T00:00:00+00:00",
        "episode_uuid": f"ep-{assertion_id}",
        "evidence_tier": "user",
        "perceiver_version": "v1",
        "absolute_semantics": "ordinary",
        "superseded": superseded,
        "binding_pending": binding_pending,
        "projection_pending": projection_pending,
        "head_subject_uuid": subject_uuid,
        "head_current_subject_uuid": subject_uuid,
        "head_current_assertion_id": assertion_id if not superseded else "current",
    }


def _matching_view(rows: list[dict]) -> dict:
    state = fold_assertions(rows).states[0]
    return {
        "uuid": "view-1",
        "view_key": "vk-1",
        "subject_uuid": state.subject_uuid,
        "namespace": NS,
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


class FakeSource:
    def __init__(
        self,
        assertions: list[dict],
        views: list[dict],
        *,
        fail_subject: str | None = None,
    ) -> None:
        self.assertions = assertions
        self.views = views
        self.fail_subject = fail_subject
        self.calls: list[tuple[str, str, str | None]] = []

    def assertions_for_projection_audit(
        self,
        subject_uuid: str,
        *,
        namespace: str | None = None,
    ) -> list[dict]:
        self.calls.append(("assertions", subject_uuid, namespace))
        if subject_uuid == self.fail_subject:
            raise RuntimeError("audit source unavailable")
        return [
            dict(row)
            for row in self.assertions
            if row["subject_uuid"] == subject_uuid
            and (namespace is None or row["namespace"] == namespace)
        ]

    def list_scalar_state_views_for_audit(
        self,
        *,
        subject_uuid: str,
        namespace: str | None = None,
    ) -> list[dict]:
        self.calls.append(("views", subject_uuid, namespace))
        return [
            dict(row)
            for row in self.views
            if row["subject_uuid"] == subject_uuid
            and (namespace is None or row["namespace"] == namespace)
        ]


def test_service_uses_assertions_not_views_to_choose_fold_members() -> None:
    current = _row("current")
    superseded = _row("old", superseded=True)
    pending = _row("pending", attribute="other", binding_pending=True)
    source = FakeSource(
        [current, superseded, pending],
        [_matching_view([current])],
    )

    report = ProjectionCoverageService(source).audit_entity(SUBJECT, namespace=NS)

    assert report.clean is True
    roles = {record.assertion_id: record.fold_role for record in report.accounting}
    assert roles["current"] is not None
    assert roles["old"] is None
    assert roles["pending"] is None
    assert source.calls == [
        ("assertions", SUBJECT, NS),
        ("views", SUBJECT, NS),
    ]


def test_projection_pending_remains_distinct_from_matching_view_parity() -> None:
    current = _row("current", projection_pending=True)
    source = FakeSource([current], [_matching_view([current])])

    report = ProjectionCoverageService(source).audit_entity(SUBJECT, namespace=NS)

    assert report.clean is True
    assert report.accounting[0].projection_status is ProjectionStatus.PROJECTION_PENDING


class CountingEligibility:
    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()

    def eligibility_for(self, assertion: dict) -> EligibilityDecision:
        assertion_id = assertion["assertion_id"]
        self.calls[assertion_id] += 1
        role = (
            EligibilityRole.RETRACTED
            if assertion_id == "retracted"
            else EligibilityRole.MATERIALIZABLE
        )
        return EligibilityDecision(role)


def test_custom_eligibility_is_evaluated_once_and_shared_with_report() -> None:
    current = _row("current")
    retracted = _row("retracted", attribute="other")
    eligibility = CountingEligibility()
    source = FakeSource([current, retracted], [_matching_view([current])])

    report = ProjectionCoverageService(source).audit_entity(
        SUBJECT,
        namespace=NS,
        eligibility_source=eligibility,
    )

    assert report.clean is True
    assert eligibility.calls == Counter({"current": 1, "retracted": 1})
    records = {record.assertion_id: record for record in report.accounting}
    assert records["retracted"].eligibility_role is EligibilityRole.RETRACTED
    assert records["retracted"].projection_status is ProjectionStatus.NOT_REQUIRED


def test_missing_view_is_reported_without_attempting_repair() -> None:
    current = _row("current")
    source = FakeSource([current], [])

    report = ProjectionCoverageService(source).audit_entity(SUBJECT, namespace=NS)

    assert [violation.kind for violation in report.parity_violations] == [
        ParityViolationKind.MISSING_VIEW
    ]
    assert report.accounting[0].projection_status is ProjectionStatus.PROJECTION_ERROR
    assert source.calls == [
        ("assertions", SUBJECT, NS),
        ("views", SUBJECT, NS),
    ]


def test_batch_is_bounded_and_preserves_entity_failures() -> None:
    good = _row("good")
    source = FakeSource([good], [_matching_view([good])], fail_subject="broken")
    service = ProjectionCoverageService(source)

    batch = service.audit_entities([(SUBJECT, NS), ("broken", NS)])

    assert len(batch.reports) == 1
    assert len(batch.failures) == 1
    assert batch.failures[0].subject_uuid == "broken"
    assert batch.failures[0].error_type == "RuntimeError"
    assert batch.clean is False

    with pytest.raises(ValueError, match="exceeds 200"):
        service.audit_entities([("entity", NS)] * 201)
