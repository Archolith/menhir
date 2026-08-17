"""Pure Realization Coverage for generic projection outcomes.

Realization Coverage asks a different question from projection-content parity: for every desired
projection outcome, is there a current lifecycle proof that the exact target/generation/definition
was durably realized? It consumes the existing T4 desired-outcome contract and T5 freshness
assessment; it creates no second receipt, ledger, or freshness authority.
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


def build_realization_coverage_report(
    *,
    definition: ProjectionDefinition,
    outcomes: Iterable[ProjectionOutcome],
    freshness: Iterable[ProjectionFreshnessAssessment],
) -> RealizationCoverageReport:
    """Classify whether every desired outcome has an exact current T5 realization proof.

    Every evaluated T4 outcome still represents a desired target, including abstention and
    retirement. ``target_present=False`` means the target left the authoritative target set; it is
    therefore not valid proof for an outcome still present in ``outcomes``. Installed-state hash
    semantics remain adapter-owned and are already checked by ``assess_freshness``.
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

    records: list[RealizationRecord] = []
    for target in sorted(desired, key=lambda item: item.sort_key):
        outcome = desired[target]
        assessment = assessments.get(target)
        if assessment is None:
            status = RealizationStatus.UNAVAILABLE
            reason = "freshness_assessment_missing"
            derivation_id = None
        elif assessment.definition_id != definition.definition_id:
            status = RealizationStatus.INVALID_PROOF
            reason = "freshness_identity_mismatch"
            derivation_id = assessment.derivation_id
        elif assessment.state == "unavailable":
            status = RealizationStatus.UNAVAILABLE
            reason = assessment.reason
            derivation_id = assessment.derivation_id
        elif assessment.current_definition_version != definition.version:
            status = RealizationStatus.INVALID_PROOF
            reason = "definition_version_mismatch"
            derivation_id = assessment.derivation_id
        elif assessment.target_present is not True:
            status = RealizationStatus.INVALID_PROOF
            reason = "desired_target_not_present"
            derivation_id = assessment.derivation_id
        elif assessment.state == "stale":
            status = RealizationStatus.STALE
            reason = assessment.reason
            derivation_id = assessment.derivation_id
        else:
            status = RealizationStatus.REALIZED
            reason = assessment.reason
            derivation_id = assessment.derivation_id

        records.append(
            RealizationRecord(
                definition_id=definition.definition_id,
                definition_version=definition.version,
                target=target,
                outcome_kind=_outcome_kind(outcome),
                status=status,
                reason=reason,
                derivation_id=derivation_id,
            )
        )

    return RealizationCoverageReport(
        definition_id=definition.definition_id,
        definition_version=definition.version,
        records=tuple(records),
    )
