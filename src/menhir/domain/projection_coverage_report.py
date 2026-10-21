"""Assertion classification, fold-role enrichment and report assembly for Projection Coverage.

These helpers moved here from ``projection_coverage`` so that module stays
small; the original module path re-exports them unchanged.
"""

from __future__ import annotations

from typing import Any

from menhir.domain.projection_coverage_parity import _slot_of_view, compare_projection_parity
from menhir.domain.projection_coverage_types import (
    AssertionEligibilitySource,
    AssertionLifecycle,
    AuditEnrichedFoldResult,
    AuditFailureClassification,
    BindingStatus,
    DefaultAssertionEligibilitySource,
    EligibilityRole,
    FoldRole,
    ProjectionAccountingRecord,
    ProjectionCoverageReport,
    ProjectionCoverageViolation,
    ProjectionStatus,
    RecommendedRepair,
    SlotKey,
)
from menhir.domain.scalar_state_fold import FoldResult


def _slot_of_assertion(row: dict[str, Any]) -> SlotKey:
    return (
        str(row.get("subject_uuid") or "").strip(),
        str(row.get("attribute") or "").strip().lower(),
        str(row.get("scope") or "").strip().lower(),
        str(row.get("value_kind") or "").strip().lower(),
        str(row.get("unit") or "").strip().lower(),
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
