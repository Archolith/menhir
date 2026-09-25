"""Pure Projection Coverage audit for ScalarStateView.

This module is deliberately read-only and infrastructure-free. It classifies durable assertion
rows, enriches the existing deterministic scalar fold with assertion roles, and compares that fold
with persisted current scalar-state View rows. Views never influence fold membership.
"""

from __future__ import annotations

# Facade: the projection coverage implementation moved to the sibling modules
# named below. Every name this module originally exposed -- public and
# private -- is re-exported here unchanged, so existing import sites keep
# working.

from menhir.domain.projection_coverage_parity import (
    _normalized_time as _normalized_time,
    _slot_of_view as _slot_of_view,
    compare_projection_parity as compare_projection_parity,
)

from menhir.domain.projection_coverage_report import (
    _slot_of_assertion as _slot_of_assertion,
    build_projection_coverage_report as build_projection_coverage_report,
    classify_binding as classify_binding,
    classify_lifecycle as classify_lifecycle,
    enrich_fold as enrich_fold,
)

from menhir.domain.projection_coverage_types import (
    AssertionEligibilitySource as AssertionEligibilitySource,
    AssertionLifecycle as AssertionLifecycle,
    AuditEnrichedFoldResult as AuditEnrichedFoldResult,
    AuditFailureClassification as AuditFailureClassification,
    BindingStatus as BindingStatus,
    DefaultAssertionEligibilitySource as DefaultAssertionEligibilitySource,
    EligibilityDecision as EligibilityDecision,
    EligibilityRole as EligibilityRole,
    FoldRole as FoldRole,
    ParityViolationKind as ParityViolationKind,
    ProjectionAccountingRecord as ProjectionAccountingRecord,
    ProjectionCoverageReport as ProjectionCoverageReport,
    ProjectionCoverageViolation as ProjectionCoverageViolation,
    ProjectionParityViolation as ProjectionParityViolation,
    ProjectionStatus as ProjectionStatus,
    RecommendedRepair as RecommendedRepair,
    SlotKey as SlotKey,
)
