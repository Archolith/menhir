"""Pure Realization Coverage for generic projection outcomes.

Realization Coverage asks a different question from projection-content parity: for every desired
projection outcome, is there a current lifecycle proof that the exact target/generation/definition
was durably realized? It also accounts for lifecycle targets that have left the authoritative target
set, so an unfinished removal cannot disappear merely because T4 no longer emits an outcome for it.
It consumes the existing T4 desired-outcome contract and T5 freshness assessment; it creates no
second receipt, ledger, or freshness authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from menhir.domain.projection import (
    ProjectionAbstention,
    ProjectionDefinition,
    ProjectionMaterialization,
    ProjectionOutcome,
    ProjectionRetirement,
    ProjectionTarget,
)
from menhir.domain.projection_lifecycle import ProjectionFreshnessAssessment

__all__ = [
    "RealizationCoverageReport",
    "RealizationOutcomeKind",
    "RealizationRecord",
    "RealizationStatus",
    "build_realization_coverage_report",
]


class RealizationOutcomeKind(str, Enum):
    MATERIALIZATION = "materialization"
    ABSTENTION = "abstention"
    RETIREMENT = "retirement"
    REMOVAL = "removal"


class RealizationStatus(str, Enum):
    REALIZED = "realized"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    INVALID_PROOF = "invalid_proof"


@dataclass(frozen=True)
class RealizationRecord:
    definition_id: str
    definition_version: int
    target: ProjectionTarget
    outcome_kind: RealizationOutcomeKind
    status: RealizationStatus
    reason: str
    derivation_id: str | None


@dataclass(frozen=True)
class RealizationCoverageReport:
    definition_id: str
    definition_version: int
    records: tuple[RealizationRecord, ...]

    @property
    def clean(self) -> bool:
        return all(record.status is RealizationStatus.REALIZED for record in self.records)


def _outcome_kind(outcome: ProjectionOutcome) -> RealizationOutcomeKind:
    if isinstance(outcome, ProjectionMaterialization):
        return RealizationOutcomeKind.MATERIALIZATION
    if isinstance(outcome, ProjectionAbstention):
        return RealizationOutcomeKind.ABSTENTION
    if isinstance(outcome, ProjectionRetirement):
        return RealizationOutcomeKind.RETIREMENT
    raise TypeError("outcomes must contain ProjectionOutcome values")


def _classify_desired(
    *,
    definition: ProjectionDefinition,
    outcome: ProjectionOutcome,
    assessment: ProjectionFreshnessAssessment | None,
) -> RealizationRecord:
    target = outcome.target
    kind = _outcome_kind(outcome)
    if assessment is None:
        return RealizationRecord(
            definition.definition_id,
            definition.version,
            target,
            kind,
            RealizationStatus.UNAVAILABLE,
            "freshness_assessment_missing",
            None,
        )
    if assessment.definition_id != definition.definition_id:
        status = RealizationStatus.INVALID_PROOF
        reason = "freshness_identity_mismatch"
    elif assessment.state == "unavailable":
        # An unavailable assessment may legitimately have no published definition version. If it does
        # name a version, however, it must still be the definition being audited.
        if (
            assessment.current_definition_version is not None
            and assessment.current_definition_version != definition.version
        ):
            status = RealizationStatus.INVALID_PROOF
            reason = "definition_version_mismatch"
        else:
            status = RealizationStatus.UNAVAILABLE
            reason = assessment.reason
    elif assessment.current_definition_version != definition.version:
        # Fresh/stale are lifecycle claims about a published definition and therefore must be bound to
        # the exact audited semantic version. ``None`` is not acceptable proof here.
        status = RealizationStatus.INVALID_PROOF
        reason = "definition_version_mismatch"
    elif assessment.target_present is not True:
        status = RealizationStatus.INVALID_PROOF
        reason = "desired_target_not_present"
    elif assessment.state == "stale":
        status = RealizationStatus.STALE
        reason = assessment.reason
    else:
        status = RealizationStatus.REALIZED
        reason = assessment.reason
    return RealizationRecord(
        definition.definition_id,
        definition.version,
        target,
        kind,
        status,
        reason,
        assessment.derivation_id,
    )


def _classify_removed(
    *,
    definition: ProjectionDefinition,
    assessment: ProjectionFreshnessAssessment,
) -> RealizationRecord:
    """Classify one lifecycle target absent from the complete desired-outcome set."""

    if assessment.definition_id != definition.definition_id:
        status = RealizationStatus.INVALID_PROOF
        reason = "freshness_identity_mismatch"
    elif assessment.state == "unavailable":
        if (
            assessment.current_definition_version is not None
            and assessment.current_definition_version != definition.version
        ):
            status = RealizationStatus.INVALID_PROOF
            reason = "definition_version_mismatch"
        else:
            status = RealizationStatus.UNAVAILABLE
            reason = assessment.reason
    elif assessment.current_definition_version != definition.version:
        status = RealizationStatus.INVALID_PROOF
        reason = "definition_version_mismatch"
    elif assessment.target_present is True:
        # The lifecycle still considers this target present even though the authoritative T4 outcome
        # set no longer contains it. Reconciliation has not carried the removal through yet.
        status = RealizationStatus.STALE
        reason = "undesired_target_still_present"
    elif assessment.target_present is not False:
        status = RealizationStatus.INVALID_PROOF
        reason = "removed_target_presence_unknown"
    elif assessment.state == "stale":
        status = RealizationStatus.STALE
        reason = assessment.reason
    else:
        status = RealizationStatus.REALIZED
        reason = assessment.reason
    return RealizationRecord(
        definition.definition_id,
        definition.version,
        assessment.target,
        RealizationOutcomeKind.REMOVAL,
        status,
        reason,
        assessment.derivation_id,
    )


def build_realization_coverage_report(
    *,
    definition: ProjectionDefinition,
    outcomes: Iterable[ProjectionOutcome],
    freshness: Iterable[ProjectionFreshnessAssessment],
) -> RealizationCoverageReport:
    """Classify exact T5 realization proof for a complete T4 desired-outcome snapshot.

    Every evaluated T4 outcome still represents a desired target, including abstention and
    retirement. ``target_present=False`` instead means a previously known lifecycle target left the
    authoritative target set. Freshness assessments for targets not present in ``outcomes`` are
    therefore treated as removal work: a fresh absent target is a realized removal; a still-present
    target is stale. P2 is responsible for supplying the complete lifecycle target snapshot so removed
    targets cannot be omitted from this pure accounting step.

    Installed-state hash semantics remain adapter-owned and are already checked by T5
    ``assess_freshness``; this function does not duplicate receipt/hash validation.
    """

    if not isinstance(definition, ProjectionDefinition):
        raise TypeError("definition must be a ProjectionDefinition")

    desired: dict[ProjectionTarget, ProjectionOutcome] = {}
    for outcome in outcomes:
        _outcome_kind(outcome)
        if outcome.target in desired:
            raise ValueError("duplicate desired projection target")
        desired[outcome.target] = outcome

    assessments: dict[ProjectionTarget, ProjectionFreshnessAssessment] = {}
    for assessment in freshness:
        if not isinstance(assessment, ProjectionFreshnessAssessment):
            raise TypeError("freshness must contain ProjectionFreshnessAssessment values")
        if assessment.target in assessments:
            raise ValueError("duplicate freshness assessment target")
        assessments[assessment.target] = assessment

    records = [
        _classify_desired(
            definition=definition,
            outcome=desired[target],
            assessment=assessments.get(target),
        )
        for target in sorted(desired, key=lambda item: item.sort_key)
    ]
    records.extend(
        _classify_removed(definition=definition, assessment=assessments[target])
        for target in sorted(
            (candidate for candidate in assessments if candidate not in desired),
            key=lambda item: item.sort_key,
        )
    )
    records.sort(key=lambda item: item.target.sort_key)

    return RealizationCoverageReport(
        definition_id=definition.definition_id,
        definition_version=definition.version,
        records=tuple(records),
    )
