"""scalar_history projection + coordinator/scheduling half of the scalar_state_service facade.

`ScalarHistoryProjectionMixin` carries `rebuild_scalar_history` (advisory ordered history per
slot, fail-closed), the `rebuild_scalar_projections` coordinator, and the deletion-repair /
due-activation scheduling methods (`repair_pending_deletions`, `activate_due_assertions`).
The mixin is composed into `ScalarStateService` by the facade module.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from menhir.domain.scalar_history import build_history
from menhir.infrastructure import consolidation_audit as _audit
from menhir.services.scalar_state_service_common import (
    _blocked_slots,
    _projection_complete,
    _slot_of_view,
)

# Preserve the facade module's logger name so log filtering is unaffected by the split.
logger = logging.getLogger("menhir.services.scalar_state_service")


class ScalarHistoryProjectionMixin:
    """scalar_history projection + projection-coordinator methods for ScalarStateService."""

    def rebuild_scalar_history(
        self, subject_uuid: str, *, namespace: str | None = None,
        source: str = "scalar-history", as_of: datetime | None = None,
        max_entries: int = 16,
    ) -> dict[str, Any]:
        """Rebuild every scalar_history View for an entity from its durable assertions AND reconcile:
        retire any current history View whose slot is absent from the freshly-projected desired set.

        Advisory: never writes scalar_state, never enters the authority lane.

        Completion is fail-closed: a write, normalization, edge-draw, or exact-count failure
        returns ``complete=False`` and leaves the durable projection work retryable.  Existing
        current Views are not reconciled away when a slot abstains for malformed source time.
        """
        if not hasattr(self._views, "record_scalar_history"):
            return {
                "subject_uuid": subject_uuid, "skipped": True,
                "complete": False, "reason": "sink_unsupported",
            }

        try:
            rows = self._assertions.materializable_assertions_for_entity(
                subject_uuid, namespace=namespace)
            result = build_history(rows, as_of=as_of, max_entries=max_entries)
        except Exception as exc:  # noqa: BLE001 - durable marker must remain retryable
            logger.exception("scalar_history source/build failed for %s", subject_uuid)
            return {
                "subject_uuid": subject_uuid, "complete": False, "error": True,
                "reason": f"{type(exc).__name__}: {exc}", "written": 0, "retired": 0,
                "abstained": [], "results": [],
            }

        desired_slots: set[tuple[str, str, str, str]] = set()
        written: list[dict[str, Any]] = []
        failed_slots: list[dict[str, Any]] = []

        for proj in result.projections:
            slot = (proj.attribute, proj.scope, proj.value_kind, proj.unit)

            audit = {
                "scalar_history_entry_count": proj.entry_count,
                "scalar_history_total_entry_count": proj.total_entry_count,
                "scalar_history_payload_entry_count": proj.payload_entry_count,
                "scalar_history_omitted_entry_count": proj.omitted_entry_count,
                # ViewRepository applies audit_props with ``SET n += $extra``; Neo4j node
                # properties cannot contain maps, so keep this duplicate audit snapshot scalar.
                "scalar_history_ops": json.dumps(proj.operation_counts, sort_keys=True),
            }

            try:
                res = self._views.record_scalar_history(
                    subject=proj.subject_display, subject_uuid=proj.subject_uuid,
                    attribute=proj.attribute, scope=proj.scope, value_kind=proj.value_kind,
                    unit=proj.unit, entries=proj.entries,
                    history_signature=proj.history_signature,
                    operation_counts=proj.operation_counts, entry_count=proj.entry_count,
                    payload_entry_count=proj.payload_entry_count,
                    omitted_entry_count=proj.omitted_entry_count,
                    first_valid_at=proj.first_valid_at, last_valid_at=proj.last_valid_at,
                    namespace=namespace, source=source, audit=audit,
                    recallable=self._scalar_history_enabled,
                    episode_uuids=list(proj.episode_uuids),
                )

                # Draw HISTORY_ENTRY provenance edges from the FULL entry set (not the bounded
                # recall payload) so truncated entries retain their assertion provenance.
                view_uuid = res.get("uuid")
                if not view_uuid or not hasattr(self._views, "draw_scalar_history_entries"):
                    raise RuntimeError("scalar_history contributor edge sink is unavailable")
                draw_result = self._views.draw_scalar_history_entries(
                    view_uuid=str(view_uuid), entries=proj.all_entries,
                )
                drawn = int(draw_result.get("history_entries", 0)) if draw_result else 0
                if drawn != proj.total_entry_count:
                    raise RuntimeError(
                        f"scalar_history contributor count mismatch: expected "
                        f"{proj.total_entry_count}, drew {drawn}"
                    )
            except Exception as exc:  # noqa: BLE001 - preserve retryability and receipt markers
                logger.exception("scalar_history projection incomplete for slot %s", slot)
                failed_slots.append({
                    "slot_key": list(proj.slot_key),
                    "error": f"{type(exc).__name__}: {exc}",
                })
                continue

            desired_slots.add(slot)
            written.append({
                "view_key": res.get("view_key"),
                "entry_count": proj.entry_count,
                "payload_entry_count": proj.payload_entry_count,
                "omitted_entry_count": proj.omitted_entry_count,
                "total_entry_count": proj.total_entry_count,
                "signature": proj.history_signature,
            })

            _audit.audit(
                "history_write", "created" if res.get("created") else "unchanged",
                namespace=namespace, subject_uuid=subject_uuid, slot=proj.slot_key,
                details={"entry_count": proj.entry_count,
                         "payload_entry_count": proj.payload_entry_count,
                         "omitted_entry_count": proj.omitted_entry_count,
                         "view_key": res.get("view_key"),
                         "uuid": res.get("uuid")},
            )

        # A malformed slot is an incomplete source projection, not evidence that an existing
        # known-good View vanished.  Keep it current/stale for observability until the next retry;
        # never delete it while claiming this rebuild complete.
        blocked_slots = _blocked_slots(result)

        # Reconcile only after every desired contributor set was durably written.  A partial
        # failure must not retire unrelated current history while the repair marker is pending.
        retired: list[str] = []
        if not failed_slots and not blocked_slots and hasattr(self._views, "list_scalar_history_views"):
            for view in self._views.list_scalar_history_views(
                    subject_uuid=subject_uuid, namespace=namespace):
                vslot = _slot_of_view(view)
                if vslot not in desired_slots:
                    if hasattr(self._views, "retire_scalar_history") and \
                       self._views.retire_scalar_history(view_key=str(view.get("view_key"))):
                        retired.append(str(view.get("view_key")))
                        _audit.audit(
                            "history_reconcile_retire", "vanished",
                            namespace=namespace, subject_uuid=subject_uuid, slot=vslot,
                            details={"view_key": view.get("view_key")},
                        )

        for _ab in result.abstentions:
            _audit.audit("history_fold", "abstain", namespace=namespace,
                         subject_uuid=subject_uuid, slot=_ab.slot_key,
                         details={"reason": _ab.reason,
                                  "malformed_assertion_ids": list(_ab.malformed_assertion_ids)})

        complete = not failed_slots and not blocked_slots

        _audit.audit(
            "history_rebuild", "done" if complete else "incomplete",
            namespace=namespace, subject_uuid=subject_uuid,
            details={"written": len(written), "retired": len(retired),
                     "abstained": len(result.abstentions), "failed_slots": failed_slots},
        )

        return {
            "subject_uuid": subject_uuid,
            "complete": complete,
            "error": not complete,
            "written": len(written),
            "retired": len(retired),
            "failed_slots": failed_slots,
            "abstained": [{"slot_key": list(a.slot_key), "reason": a.reason}
                          | ({"malformed_assertion_ids": list(a.malformed_assertion_ids)}
                             if a.malformed_assertion_ids else {})
                          for a in result.abstentions],
            "results": written,
        }

    # ---- projection coordinator -----------------------------------------------

    def rebuild_scalar_projections(
        self, subject_uuid: str, *, namespace: str | None = None,
        source: str = "scalar-state", as_of: datetime | None = None,
        history_enabled: bool = False,
    ) -> dict[str, Any]:
        """Coordinator: rebuild both scalar_state and scalar_history (when enabled) for an entity.

        This is the preferred entry point for callers that need all enabled projections rebuilt.
        The individual `rebuild_scalar_state` and `rebuild_scalar_history` methods remain as
        compatibility entrypoints.

        `history_enabled` gates whether scalar_history is built. Callers should thread the
        feature flag value from settings.
        """
        state_result = self.rebuild_scalar_state(
            subject_uuid, namespace=namespace, source=source, as_of=as_of)

        history_result: dict[str, Any] | None = None
        if history_enabled:
            try:
                history_result = self.rebuild_scalar_history(
                    subject_uuid, namespace=namespace, source="scalar-history", as_of=as_of)
            except Exception:  # noqa: BLE001 - history is advisory; never fail a state rebuild
                logger.exception(
                    "scalar_history rebuild failed for %s (non-fatal; state projection succeeded)",
                    subject_uuid)
                history_result = {
                    "subject_uuid": subject_uuid, "error": True, "complete": False,
                    "reason": "unexpected history rebuild exception",
                }

        return {
            "subject_uuid": subject_uuid,
            "complete": _projection_complete({"history": history_result, **state_result}),
            "state": state_result,
            "history": history_result,
        }

    def repair_pending_deletions(
        self,
        *,
        operation_id: str | None = None,
        namespaces: list[str] | None = None,
        limit: int = 200,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        """Finish delete-triggered scalar reprojection from durable pending receipts (G20).

        A receipt is completed only after the affected subject+namespace has been re-folded.  A
        failed rebuild leaves it pending for the next immediate retry or scheduler pass.  All rows in
        one pass share one concrete evaluation timestamp so future assertions cannot activate early.
        """
        work = self._assertions.pending_projection_repairs(
            operation_id=operation_id, namespaces=namespaces, limit=limit)
        evaluation_time = as_of or datetime.now(timezone.utc)
        repaired: list[str] = []
        failed: list[dict[str, str]] = []
        for row in work:
            repair_key = str(row.get("repair_key") or "")
            subject_uuid = str(row.get("subject_uuid") or "")
            namespace = row.get("namespace")
            if not repair_key or not subject_uuid:
                failed.append({"repair_key": repair_key, "error": "invalid repair receipt"})
                continue
            try:
                # Deletions ALWAYS rebuild history (cleanup must be complete regardless of
                # the feature flag — a stale history View on deleted data is a data leak).
                result = self.rebuild_scalar_projections(
                    subject_uuid, namespace=namespace, as_of=evaluation_time,
                    source="scalar-delete-repair",
                    # Deletion cleanup is unconditional when the sink exposes history.  A
                    # legacy state-only sink has no stored history surface to clean, so it is
                    # still a valid state-only repair rather than a permanently failing marker.
                    history_enabled=hasattr(self._views, "record_scalar_history"))
                # Do NOT mark complete unless every enabled projection explicitly completed —
                # leave the deletion receipt pending for scheduler retry.
                if not _projection_complete(result):
                    failed.append({
                        "repair_key": repair_key,
                        "error": "projection rebuild incomplete; receipt left pending for retry",
                    })
                    continue
                if self._assertions.mark_projection_repair_complete(repair_key):
                    repaired.append(repair_key)
            except Exception as exc:  # noqa: BLE001 - receipt deliberately remains pending
                logger.exception("scalar deletion repair failed for %s", repair_key)
                failed.append({"repair_key": repair_key, "error": f"{type(exc).__name__}: {exc}"})
        return {"repaired": repaired, "failed": failed, "examined": len(work)}

    def activate_due_assertions(
        self,
        *,
        as_of: datetime | None = None,
        namespaces: list[str] | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Claim future assertions whose valid_at has arrived, then rebuild their projections."""
        evaluation_time = as_of or datetime.now(timezone.utc)
        claimed = self._assertions.claim_due_scalar_activations(
            as_of=evaluation_time.isoformat(), namespaces=namespaces, limit=limit)
        repaired = self.repair_pending_deletions(
            namespaces=namespaces, limit=limit, as_of=evaluation_time)
        return {"claimed": len(claimed), **repaired}
