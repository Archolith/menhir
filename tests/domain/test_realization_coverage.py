from __future__ import annotations

import pytest

from menhir.domain.projection import (
    ProjectionAbstention,
    ProjectionDefinition,
    ProjectionMaterialization,
    ProjectionRetirement,
    ProjectionTarget,
)
from menhir.domain.projection_lifecycle import ProjectionFreshnessAssessment
from menhir.domain.realization_coverage import (
    RealizationOutcomeKind,
    RealizationStatus,
    build_realization_coverage_report,
)


def _target(subject: str) -> ProjectionTarget:
    return ProjectionTarget(namespace="tenant-a", subject_id=subject, key=("state",))


def _definition(version: int = 2) -> ProjectionDefinition:
    return ProjectionDefinition(
        definition_id="example.current-state",
        version=version,
        input_assertion_types=frozenset({"example.assertion"}),
        output_view_kind="example_view",
        assertion_id_resolver=lambda assertion: str(assertion),
        assertion_type_resolver=lambda _assertion: "example.assertion",
        target_resolver=lambda _assertion: None,
        fold=lambda _target, _assertions, _as_of: None,  # type: ignore[arg-type]
    )


def _assessment(
    target: ProjectionTarget,
    *,
    state: str = "fresh",
    reason: str = "certified_current",
    definition_id: str = "example.current-state",
    version: int | None = 2,
    target_present: bool | None = True,
) -> ProjectionFreshnessAssessment:
    return ProjectionFreshnessAssessment(
        state=state,  # type: ignore[arg-type]
        reason=reason,
        definition_id=definition_id,
        target=target,
        current_definition_version=version,
        target_present=target_present,
        work_generation=3 if version is not None else None,
        applied_generation=3 if version is not None else None,
        certified_definition_version=version,
        certified_generation=3 if version is not None else None,
        certified_projection_hash="hash-3" if version is not None else None,
        current_projection_hash="hash-3" if version is not None else None,
        derivation_id="derive-3" if version is not None else None,
    )


@pytest.mark.unit
def test_all_desired_outcome_kinds_can_be_realized_by_existing_freshness_proof() -> None:
    materialized = _target("a")
    abstained = _target("b")
    retired = _target("c")
    outcomes = [
        ProjectionRetirement(target=retired, reason="expired"),
        ProjectionMaterialization(
            target=materialized,
            view_kind="example_view",
            payload={"value": 1},
            contributor_ids=("assertion-1",),
        ),
        ProjectionAbstention(target=abstained, reason="conflict"),
    ]

    report = build_realization_coverage_report(
        definition=_definition(),
        outcomes=outcomes,
        freshness=[
            _assessment(retired),
            _assessment(materialized),
            _assessment(abstained),
        ],
    )

    assert report.clean is True
    assert [record.target.subject_id for record in report.records] == ["a", "b", "c"]
    assert [record.outcome_kind for record in report.records] == [
        RealizationOutcomeKind.MATERIALIZATION,
        RealizationOutcomeKind.ABSTENTION,
        RealizationOutcomeKind.RETIREMENT,
    ]
    assert all(record.status is RealizationStatus.REALIZED for record in report.records)


@pytest.mark.unit
def test_missing_stale_and_unavailable_proofs_are_not_realized() -> None:
    missing = _target("a")
    stale = _target("b")
    unavailable = _target("c")
    outcomes = [
        ProjectionRetirement(target=missing, reason="expired"),
        ProjectionRetirement(target=stale, reason="expired"),
        ProjectionRetirement(target=unavailable, reason="expired"),
    ]

    report = build_realization_coverage_report(
        definition=_definition(),
        outcomes=outcomes,
        freshness=[
            _assessment(stale, state="stale", reason="pending_generation"),
            _assessment(
                unavailable,
                state="unavailable",
                reason="projection_state_unavailable",
            ),
        ],
    )

    by_subject = {record.target.subject_id: record for record in report.records}
    assert report.clean is False
    assert by_subject["a"].status is RealizationStatus.UNAVAILABLE
    assert by_subject["a"].reason == "freshness_assessment_missing"
    assert by_subject["b"].status is RealizationStatus.STALE
    assert by_subject["b"].reason == "pending_generation"
    assert by_subject["c"].status is RealizationStatus.UNAVAILABLE


@pytest.mark.unit
def test_wrong_definition_or_absent_desired_target_is_invalid_proof() -> None:
    wrong_definition = _target("a")
    wrong_version = _target("b")
    absent = _target("c")
    outcomes = [
        ProjectionRetirement(target=wrong_definition, reason="expired"),
        ProjectionRetirement(target=wrong_version, reason="expired"),
        ProjectionRetirement(target=absent, reason="expired"),
    ]

    report = build_realization_coverage_report(
        definition=_definition(),
        outcomes=outcomes,
        freshness=[
            _assessment(wrong_definition, definition_id="other.definition"),
            _assessment(wrong_version, version=1),
            _assessment(absent, target_present=False),
        ],
    )

    by_subject = {record.target.subject_id: record for record in report.records}
    assert by_subject["a"].status is RealizationStatus.INVALID_PROOF
    assert by_subject["a"].reason == "freshness_identity_mismatch"
    assert by_subject["b"].status is RealizationStatus.INVALID_PROOF
    assert by_subject["b"].reason == "definition_version_mismatch"
    assert by_subject["c"].status is RealizationStatus.INVALID_PROOF
    assert by_subject["c"].reason == "desired_target_not_present"


@pytest.mark.unit
def test_duplicate_desired_or_freshness_targets_fail_closed() -> None:
    target = _target("a")
    outcome = ProjectionRetirement(target=target, reason="expired")

    with pytest.raises(ValueError, match="duplicate desired"):
        build_realization_coverage_report(
            definition=_definition(),
            outcomes=[outcome, outcome],
            freshness=[_assessment(target)],
        )

    with pytest.raises(ValueError, match="duplicate freshness"):
        build_realization_coverage_report(
            definition=_definition(),
            outcomes=[outcome],
            freshness=[_assessment(target), _assessment(target)],
        )
