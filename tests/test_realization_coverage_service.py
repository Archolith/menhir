from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from menhir.domain.projection import (
    ProjectionDefinition,
    ProjectionMaterialization,
    ProjectionTarget,
)
from menhir.domain.projection_lifecycle import (
    ProjectionFreshnessAssessment,
    ProjectionLifecycleCorruptionError,
    ProjectionWorkToken,
)
from menhir.domain.realization_coverage import RealizationOutcomeKind, RealizationStatus
from menhir.services.realization_coverage_service import RealizationCoverageService


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


def _token(
    target: ProjectionTarget,
    *,
    version: int = 2,
    present: bool = True,
) -> ProjectionWorkToken:
    return ProjectionWorkToken(
        work_key=f"work:{target.subject_id}",
        definition_id="example.current-state",
        definition_version=version,
        target=target,
        generation=3,
        target_present=present,
        reason="test",
    )


def _assessment(
    target: ProjectionTarget,
    *,
    present: bool,
    state: str = "fresh",
    reason: str = "certified_current",
) -> ProjectionFreshnessAssessment:
    return ProjectionFreshnessAssessment(
        state=state,  # type: ignore[arg-type]
        reason=reason,
        definition_id="example.current-state",
        target=target,
        current_definition_version=2,
        target_present=present,
        work_generation=3,
        applied_generation=3,
        certified_definition_version=2,
        certified_generation=3,
        certified_projection_hash=f"hash:{target.subject_id}:{present}",
        current_projection_hash=f"hash:{target.subject_id}:{present}",
        derivation_id=f"derive:{target.subject_id}",
    )


@dataclass
class FakeLifecycle:
    tokens: tuple[ProjectionWorkToken, ...]
    assessments: dict[ProjectionTarget, ProjectionFreshnessAssessment]
    calls: list[tuple[ProjectionTarget, str | None]] = field(default_factory=list)

    def targets_for_definition(self, definition_id: str):
        assert definition_id == "example.current-state"
        return self.tokens

    def assess_freshness(self, *, definition_id, target, current_projection_hash):
        assert definition_id == "example.current-state"
        self.calls.append((target, current_projection_hash))
        return self.assessments[target]


@dataclass
class FakeHashes:
    calls: list[tuple[ProjectionTarget, bool]] = field(default_factory=list)

    def current_projection_hash(self, *, definition, target, target_present):
        assert definition.definition_id == "example.current-state"
        self.calls.append((target, target_present))
        return f"hash:{target.subject_id}:{target_present}"


@pytest.mark.unit
def test_service_audits_union_of_desired_and_removed_lifecycle_targets() -> None:
    desired = _target("desired")
    removed = _target("removed")
    lifecycle = FakeLifecycle(
        tokens=(_token(desired), _token(removed, present=False)),
        assessments={
            desired: _assessment(desired, present=True),
            removed: _assessment(removed, present=False),
        },
    )
    hashes = FakeHashes()
    outcome = ProjectionMaterialization(
        target=desired,
        view_kind="example_view",
        payload={"value": 1},
        contributor_ids=("a-1",),
    )

    report = RealizationCoverageService(lifecycle, hashes).audit_definition(
        _definition(),
        [outcome],
    )

    assert report.clean is True
    by_subject = {record.target.subject_id: record for record in report.records}
    assert by_subject["desired"].outcome_kind is RealizationOutcomeKind.MATERIALIZATION
    assert by_subject["removed"].outcome_kind is RealizationOutcomeKind.REMOVAL
    assert by_subject["removed"].status is RealizationStatus.REALIZED
    assert hashes.calls == [(desired, True), (removed, False)]


@pytest.mark.unit
def test_new_desired_target_missing_from_lifecycle_is_still_assessed() -> None:
    target = _target("new")
    unavailable = ProjectionFreshnessAssessment(
        state="unavailable",
        reason="target_not_registered",
        definition_id="example.current-state",
        target=target,
        current_definition_version=2,
        target_present=None,
        work_generation=None,
        applied_generation=None,
        certified_definition_version=None,
        certified_generation=None,
        certified_projection_hash=None,
        current_projection_hash=None,
        derivation_id=None,
    )
    lifecycle = FakeLifecycle(tokens=(), assessments={target: unavailable})
    hashes = FakeHashes()
    outcome = ProjectionMaterialization(
        target=target,
        view_kind="example_view",
        payload={"value": 1},
        contributor_ids=("a-1",),
    )

    report = RealizationCoverageService(lifecycle, hashes).audit_definition(
        _definition(),
        [outcome],
    )

    assert report.records[0].status is RealizationStatus.UNAVAILABLE
    assert report.records[0].reason == "target_not_registered"
    assert hashes.calls == [(target, True)]


@pytest.mark.unit
def test_noncurrent_lifecycle_snapshot_fails_before_adapter_hashing() -> None:
    target = _target("old")
    lifecycle = FakeLifecycle(
        tokens=(_token(target, version=1),),
        assessments={target: _assessment(target, present=True)},
    )
    hashes = FakeHashes()

    with pytest.raises(ProjectionLifecycleCorruptionError, match="definition version"):
        RealizationCoverageService(lifecycle, hashes).audit_definition(_definition(), [])

    assert hashes.calls == []
