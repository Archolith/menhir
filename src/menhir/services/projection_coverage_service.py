"""Read-only orchestration for Projection Coverage v1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from menhir.domain.projection_coverage import (
    AssertionEligibilitySource,
    AssertionLifecycle,
    BindingStatus,
    DefaultAssertionEligibilitySource,
    EligibilityDecision,
    EligibilityRole,
    ProjectionCoverageReport,
    build_projection_coverage_report,
    classify_binding,
    classify_lifecycle,
)
from menhir.domain.scalar_state_fold import fold_assertions


class ProjectionCoverageSource(Protocol):
    def assertions_for_projection_audit(
        self,
        subject_uuid: str,
        *,
        namespace: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def list_scalar_state_views_for_audit(
        self,
        *,
        subject_uuid: str,
        namespace: str | None = None,
    ) -> list[dict[str, Any]]: ...


class _CachedEligibilitySource:
    """Evaluate one eligibility decision per assertion for the entire audit."""

    def __init__(self, source: AssertionEligibilitySource) -> None:
        self._source = source
        self._decisions: dict[str, EligibilityDecision] = {}

    def eligibility_for(self, assertion: dict[str, Any]) -> EligibilityDecision:
        assertion_id = str(assertion.get("assertion_id") or "")
        key = assertion_id or f"row:{id(assertion)}"
        if key not in self._decisions:
            self._decisions[key] = self._source.eligibility_for(assertion)
        return self._decisions[key]


class _CountingOffEligibility:
    """With counting off the writer folds no occurrences, so none may be expected to materialize."""

    def __init__(self, source: AssertionEligibilitySource) -> None:
        self._source = source

    def eligibility_for(self, assertion: dict[str, Any]) -> EligibilityDecision:
        if str(assertion.get("operation")) == "occurrence":
            return EligibilityDecision(EligibilityRole.OPERATOR_VETOED, "counting_disabled")
        return self._source.eligibility_for(assertion)


@dataclass(frozen=True)
class ProjectionCoverageBatchFailure:
    subject_uuid: str
    namespace: str | None
    error_type: str
    message: str


@dataclass(frozen=True)
class ProjectionCoverageBatchReport:
    reports: tuple[ProjectionCoverageReport, ...]
    failures: tuple[ProjectionCoverageBatchFailure, ...]

    @property
    def clean(self) -> bool:
        return not self.failures and all(report.clean for report in self.reports)


class ProjectionCoverageService:
    """Recompute desired scalar state from assertions and compare it with current Views."""

    MAX_BATCH = 200

    def __init__(
        self,
        source: ProjectionCoverageSource,
        *,
        counting_enabled: bool,
        count_match: bool,
        as_of: datetime | None = None,
    ) -> None:
        """`counting_enabled` and `count_match` must be the writer's: the adapter's
        `counting_enabled` and `counting_match_enabled`, exactly as `scalar_state_service()` passes
        them to ScalarStateService (match is off whenever counting is off)."""
        self._source = source
        self._counting_enabled = bool(counting_enabled)
        self._count_match = bool(count_match) and self._counting_enabled
        self._as_of = as_of

    def audit_entity(
        self,
        subject_uuid: str,
        *,
        namespace: str | None = None,
        eligibility_source: AssertionEligibilitySource | None = None,
    ) -> ProjectionCoverageReport:
        """Audit one entity without mutating assertions, Views, or projection markers."""
        subject_uuid = str(subject_uuid or "").strip()
        if not subject_uuid:
            raise ValueError("projection coverage audit requires a non-blank subject_uuid")

        assertions = self._source.assertions_for_projection_audit(
            subject_uuid,
            namespace=namespace,
        )
        actual_views = self._source.list_scalar_state_views_for_audit(
            subject_uuid=subject_uuid,
            namespace=namespace,
        )

        # A report slot deliberately excludes namespace because a scoped audit represents exactly one
        # silo.  An unscoped read is safe only when all durable inputs resolve to that same single silo;
        # otherwise folding them together could make one tenant's assertion satisfy another tenant's
        # projection.  Refuse the ambiguous shape instead of manufacturing cross-namespace parity.
        if namespace is None:
            observed_namespaces = {
                None if row.get("namespace") is None else str(row.get("namespace"))
                for row in [*assertions, *actual_views]
            }
            if len(observed_namespaces) > 1:
                rendered = ", ".join(sorted(repr(value) for value in observed_namespaces))
                raise ValueError(
                    "projection coverage audit requires an explicit namespace when one subject "
                    f"spans multiple namespaces: {rendered}"
                )

        base_eligibility = eligibility_source or DefaultAssertionEligibilitySource()
        if not self._counting_enabled:
            base_eligibility = _CountingOffEligibility(base_eligibility)
        cached_eligibility = _CachedEligibilitySource(base_eligibility)

        materializable: list[dict[str, Any]] = []
        for assertion in assertions:
            if classify_lifecycle(assertion) is not AssertionLifecycle.CURRENT:
                continue
            if classify_binding(assertion) is not BindingStatus.BOUND:
                continue
            if (
                cached_eligibility.eligibility_for(assertion).role
                is not EligibilityRole.MATERIALIZABLE
            ):
                continue
            materializable.append(assertion)

        # Same fold parameters as the writer: match mode as configured and evaluation at now (UTC,
        # inclusive, computed once), so a correct match-mode View is not reported orphan/missing.
        # Views never choose these members.
        fold = fold_assertions(
            materializable,
            as_of=self._as_of or datetime.now(timezone.utc),
            include_occurrences=self._counting_enabled,
            count_match=self._count_match,
        )
        return build_projection_coverage_report(
            subject_uuid=subject_uuid,
            namespace=namespace,
            assertions=assertions,
            fold=fold,
            actual_views=actual_views,
            eligibility_source=cached_eligibility,
        )

    def audit_entities(
        self,
        work_items: list[tuple[str, str | None]],
    ) -> ProjectionCoverageBatchReport:
        """Audit a bounded work list, isolating failures per entity instead of hiding them."""
        if len(work_items) > self.MAX_BATCH:
            raise ValueError(
                f"projection coverage batch exceeds {self.MAX_BATCH} work items"
            )

        reports: list[ProjectionCoverageReport] = []
        failures: list[ProjectionCoverageBatchFailure] = []
        for subject_uuid, namespace in work_items:
            try:
                reports.append(
                    self.audit_entity(subject_uuid, namespace=namespace)
                )
            except Exception as exc:  # noqa: BLE001 - failure is a first-class batch result
                failures.append(
                    ProjectionCoverageBatchFailure(
                        subject_uuid=str(subject_uuid),
                        namespace=namespace,
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )
                )
        return ProjectionCoverageBatchReport(
            reports=tuple(reports),
            failures=tuple(failures),
        )
