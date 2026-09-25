"""Shared seams and pure helpers for the scalar_state_service facade split.

`_AssertionSource` and `_ViewSink` are the two protocols `ScalarStateService` is built on; the
slot/reason/blocked/completion helpers are pure functions over fold results and View rows, shared
by the state, history, and merge-lifecycle sibling modules.
"""

from __future__ import annotations

from typing import Any, Protocol

from menhir.domain.scalar_history import MALFORMED_VALID_AT
from menhir.domain.scalar_state_fold import FoldResult


class _AssertionSource(Protocol):
    def materializable_assertions_for_entity(
        self, subject_uuid: str, *, namespace: str | None = None
    ) -> list[dict[str, Any]]: ...

    def rebind_assertions(
        self, *, absorbed_uuid: str, survivor_uuid: str, merge_op_id: str,
        namespace: str | None = None,
    ) -> dict[str, Any]: ...

    def restore_rebound_assertions(
        self, *, merge_op_id: str, namespace: str | None = None
    ) -> dict[str, Any]: ...

    def record_reconcile_receipt(
        self, *, operation_id: str, operation_kind: str, merge_op_id: str | None = None,
        namespace: str | None = None,
    ) -> None: ...

    def record_reconcile_intent(
        self, *, operation_id: str, operation_kind: str, merge_op_id: str | None = None,
        namespace: str | None = None,
    ) -> None: ...

    def reconcile_complete(
        self, operation_id: str, *, operation_kind: str | None = None,
        namespace: str | None = None, any_namespace: bool = False,
    ) -> bool: ...

    def orphaned_assertions(
        self, *, namespaces: list[str] | None = None, limit: int = 200
    ) -> list[dict[str, Any]]: ...

    def mark_orphan_repair_attempted(
        self, work_items: list[dict[str, Any]], *, at: str
    ) -> int: ...

    def entity_exists(self, uuid: str) -> bool: ...

    def namespaces_for_operation(
        self, *, merge_op_id: str, absorbed_uuid: str | None = None
    ) -> list[str]: ...

    def namespaces_for_unmerge(
        self, *, unmerge_op_id: str, merge_op_id: str
    ) -> list[str]: ...

    def pending_projection_repairs(
        self, *, operation_id: str | None = None, namespaces: list[str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]: ...

    def mark_projection_repair_complete(self, repair_key: str) -> bool: ...

    def claim_due_scalar_activations(
        self, *, as_of: str, namespaces: list[str] | None = None, limit: int = 200,
    ) -> list[dict[str, Any]]: ...


class _ViewSink(Protocol):
    def record_scalar_state(self, **kwargs: Any) -> dict[str, Any]: ...

    def list_scalar_state_views(
        self, *, subject_uuid: str, namespace: str | None = None
    ) -> list[dict[str, Any]]: ...

    def retire_scalar_state(self, *, view_key: str) -> bool: ...

    # scalar_history methods — optional (checked with hasattr for backward compat).
    def record_scalar_history(self, **kwargs: Any) -> dict[str, Any]: ...

    def list_scalar_history_views(
        self, *, subject_uuid: str, namespace: str | None = None
    ) -> list[dict[str, Any]]: ...

    def retire_scalar_history(self, *, view_key: str) -> bool: ...

    def draw_scalar_history_entries(
        self, *, view_uuid: str, entries: list[dict[str, Any]],
    ) -> dict[str, int]: ...


def _slot_of_view(v: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(v.get("attribute", "")).strip().lower(),
        str(v.get("scope", "")).strip().lower(),
        str(v.get("value_kind", "")).strip().lower(),
        str(v.get("unit", "") or "").strip().lower(),
    )


def _retirement_reason(vslot: tuple[str, str, str, str], result: FoldResult) -> str:
    """Why a current View's slot is absent from the freshly-folded desired set.

    Compares the 4-tuple view slot (attribute, scope, value_kind, unit) against the
    5-tuple Expiry/Abstention slot_keys, dropping the leading subject_uuid (both sides are
    scoped to one subject, so discarding it cannot cross subjects)."""
    if any(tuple(e.slot_key[1:]) == vslot for e in result.expiries):
        return "expiry"
    if any(tuple(a.slot_key[1:]) == vslot for a in result.abstentions):
        return "abstain"
    return "vanished"


def _blocked_slots(result: FoldResult) -> set[tuple[str, str, str, str]]:
    """Slots whose history projection is blocked by a malformed source time (MALFORMED_VALID_AT).

    Emitted as 4-tuples in the view-slot coordinate system (attribute, scope, value_kind,
    unit): keep the trailing four elements of the 5-tuple slot_key, dropping only
    subject_uuid, so the elements carry the unit and stay comparable module-wide."""
    return {
        tuple(a.slot_key[1:5]) for a in result.abstentions
        if a.reason == MALFORMED_VALID_AT
    }


def _projection_complete(result: Any) -> bool:
    """Return whether a rebuild result is safe to use as a durable completion proof."""
    if not isinstance(result, dict) or result.get("complete") is False:
        return False
    history = result.get("history")
    return history is None or (isinstance(history, dict) and history.get("complete") is True)
