"""Strict parity comparison between the desired fold and persisted scalar-state Views.

These helpers moved here from ``projection_coverage`` so that module stays
small; the original module path re-exports them unchanged.
"""

from __future__ import annotations

from datetime import timezone
from typing import Any

from menhir.domain.projection_coverage_types import (
    AuditEnrichedFoldResult,
    AuditFailureClassification,
    ParityViolationKind,
    ProjectionCoverageViolation,
    ProjectionParityViolation,
    RecommendedRepair,
    SlotKey,
)
from menhir.domain.temporal import parse_iso8601
from menhir.domain.typed_assertion import normalize_scalar


def _slot_of_view(row: dict[str, Any]) -> SlotKey:
    return (
        str(row.get("subject_uuid") or row.get("view_subject_uuid") or "").strip(),
        str(row.get("attribute") or row.get("ss_attribute") or "").strip().lower(),
        str(row.get("scope") or row.get("ss_scope") or "").strip().lower(),
        str(row.get("value_kind") or row.get("ss_kind") or "").strip().lower(),
        str(row.get("unit") or row.get("ss_unit") or "").strip().lower(),
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
