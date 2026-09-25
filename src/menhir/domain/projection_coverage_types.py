"""Pure value types for the Projection Coverage audit.

Lifecycle, binding, eligibility, fold-role, projection-status and violation
enums, the frozen report/record dataclasses, the eligibility source protocol
with its default implementation, and the ``SlotKey`` alias. These names moved
here from ``projection_coverage`` so that module stays small; the original
module path re-exports all of them unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from menhir.domain.scalar_state_fold import FoldResult

SlotKey = tuple[str, str, str, str, str]


class AssertionLifecycle(str, Enum):
    CURRENT = "current"
    SUPERSEDED = "superseded"


class BindingStatus(str, Enum):
    BOUND = "bound"
    BINDING_PENDING = "binding_pending"
    BINDING_MISMATCH = "binding_mismatch"


class EligibilityRole(str, Enum):
    MATERIALIZABLE = "materializable"
    BINDING_ADVISORY = "binding_advisory"
    RETRACTED = "retracted"
    OPERATOR_VETOED = "operator_vetoed"


class FoldRole(str, Enum):
    CONTRIBUTOR = "contributor"
    NON_CONTRIBUTING_MEMBER = "non_contributing_member"
    SLOT_ABSTENTION_MEMBER = "slot_abstention_member"


class ProjectionStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    PROJECTION_PENDING = "projection_pending"
    PROJECTED = "projected"
    PROJECTION_ERROR = "projection_error"


class AuditFailureClassification(str, Enum):
    UNACCOUNTED = "unaccounted"
    MULTIPLY_ACCOUNTED = "multiply_accounted"
    INVALID_COMBINATION = "invalid_combination"
    NAMESPACE_MISMATCH = "namespace_mismatch"
    CORRUPT_OR_BYPASSED_WRITE_PATH = "corrupt_or_bypassed_write_path"


class ParityViolationKind(str, Enum):
    MISSING_VIEW = "missing_view"
    ORPHANED_VIEW = "orphaned_view"
    VALUE_MISMATCH = "value_mismatch"
    VALID_AT_MISMATCH = "valid_at_mismatch"
    CONTRIBUTOR_SET_MISMATCH = "contributor_set_mismatch"
    TIER_MISMATCH = "tier_mismatch"
    PROVENANCE_MISMATCH = "provenance_mismatch"


class RecommendedRepair(str, Enum):
    REBUILD_VIEW = "rebuild_view"
    RETIRE_ORPHAN = "retire_orphan"
    INSPECT_WRITE_PATH = "inspect_write_path"
    OPERATOR_ONLY = "operator_only"


@dataclass(frozen=True)
class EligibilityDecision:
    role: EligibilityRole
    reason: str | None = None


class AssertionEligibilitySource(Protocol):
    def eligibility_for(self, assertion: dict[str, Any]) -> EligibilityDecision: ...


class DefaultAssertionEligibilitySource:
    """V1 eligibility: authority changes are not invented by the audit."""

    def eligibility_for(self, assertion: dict[str, Any]) -> EligibilityDecision:
        return EligibilityDecision(EligibilityRole.MATERIALIZABLE)


@dataclass(frozen=True)
class ProjectionAccountingRecord:
    assertion_id: str
    namespace: str | None
    slot_key: SlotKey
    lifecycle: AssertionLifecycle
    binding_status: BindingStatus
    eligibility_role: EligibilityRole | None
    fold_role: FoldRole | None
    projection_status: ProjectionStatus
    evidence_tier: str
    projection_pending: bool


@dataclass(frozen=True)
class ProjectionCoverageViolation:
    classification: AuditFailureClassification
    message: str
    assertion_id: str | None = None
    slot_key: SlotKey | None = None
    repairable: bool = False
    recommended_repair: RecommendedRepair = RecommendedRepair.OPERATOR_ONLY


@dataclass(frozen=True)
class ProjectionParityViolation:
    kind: ParityViolationKind
    slot_key: SlotKey
    message: str
    view_key: str | None = None
    repairable: bool = True
    recommended_repair: RecommendedRepair = RecommendedRepair.REBUILD_VIEW


@dataclass(frozen=True)
class AuditEnrichedFoldResult:
    fold: FoldResult
    roles: tuple[tuple[str, FoldRole], ...]

    def role_for(self, assertion_id: str) -> FoldRole | None:
        for candidate, role in self.roles:
            if candidate == assertion_id:
                return role
        return None


@dataclass(frozen=True)
class ProjectionCoverageReport:
    subject_uuid: str
    namespace: str | None
    accounting: tuple[ProjectionAccountingRecord, ...]
    coverage_violations: tuple[ProjectionCoverageViolation, ...]
    parity_violations: tuple[ProjectionParityViolation, ...]
    fold: AuditEnrichedFoldResult

    @property
    def clean(self) -> bool:
        return not self.coverage_violations and not self.parity_violations
