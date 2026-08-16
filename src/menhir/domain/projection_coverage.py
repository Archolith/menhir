"""Pure Projection Coverage audit for ScalarStateView.

This module is deliberately read-only and infrastructure-free. It classifies durable assertion
rows, enriches the existing deterministic scalar fold with assertion roles, and compares that fold
with persisted current scalar-state View rows. Views never influence fold membership.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from enum import Enum
from typing import Any, Protocol

from menhir.domain.scalar_state_fold import FoldResult
from menhir.domain.temporal import parse_iso8601
from menhir.domain.typed_assertion import normalize_scalar

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


def _slot_of_assertion(row: dict[str, Any]) -> SlotKey:
    return (
        str(row.get("subject_uuid") or "").strip(),
        str(row.get("attribute") or "").strip().lower(),
        str(row.get("scope") or "").strip().lower(),
        str(row.get("value_kind") or "").strip().lower(),
        str(row.get("unit") or "").strip().lower(),
    )


def _slot_of_view(row: dict[str, Any]) -> SlotKey:
    return (
        str(row.get("subject_uuid") or row.get("view_subject_uuid") or "").strip(),
        str(row.get("attribute") or row.get("ss_attribute") or "").strip().lower(),
        str(row.get("scope") or row.get("ss_scope") or "").strip().lower(),
        str(row.get("value_kind") or row.get("ss_kind") or "").strip().lower(),
        str(row.get("unit") or row.get("ss_unit") or "").strip().lower(),
    )


def classify_lifecycle(assertion: dict[str, Any]) -> AssertionLifecycle:
    return (
        AssertionLifecycle.SUPERSEDED
        if bool(assertion.get("superseded", False))
        else AssertionLifecycle.CURRENT
    )


def classify_binding(assertion: dict[str, Any]) -> BindingStatus:
    if bool(assertion.get("binding_pending", False)):
        return BindingStatus.BINDING_PENDING

    owner = str(
        assertion.get("head_current_subject_uuid")
        or assertion.get("head_subject_uuid")
        or ""
    ).strip()
    subject = str(assertion.get("subject_uuid") or "").strip()
    current_id = str(assertion.get("head_current_assertion_id") or "").strip()
    assertion_id = str(assertion.get("assertion_id") or "").strip()

    if owner and subject and owner != subject:
        return BindingStatus.BINDING_MISMATCH
    if (
        classify_lifecycle(assertion) is AssertionLifecycle.CURRENT
        and current_id
        and assertion_id
        and current_id != assertion_id
    ):
        return BindingStatus.BINDING_MISMATCH
    return BindingStatus.BOUND


def enrich_fold(
    materializable_assertions: list[dict[str, Any]],
    fold: FoldResult,
) -> AuditEnrichedFoldResult:
    """Assign fold roles using only the exact input set and fold result."""

    by_slot: dict[SlotKey, list[str]] = {}
    for row in materializable_assertions:
        assertion_id = str(row.get("assertion_id") or "")
        if assertion_id:
            by_slot.setdefault(_slot_of_assertion(row), []).append(assertion_id)

    role_by_id: dict[str, FoldRole] = {}
    for state in fold.states:
        slot = tuple(state.slot_key)
        contributors = set(state.contributor_ids)
        for assertion_id in by_slot.get(slot, []):
            role_by_id[assertion_id] = (
                FoldRole.CONTRIBUTOR
                if assertion_id in contributors
                else FoldRole.NON_CONTRIBUTING_MEMBER
            )

    no_view_slots = {tuple(item.slot_key) for item in fold.abstentions}
    expiry_slots = {tuple(item.slot_key) for item in fold.expiries}
    for slot in no_view_slots:
        for assertion_id in by_slot.get(slot, []):
            role_by_id[assertion_id] = FoldRole.SLOT_ABSTENTION_MEMBER
    for slot in expiry_slots:
        for assertion_id in by_slot.get(slot, []):
            role_by_id[assertion_id] = FoldRole.NON_CONTRIBUTING_MEMBER

    return AuditEnrichedFoldResult(
        fold=fold,
        roles=tuple(sorted(role_by_id.items(), key=lambda item: item[0])),
    )


def _normalized_time(value: Any) -> tuple[str, str]:
    parsed = parse_iso8601(value)
    if parsed is not None:
        return ("parsed", parsed.astimezone(timezone.utc).isoformat())
    return ("raw", str(value or "").strip())


def compare_projection_parity(
    desired: AuditEnrichedFoldResult,
    actual_views: list[dict[str, Any]],
    *,
    namespace: str | None,
) -> tuple[tuple[ProjectionCoverageViolation, ...], tuple[ProjectionParityViolation, ...]]:
    """Strictly compare desired fold output to current persisted Views."""

    coverage: list[ProjectionCoverageViolation] = []
    parity: list[ProjectionParityViolation] = []

    views_by_slot: dict[SlotKey, list[dict[str, Any]]] = {}
    for view in actual_views:
        slot = _slot_of_view(view)
        view_namespace = view.get("namespace")
        if namespace is not None and view_namespace != namespace:
            coverage.append(
                ProjectionCoverageViolation(
                    AuditFailureClassification.NAMESPACE_MISMATCH,
                    f"current View {view.get('view_key')!r} belongs to namespace "
                    f"{view_namespace!r}, expected {namespace!r}",
                    slot_key=slot,
                    repairable=False,
                    recommended_repair=RecommendedRepair.INSPECT_WRITE_PATH,
                )
            )
            continue
        views_by_slot.setdefault(slot, []).append(view)

    states_by_slot = {tuple(state.slot_key): state for state in desired.fold.states}
    abstention_slots = {tuple(item.slot_key) for item in desired.fold.abstentions}
    expiry_slots = {tuple(item.slot_key) for item in desired.fold.expiries}

    for slot in sorted(states_by_slot):
        state = states_by_slot[slot]
        views = views_by_slot.get(slot, [])
        if not views:
            parity.append(
                ProjectionParityViolation(
                    ParityViolationKind.MISSING_VIEW,
                    slot,
                    "fold requires a current ScalarStateView but none exists",
                    recommended_repair=RecommendedRepair.REBUILD_VIEW,
                )
            )
            continue
        if len(views) > 1:
            coverage.append(
                ProjectionCoverageViolation(
                    AuditFailureClassification.CORRUPT_OR_BYPASSED_WRITE_PATH,
                    f"{len(views)} current ScalarStateViews exist for one slot",
                    slot_key=slot,
                    repairable=False,
                    recommended_repair=RecommendedRepair.INSPECT_WRITE_PATH,
                )
            )
            continue

        view = views[0]
        view_key = str(view.get("view_key") or "") or None
        actual_value = view.get("view_value", view.get("value", view.get("ss_value")))
        if normalize_scalar(actual_value) != normalize_scalar(state.value):
            parity.append(
                ProjectionParityViolation(
                    ParityViolationKind.VALUE_MISMATCH,
                    slot,
                    f"View value {actual_value!r} != folded value {normalize_scalar(state.value)!r}",
                    view_key=view_key,
                )
            )
        if _normalized_time(view.get("valid_at")) != _normalized_time(state.valid_at):
            parity.append(
                ProjectionParityViolation(
                    ParityViolationKind.VALID_AT_MISMATCH,
                    slot,
                    "View valid_at does not equal the fold's latest contributing valid_at",
                    view_key=view_key,
                )
            )
        actual_contributors = {
            str(value) for value in (view.get("scalar_contributors") or []) if str(value)
        }
        if actual_contributors != set(state.contributor_ids):
            parity.append(
                ProjectionParityViolation(
                    ParityViolationKind.CONTRIBUTOR_SET_MISMATCH,
                    slot,
                    "View contributor assertion-id set does not equal fold contributors",
                    view_key=view_key,
                )
            )
        if str(view.get("scalar_effective_tier") or "") != state.effective_tier:
            parity.append(
                ProjectionParityViolation(
                    ParityViolationKind.TIER_MISMATCH,
                    slot,
                    "View effective tier does not equal the fold's weakest contributor tier",
                    view_key=view_key,
                )
            )
        actual_episodes = {
            str(value) for value in (view.get("episode_uuids") or []) if str(value)
        }
        if actual_episodes != set(state.episode_uuids):
            parity.append(
                ProjectionParityViolation(
                    ParityViolationKind.PROVENANCE_MISMATCH,
                    slot,
                    "View episode provenance set does not equal fold provenance",
                    view_key=view_key,
                )
            )

    for slot in sorted(views_by_slot):
        if slot in states_by_slot:
            continue
        for view in views_by_slot[slot]:
            view_key = str(view.get("view_key") or "") or None
            parity.append(
                ProjectionParityViolation(
                    ParityViolationKind.ORPHANED_VIEW,
                    slot,
                    "current ScalarStateView has no desired folded state",
                    view_key=view_key,
                    recommended_repair=RecommendedRepair.RETIRE_ORPHAN,
                )
            )
        if slot in abstention_slots or slot in expiry_slots:
            coverage.append(
                ProjectionCoverageViolation(
                    AuditFailureClassification.INVALID_COMBINATION,
                    "current View exists for a slot whose deterministic fold requires no current View",
                    slot_key=slot,
                    repairable=True,
                    recommended_repair=RecommendedRepair.RETIRE_ORPHAN,
                )
            )

    coverage.sort(
        key=lambda item: (
            item.classification.value,
            item.slot_key or ("", "", "", "", ""),
            item.assertion_id or "",
            item.message,
        )
    )
    parity.sort(key=lambda item: (item.slot_key, item.kind.value, item.view_key or ""))
    return tuple(coverage), tuple(parity)


def build_projection_coverage_report(
    *,
    subject_uuid: str,
    namespace: str | None,
    assertions: list[dict[str, Any]],
    fold: FoldResult,
    actual_views: list[dict[str, Any]],
    eligibility_source: AssertionEligibilitySource | None = None,
) -> ProjectionCoverageReport:
    """Build one deterministic report from already-loaded durable inputs."""

    eligibility = eligibility_source or DefaultAssertionEligibilitySource()

    decisions: dict[str, tuple[AssertionLifecycle, BindingStatus, EligibilityRole | None]] = {}
    materializable: list[dict[str, Any]] = []
    for row in assertions:
        assertion_id = str(row.get("assertion_id") or "")
        lifecycle = classify_lifecycle(row)
        binding = classify_binding(row)
        eligibility_role: EligibilityRole | None = None
        if lifecycle is AssertionLifecycle.CURRENT:
            if binding is BindingStatus.BOUND:
                eligibility_role = eligibility.eligibility_for(row).role
            else:
                eligibility_role = EligibilityRole.BINDING_ADVISORY
        decisions[assertion_id] = (lifecycle, binding, eligibility_role)
        if (
            lifecycle is AssertionLifecycle.CURRENT
            and binding is BindingStatus.BOUND
            and eligibility_role is EligibilityRole.MATERIALIZABLE
        ):
            materializable.append(row)

    enriched = enrich_fold(materializable, fold)
    coverage, parity = compare_projection_parity(enriched, actual_views, namespace=namespace)
    coverage_list = list(coverage)

    parity_slots = {item.slot_key for item in parity}
    coverage_error_slots = {
        item.slot_key
        for item in coverage
        if item.slot_key is not None
        and item.classification is AuditFailureClassification.CORRUPT_OR_BYPASSED_WRITE_PATH
    }
    view_slots = {
        _slot_of_view(view)
        for view in actual_views
        if namespace is None or view.get("namespace") == namespace
    }
    abstention_slots = {tuple(item.slot_key) for item in fold.abstentions}
    expiry_slots = {tuple(item.slot_key) for item in fold.expiries}

    accounting: list[ProjectionAccountingRecord] = []
    for row in assertions:
        assertion_id = str(row.get("assertion_id") or "")
        lifecycle, binding, eligibility_role = decisions[assertion_id]
        slot = _slot_of_assertion(row)
        fold_role = enriched.role_for(assertion_id)

        if (
            lifecycle is AssertionLifecycle.CURRENT
            and binding is BindingStatus.BINDING_MISMATCH
        ):
            coverage_list.append(
                ProjectionCoverageViolation(
                    AuditFailureClassification.CORRUPT_OR_BYPASSED_WRITE_PATH,
                    "current assertion binding does not match durable TypedAssertionHead authority",
                    assertion_id=assertion_id,
                    slot_key=slot,
                    repairable=False,
                    recommended_repair=RecommendedRepair.INSPECT_WRITE_PATH,
                )
            )

        if (
            lifecycle is AssertionLifecycle.CURRENT
            and binding is BindingStatus.BOUND
            and eligibility_role is EligibilityRole.MATERIALIZABLE
            and fold_role is None
        ):
            coverage_list.append(
                ProjectionCoverageViolation(
                    AuditFailureClassification.UNACCOUNTED,
                    "current materializable assertion has no deterministic fold role",
                    assertion_id=assertion_id,
                    slot_key=slot,
                    repairable=False,
                    recommended_repair=RecommendedRepair.INSPECT_WRITE_PATH,
                )
            )

        pending = bool(row.get("projection_pending", False))
        if not (
            lifecycle is AssertionLifecycle.CURRENT
            and binding is BindingStatus.BOUND
            and eligibility_role is EligibilityRole.MATERIALIZABLE
        ):
            projection_status = ProjectionStatus.NOT_REQUIRED
        elif slot in abstention_slots or slot in expiry_slots:
            projection_status = ProjectionStatus.NOT_REQUIRED
        elif pending:
            projection_status = ProjectionStatus.PROJECTION_PENDING
        elif slot not in view_slots or slot in parity_slots or slot in coverage_error_slots:
            projection_status = ProjectionStatus.PROJECTION_ERROR
        else:
            projection_status = ProjectionStatus.PROJECTED

        row_namespace = row.get("namespace")
        if namespace is not None and row_namespace != namespace:
            coverage_list.append(
                ProjectionCoverageViolation(
                    AuditFailureClassification.NAMESPACE_MISMATCH,
                    f"assertion belongs to namespace {row_namespace!r}, expected {namespace!r}",
                    assertion_id=assertion_id,
                    slot_key=slot,
                    repairable=False,
                    recommended_repair=RecommendedRepair.INSPECT_WRITE_PATH,
                )
            )

        accounting.append(
            ProjectionAccountingRecord(
                assertion_id=assertion_id,
                namespace=row_namespace,
                slot_key=slot,
                lifecycle=lifecycle,
                binding_status=binding,
                eligibility_role=eligibility_role,
                fold_role=fold_role,
                projection_status=projection_status,
                evidence_tier=str(row.get("evidence_tier") or ""),
                projection_pending=pending,
            )
        )

    coverage_list.sort(
        key=lambda item: (
            item.classification.value,
            item.slot_key or ("", "", "", "", ""),
            item.assertion_id or "",
            item.message,
        )
    )
    accounting.sort(key=lambda item: (item.slot_key, item.assertion_id))
    return ProjectionCoverageReport(
        subject_uuid=subject_uuid,
        namespace=namespace,
        accounting=tuple(accounting),
        coverage_violations=tuple(coverage_list),
        parity_violations=parity,
        fold=enriched,
    )
