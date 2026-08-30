"""ScalarState + ScalarHistory fold/rebuild orchestration.

`rebuild_scalar_state(subject_uuid)` reads the entity's CURRENT, fully-bound :TypedAssertion rows,
runs the pure deterministic fold (`domain.scalar_state_fold.fold_assertions`), and AUTHORITATIVELY
upserts one kind='scalar_state' View per non-abstained slot via `record_scalar_state` (which writes
in projection-authoritative mode: it bypasses the incremental LWW guard so a correction can move the
current value backward in time, and fully re-renders derived fields). It then reconciles: any current
View whose slot is absent from the freshly-folded desired set is retired. Because the same pure fold
backs both the live ingest path and this replay, rebuilding from the durable log reproduces the live
View exactly.

`rebuild_scalar_history(subject_uuid)` reads the same materializable assertions and builds advisory,
ordered scalar_history Views per slot — including delta-only slots that scalar_state correctly
refuses to ground. See `domain.scalar_history.build_history`.

`rebuild_scalar_projections(subject_uuid)` is the coordinator: it runs both enabled projections and
clears projection_pending only after all succeed. Callers that need both projections rebuilt should
prefer this method. The individual methods remain as compatibility entrypoints.

Authority's source of truth is the EVENT LOG, read at recall time via `current_authority()` (the
weakest-contributor effective tier per slot) — never the View's stamped audit receipt. The receipt is
a best-effort snapshot kept fresh for observability; Piece D must resolve authority through
`current_authority()`, not by trusting node props.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Protocol

from menhir.domain.scalar_history import MALFORMED_VALID_AT, build_history
from menhir.domain.scalar_state_fold import Expiry, FoldResult, fold_assertions
from menhir.infrastructure import consolidation_audit as _audit
from menhir.clock import utc_now_iso as _utc_now_iso

logger = logging.getLogger(__name__)




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


class ScalarStateService:
    """Fold durable assertions into ScalarStateViews and rebuild them deterministically."""

    def __init__(
        self, assertions: _AssertionSource, views: _ViewSink, *,
        scalar_history_enabled: bool = False,
    ) -> None:
        self._assertions = assertions
        self._views = views
        self._scalar_history_enabled = scalar_history_enabled

    def fold_entity(
        self, subject_uuid: str, *, namespace: str | None = None, as_of: datetime | None = None,
    ) -> FoldResult:
        """Pure read+fold (no writes): the current folded state per slot for an entity. Reads ONLY
        materializable (fully-bound, current) assertions — a binding_pending row must never
        materialize a View. `namespace` (C.4.4) scopes the fold to ONE silo so it never mixes two
        tenants' assertions sharing a subject_uuid; None = all (unchanged behavior). `as_of` (Phase B)
        is the evaluation time: future assertions (`valid_at > as_of`) are ignored; None = now(UTC)."""
        rows = self._assertions.materializable_assertions_for_entity(subject_uuid, namespace=namespace)
        return fold_assertions(rows, as_of=as_of)

    def current_authority(
        self, subject_uuid: str, *, namespace: str | None = None, as_of: datetime | None = None,
    ) -> dict[tuple[str, str, str, str], str]:
        """Read-time authority SSOT: the EFFECTIVE evidence tier per slot, computed from the current
        event log (not the View's stamped snapshot). Piece D reads authority through THIS, so a
        stale receipt on a node can never grant authority the fold does not currently support."""
        # Authority is always a real-time read unless the caller explicitly asks for another lens.
        # Never let as_of=None mean "fold the future" at this boundary (G19).
        result = self.fold_entity(
            subject_uuid, namespace=namespace, as_of=as_of or datetime.now(timezone.utc))
        return {
            (s.attribute, s.scope, s.value_kind, s.unit): s.effective_tier
            for s in result.states
        }

    def current_expiries(
        self, subject_uuid: str, *, namespace: str | None = None, as_of: datetime | None = None,
    ) -> dict[tuple[str, str, str, str], "Expiry"]:
        """Read-time expiry SSOT (G13): the Expiry per slot whose value has ENDED with no current
        replacement ("I used to X"). Mirrors current_authority; keyed by (attribute, scope, value_kind,
        unit). An expired slot has NO current View by design (the fold retires it), so recall surfaces
        the EXPIRY VERDICT (last-known value + date + current-UNKNOWN) instead of letting the old
        observations read as current."""
        result = self.fold_entity(
            subject_uuid, namespace=namespace, as_of=as_of or datetime.now(timezone.utc))
        return {
            (e.slot_key[1], e.slot_key[2], e.slot_key[3], e.slot_key[4]): e
            for e in result.expiries
        }

    def rebuild_scalar_state(
        self, subject_uuid: str, *, namespace: str | None = None, source: str = "scalar-state",
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        """Rebuild every ScalarStateView for an entity from its durable assertions AND reconcile:
        retire any current View whose slot is absent from the freshly-folded desired set (a slot
        that vanished, moved via a semantic correction, or now abstains). `namespace` scopes BOTH the
        fold read and the View write/retire to one silo (C.4.4), so a scoped rebuild never folds or
        retires another tenant's state; None = all. `as_of` (Phase B) is the evaluation time, threaded
        through the single fold so live and rebuild are identical for the same (rows, as_of). `None` = no
        time filter (a pure replay of the given rows, backward-compatible) -- the live activation default
        (now(UTC)) is deliberately NOT flipped here; it is opt-in until the perceiver produces real future
        `valid_at`s to filter, per the Phase B plan. Returns {written, retired, abstained, expired,
        results}. Idempotent."""
        result = self.fold_entity(subject_uuid, namespace=namespace, as_of=as_of)

        desired_slots: set[tuple[str, str, str, str]] = set()
        written: list[dict[str, Any]] = []
        stale_skipped: list[dict[str, Any]] = []
        for state in result.states:
            slot = (state.attribute, state.scope, state.value_kind, state.unit)
            audit = {
                "scalar_contributors": list(state.contributor_ids),
                "scalar_effective_tier": state.effective_tier,
                "scalar_anchor_value": str(state.anchor_value),
                "scalar_delta_total": state.delta_total,
            }
            res = self._views.record_scalar_state(
                subject=state.subject_display, subject_uuid=state.subject_uuid,
                attribute=state.attribute, scope=state.scope, value_kind=state.value_kind,
                unit=state.unit, value=state.value, display=None, namespace=namespace,
                valid_at=state.valid_at, source=source, audit=audit,
                episode_uuids=list(state.episode_uuids),
            )
            if res.get("stale_skipped"):
                # Authoritative rebuild must never be LWW-rejected; if it somehow is, the slot is NOT
                # correctly materialized, so it must NOT count as written and must NOT protect the
                # (stale) View from retirement — surface it instead of concealing the failure.
                stale_skipped.append({"slot_key": list(slot), "view_key": res.get("view_key")})
                _audit.audit(
                    "view_write", "stale_skipped", namespace=namespace,
                    subject_uuid=subject_uuid, slot=slot,
                    details={"value": state.value, "valid_at": state.valid_at,
                             "view_key": res.get("view_key")},
                )
                continue
            desired_slots.add(slot)
            written.append({
                "view_key": res.get("view_key"), "value": state.value,
                "contributor_ids": list(state.contributor_ids),
                "effective_tier": state.effective_tier,
            })
            # Distinguish every non-stale-skipped outcome: superseded (new current over old),
            # created (first version), deduped (a concurrent worker won ss_view_key_current -> we
            # CONVERGED on its node, which may or may not carry our value), else unchanged (sig match).
            if res.get("superseded"):
                _write_state = "superseded"
            elif res.get("created"):
                _write_state = "created"
            elif res.get("deduped"):
                _write_state = "deduped"
            else:
                _write_state = "unchanged"
            _audit.audit(
                "view_write", _write_state,
                namespace=namespace, subject_uuid=subject_uuid, slot=slot,
                details={"value": state.value, "valid_at": state.valid_at,
                         "effective_tier": state.effective_tier, "view_key": res.get("view_key"),
                         "uuid": res.get("uuid"), "winner_sig": res.get("winner_sig")},
            )

            # Phase 3 (7.D/G11): draw the View's provenance edges from the fold's algebra (anchor +
            # live deltas + excluded prior anchors), atomically rewritten each rebuild so they never
            # lie or double up. Guarded on the View uuid + an anchor; a mid-rebuild crash self-heals on
            # the next rebuild's redraw. Best-effort: an edge-draw failure must not fail the rebuild
            # (the View + its stamped provenance list are already durable).
            view_uuid = res.get("uuid")
            if view_uuid and state.anchor_id and hasattr(self._views, "draw_scalar_state_provenance_edges"):
                try:
                    self._views.draw_scalar_state_provenance_edges(
                        view_uuid=str(view_uuid), anchor_id=state.anchor_id,
                        contributed_delta_ids=list(state.contributed_delta_ids),
                        superseded_anchor_ids=list(state.superseded_anchor_ids),
                    )
                except Exception:  # noqa: BLE001 - provenance edges are advisory; never fail a rebuild
                    logger.exception(
                        "scalar_state provenance-edge draw failed for view %s (non-fatal)", view_uuid)

        # Reconcile: retire current scalar Views whose slot is no longer desired. This covers a
        # vanished slot, an abstaining slot (its fold produced no state), a semantic correction that
        # moved a claim to a different slot (owned -> sold), and an EXPIRED slot ("I used to X": the
        # fold yields an Expiry, not a state, so the slot is absent from desired_slots and its View is
        # retired with NO replacement — expired-with-current-unknown). Idempotent.
        retired: list[str] = []
        for view in self._views.list_scalar_state_views(subject_uuid=subject_uuid, namespace=namespace):
            vslot = _slot_of_view(view)
            if vslot not in desired_slots:
                if self._views.retire_scalar_state(view_key=str(view.get("view_key"))):
                    retired.append(str(view.get("view_key")))
                    # The load-bearing "why did the current View vanish" signal: the slot folded to no
                    # desired state (abstention / expiry / vanished / moved), so its View is retired with
                    # NO replacement. Record the reason class so a replay explains it without log spelunk.
                    _reason = _retirement_reason(vslot, result)
                    _audit.audit(
                        "reconcile_retire", _reason, namespace=namespace,
                        subject_uuid=subject_uuid, slot=vslot,
                        details={"view_key": view.get("view_key"), "retired_value": view.get("ss_value")},
                    )

        for _ab in result.abstentions:
            _audit.audit("fold", "abstain", namespace=namespace, subject_uuid=subject_uuid,
                         slot=_ab.slot_key, details={"reason": _ab.reason})
        for _ex in result.expiries:
            _audit.audit("fold", "expiry", namespace=namespace, subject_uuid=subject_uuid,
                         slot=_ex.slot_key, details={"expired_value": _ex.expired_value, "valid_at": _ex.valid_at})
        _audit.audit(
            "rebuild", "done", namespace=namespace, subject_uuid=subject_uuid,
            details={"written": len(written), "retired": len(retired),
                     "abstained": len(result.abstentions), "expired": len(result.expiries),
                     "stale_skipped": len(stale_skipped)},
        )

        return {
            "subject_uuid": subject_uuid,
            "complete": not stale_skipped,
            "written": len(written),
            "retired": len(retired),
            "stale_skipped": stale_skipped,
            "abstained": [{"slot_key": list(a.slot_key), "reason": a.reason} for a in result.abstentions],
            "expired": [
                {"slot_key": list(e.slot_key), "expired_value": e.expired_value, "valid_at": e.valid_at}
                for e in result.expiries
            ],
            "results": written,
        }

    # ---- scalar_history projection (advisory ordered history per slot) ---------

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

    # ---- merge lifecycle (Piece C.3) ---------------------------------------------------------

    def handle_merge(
        self, *, absorbed_uuid: str, survivor_uuid: str, merge_op_id: str,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """After an ENTITY_MERGE commits, reconcile scalar state onto the survivor. Order matters:
        (1) RETIRE every scalar View still keyed on the absorbed (now-deleted) entity; (2) REBIND
        the absorbed entity's assertions onto the survivor, journaled per merge_op_id (event-log
        rebinding, not key rewrite, so a shared slot can't collide on a View key); (3) REBUILD the
        survivor's Views (the fold resolves overlapping slots by latest anchor). Only after ALL
        THREE succeed is a :ScalarReconcile receipt written — so a crash between rebind and rebuild
        (which leaves no dead subject_uuid for the orphan scan) is still found by the repair pass as
        a committed merge WITHOUT a receipt. Idempotent."""
        retired_absorbed: list[str] = []
        for view in self._views.list_scalar_state_views(subject_uuid=absorbed_uuid, namespace=namespace):
            if self._views.retire_scalar_state(view_key=str(view.get("view_key"))):
                retired_absorbed.append(str(view.get("view_key")))
        # Retire absorbed entity's scalar_history Views too (stale current history on a dead entity).
        retired_history: list[str] = []
        if hasattr(self._views, "list_scalar_history_views"):
            for view in self._views.list_scalar_history_views(
                    subject_uuid=absorbed_uuid, namespace=namespace):
                if self._views.retire_scalar_history(view_key=str(view.get("view_key"))):
                    retired_history.append(str(view.get("view_key")))
        rebound = self._assertions.rebind_assertions(
            absorbed_uuid=absorbed_uuid, survivor_uuid=survivor_uuid, merge_op_id=merge_op_id,
            namespace=namespace)
        rebuild = self.rebuild_scalar_projections(
            survivor_uuid, namespace=namespace,
            history_enabled=self._scalar_history_enabled)
        # Receipt is the LAST step: its presence means every stage completed. Keyed by the merge's
        # OWN op_id + ENTITY_MERGE, so the repair pass can find a committed merge without one.
        # NAMESPACE-KEYED receipt (C.4.4): this reconciliation covered exactly ONE silo, so its proof
        # of completion is scoped to that silo. A global receipt would let a tenant-A success certify a
        # tenant-B failure of the same op and permanently mask tenant-B's missing View.
        # Do NOT record the receipt if history rebuild errored — leave receiptless for scheduler retry.
        history = rebuild.get("history")
        history_errored = not _projection_complete(rebuild)
        if not history_errored:
            self._assertions.record_reconcile_receipt(
                operation_id=merge_op_id, operation_kind="ENTITY_MERGE", namespace=namespace)
        else:
            logger.warning(
                "merge reconciliation for %s: history rebuild failed; receipt withheld for retry",
                merge_op_id)
        _audit.audit(
            "merge", "reconciled" if not history_errored else "partial",
            namespace=namespace, subject_uuid=survivor_uuid,
            details={"absorbed_uuid": absorbed_uuid, "merge_op_id": merge_op_id,
                     "retired_absorbed": len(retired_absorbed),
                     "retired_history": len(retired_history),
                     "rebound": rebound["rebound"],
                     "history_errored": history_errored},
        )
        return {
            "absorbed_uuid": absorbed_uuid, "survivor_uuid": survivor_uuid,
            "merge_op_id": merge_op_id,
            "retired_absorbed": len(retired_absorbed),
            "retired_history": len(retired_history),
            "rebound": rebound["rebound"],
            "survivor_rebuild": rebuild,
            "history_errored": history_errored,
        }

    def handle_unmerge(
        self, *, survivor_uuid: str, absorbed_uuid: str, merge_op_id: str,
        unmerge_op_id: str, namespace: str | None = None,
    ) -> dict[str, Any]:
        """Inverse of `handle_merge`, scoped to the SAME merge_op_id whose rebind this reverses: move
        exactly that op's rebound assertions back to their recorded owners, then rebuild every
        affected entity. In a chain A->B->C, unmerging op2 (C->B) returns op2's assertions (A-origin
        AND B-native) to B; a later unmerge of op1 (B->A) returns only op1's (A-origin) to A. The
        receipt is keyed by the UNMERGE's OWN op_id + ENTITY_UNMERGE (the forward merge is marked
        REVERSED, so a merge-only scan would never revisit it — blocker 3). Idempotent.

        INTENT BEFORE RESTORE: `restore_rebound_assertions` deletes the forward op's :AssertionRebind
        records, which are otherwise the only durable carrier of this unmerge's namespace. Writing the
        pending marker FIRST means a restore-succeeded/rebuild-failed crash still leaves discoverable
        namespace evidence, so the retry finds the receiptless unmerge instead of skipping it forever.
        Namespace evidence must never be consumed before the namespace-keyed receipt is complete."""
        self._assertions.record_reconcile_intent(
            operation_id=unmerge_op_id, operation_kind="ENTITY_UNMERGE", merge_op_id=merge_op_id,
            namespace=namespace)
        restored = self._assertions.restore_rebound_assertions(
            merge_op_id=merge_op_id, namespace=namespace)
        rebuilt: dict[str, Any] = {}
        history_errored = False
        for uuid in {survivor_uuid, absorbed_uuid, *restored.get("from_uuids", [])}:
            r = self.rebuild_scalar_projections(
                uuid, namespace=namespace,
                history_enabled=self._scalar_history_enabled)
            rebuilt[uuid] = r
            if not _projection_complete(r):
                history_errored = True
        # Do NOT record the receipt if any entity's history rebuild errored — leave receiptless
        # for scheduler retry (state rebuilds are idempotent).
        if not history_errored:
            self._assertions.record_reconcile_receipt(
                operation_id=unmerge_op_id, operation_kind="ENTITY_UNMERGE", merge_op_id=merge_op_id,
                namespace=namespace)
        else:
            logger.warning(
                "unmerge reconciliation for %s: history rebuild failed on one or more entities; "
                "receipt withheld for retry", unmerge_op_id)
        _audit.audit(
            "unmerge", "reconciled" if not history_errored else "partial",
            namespace=namespace, subject_uuid=survivor_uuid,
            details={"absorbed_uuid": absorbed_uuid, "merge_op_id": merge_op_id,
                     "unmerge_op_id": unmerge_op_id, "restored": restored["restored"],
                     "rebuilt_uuids": sorted(rebuilt.keys()),
                     "history_errored": history_errored},
        )
        return {
            "survivor_uuid": survivor_uuid, "absorbed_uuid": absorbed_uuid,
            "merge_op_id": merge_op_id, "unmerge_op_id": unmerge_op_id,
            "restored": restored["restored"], "rebuilt": {u: r for u, r in rebuilt.items()},
            "history_errored": history_errored,
        }

    @staticmethod
    def _downstream_index(merges: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        """Index committed merges by their ABSORBED uuid. A merge chain A->B->C is expressed as
        op1{absorbed:A, survivor:B} and op2{absorbed:B, survivor:C}: op2 is DOWNSTREAM of op1 because
        op2.absorbed == op1.survivor. So `index[survivor]` yields the ops to replay after a given op."""
        index: dict[str, list[dict[str, Any]]] = {}
        for op in merges:
            if str(op.get("op_id") or ""):
                index.setdefault(str(op.get("absorbed_uuid")), []).append(op)
        return index

    def _resolve_unmerge_namespaces(
        self, *, unmerge_op_id: str, merge_op_id: str, allowed: set[str] | None,
    ) -> list[str | None]:
        """Namespaces an unmerge affects: the forward merge's surviving rebind records UNION this
        unmerge's own reconcile markers. The marker source is what survives a
        restore-succeeded/rebuild-failed crash (restore deletes the rebind records), keeping a
        receiptless unmerge discoverable on the next scheduled pass. Filtered by the allowlist."""
        nss = self._assertions.namespaces_for_unmerge(
            unmerge_op_id=unmerge_op_id, merge_op_id=merge_op_id)
        if allowed is not None:
            nss = [ns for ns in nss if ns in allowed]
        return list(nss)

    def _resolve_op_namespaces(
        self, *, merge_op_id: str, absorbed_uuid: str | None, allowed: set[str] | None,
    ) -> list[str | None]:
        """Namespaces a lifecycle op AFFECTS, derived from the op's OWN assertions (its
        :AssertionRebind records + any assertions still on the absorbed uuid) — never from
        survivor-native rows, so a tenant-A merge whose survivor holds unrelated tenant-B assertions
        is a tenant-A op, not a multi-namespace violation. An op legitimately spanning TWO silos yields
        BOTH, and each is reconciled + receipted INDEPENDENTLY (one silo's success cannot certify the
        other). Filtered by an explicit allowlist. Empty = nothing of ours to do."""
        nss = self._assertions.namespaces_for_operation(
            merge_op_id=merge_op_id, absorbed_uuid=absorbed_uuid)
        if allowed is not None:
            nss = [ns for ns in nss if ns in allowed]
        return list(nss)

    def _replay_merge_chain(
        self, op: dict[str, Any], *, downstream: dict[str, list[dict[str, Any]]],
        visited: set[tuple[str | None, str]], namespace: str | None,
        repaired: list[str], errored: list[str], downstream_replayed: list[str],
    ) -> None:
        """Re-run one merge's reconciliation, then WALK FORWARD along the merge lineage and re-run
        every downstream merge — EVEN THOSE WITH A RECEIPT (carried C.3 obligation): if A->B (op1)
        failed but B->C (op2) succeeded, repairing op1 rebinds A's assertions onto B and op2's receipt
        is now stale. `handle_merge` is idempotent, so replaying a completed downstream op is safe.
        `visited` is keyed by (namespace, op_id) so the same op can be reconciled independently per
        silo. PER-OP ISOLATION: an op whose reconciliation raises is recorded in `errored` and its
        downstream is NOT traversed (its assertions did not move), but sibling chains proceed. The root
        (first) op goes to `repaired`; successfully-replayed downstream ops to `downstream_replayed`."""
        queue: list[tuple[dict[str, Any], bool]] = [(op, False)]
        while queue:
            cur, is_down = queue.pop(0)
            op_id = str(cur.get("op_id") or "")
            key = (namespace, op_id)
            if not op_id or key in visited:
                continue
            visited.add(key)
            try:
                self.handle_merge(
                    absorbed_uuid=str(cur.get("absorbed_uuid")),
                    survivor_uuid=str(cur.get("survivor_uuid")),
                    merge_op_id=op_id, namespace=namespace)
            except Exception:  # noqa: BLE001 - isolate one op; do not traverse its (unmoved) downstream
                errored.append(op_id)
                logger.exception(
                    "reconciliation errored on merge op_id=%s (ns=%s); leaving for retry",
                    op_id, namespace)
                continue
            (downstream_replayed if is_down else repaired).append(op_id)
            for down in downstream.get(str(cur.get("survivor_uuid")), []):
                if (namespace, str(down.get("op_id") or "")) not in visited:
                    queue.append((down, True))

    def repair_incomplete_reconciliations(
        self, *, committed_merges: list[dict[str, Any]] | None = None,
        committed_unmerges: list[dict[str, Any]] | None = None,
        allowed_namespaces: set[str] | None = None,
    ) -> dict[str, Any]:
        """Repair pass (blocker 3/4 + C.4.4 transitive): re-run scalar reconciliation for every
        committed lifecycle op lacking a receipt of its OWN kind — covering the rebind-ok/rebuild-failed
        merge (no dead subject_uuid) AND the committed-unmerge-whose-scalar-hook-failed. Each op's
        NAMESPACE is derived fail-closed from its own assertions (not a caller default), scoping rebind/
        fold/rebuild to that silo; an op outside an explicit `allowed_namespaces` allowlist is skipped,
        and an op spanning >1 namespace is errored (never guessed). TRANSITIVE: re-running a merge also
        replays every downstream merge in its lineage. PER-OP ISOLATION: one failing op is recorded in
        `errored_*` (no receipt) while unrelated roots proceed. Inputs come from the operations journal:
        committed_merges [{op_id, absorbed_uuid, survivor_uuid}] (FULL lineage), committed_unmerges
        [{op_id, merge_op_id, absorbed_uuid, survivor_uuid}]. Idempotent."""
        merges = [op for op in (committed_merges or []) if str(op.get("op_id") or "")]
        downstream = self._downstream_index(merges)
        visited: set[tuple[str | None, str]] = set()
        repaired_merges: list[str] = []
        errored_merges: list[str] = []
        downstream_replayed: list[str] = []
        for op in merges:
            op_id = str(op.get("op_id"))
            # Each affected silo is checked and repaired INDEPENDENTLY: a receipt for tenant-A does not
            # mark tenant-B complete, so a partially-failed multi-silo op is still found.
            for ns in self._resolve_op_namespaces(
                    merge_op_id=op_id, absorbed_uuid=str(op.get("absorbed_uuid")),
                    allowed=allowed_namespaces):
                if self._assertions.reconcile_complete(
                        op_id, operation_kind="ENTITY_MERGE", namespace=ns):
                    continue
                if (ns, op_id) in visited:
                    continue
                self._replay_merge_chain(
                    op, downstream=downstream, visited=visited, namespace=ns,
                    repaired=repaired_merges, errored=errored_merges,
                    downstream_replayed=downstream_replayed)
        repaired_unmerges: list[str] = []
        errored_unmerges: list[str] = []
        for op in committed_unmerges or []:
            op_id = str(op.get("op_id") or "")
            if not op_id:
                continue
            # an unmerge's affected silos come from the FORWARD merge's rebind records (the exact rows
            # it will restore) UNION its own reconcile markers — never from either entity's current
            # assertions. The marker source covers the restore-ok/rebuild-failed crash, where restore
            # already deleted the rebind records but the op is still receiptless.
            for ns in self._resolve_unmerge_namespaces(
                    unmerge_op_id=op_id, merge_op_id=str(op.get("merge_op_id")),
                    allowed=allowed_namespaces):
                if self._assertions.reconcile_complete(
                        op_id, operation_kind="ENTITY_UNMERGE", namespace=ns):
                    continue
                try:
                    self.handle_unmerge(
                        survivor_uuid=str(op.get("survivor_uuid")),
                        absorbed_uuid=str(op.get("absorbed_uuid")),
                        merge_op_id=str(op.get("merge_op_id")), unmerge_op_id=op_id, namespace=ns)
                    repaired_unmerges.append(op_id)
                except Exception:  # noqa: BLE001 - isolate one unmerge; others + orphan pass proceed
                    errored_unmerges.append(op_id)
                    logger.exception(
                        "reconciliation errored on unmerge op_id=%s (ns=%s); leaving for retry",
                        op_id, ns)
        # downstream ops replayed only because an upstream op was repaired (excl. those that are roots).
        downstream_only = sorted(set(downstream_replayed) - set(repaired_merges))
        return {"repaired": len(repaired_merges) + len(repaired_unmerges),
                "merge_op_ids": repaired_merges, "unmerge_op_ids": repaired_unmerges,
                "errored_merge_op_ids": sorted(set(errored_merges)),
                "errored_unmerge_op_ids": sorted(set(errored_unmerges)),
                "downstream_replayed_op_ids": downstream_only}

    @staticmethod
    def _terminal_survivor(op: dict[str, Any], op_by_absorbed: dict[str, dict[str, Any]]) -> str:
        """Walk a merge chain forward to its FINAL survivor. `op_by_absorbed` maps an absorbed uuid to
        the op that absorbed it, so if survivor S was itself later absorbed, follow to that op's
        survivor. Cycle-guarded."""
        seen: set[str] = set()
        survivor = str(op.get("survivor_uuid") or "")
        while survivor in op_by_absorbed and survivor not in seen:
            seen.add(survivor)
            survivor = str(op_by_absorbed[survivor].get("survivor_uuid") or "")
        return survivor

    def repair_orphaned_assertions(
        self, *, committed_merges: list[dict[str, Any]] | None = None,
        allowed_namespaces: set[str] | None = None, limit: int = 200,
    ) -> dict[str, Any]:
        """Scheduled orphan-rebind pass (carried C.3 obligation): resolve assertions whose subject
        Entity no longer exists — orphaned by a merge whose post-commit rebind never ran — to their
        surviving entity via merge lineage, then rebind + rebuild TRANSITIVELY along the chain. Each
        orphan work item is `{subject_uuid, namespace}`; the View is rebuilt/retired in the orphan's OWN
        durable namespace (never the default silo). `allowed_namespaces` is a fail-closed allowlist
        (also applied in the query). `committed_merges` is the FULL merge lineage (its `limit` is a
        history cap, independent of the orphan work `limit`). FAIL-CLOSED SURVIVOR: the chain's terminal
        survivor Entity must exist — a chain ending at another deleted entity stays `unresolved`, never
        reported repaired against a dead uuid. An orphan with no lineage is `unresolved` too. Per-row
        exceptions are isolated (`errored`). EVERY examined orphan is stamped so a bounded scan is
        eventually complete. Idempotent."""
        ns_arg = sorted(allowed_namespaces) if allowed_namespaces is not None else None
        work = self._assertions.orphaned_assertions(namespaces=ns_arg, limit=limit)
        if not work:
            return {"orphans": 0, "repaired": [], "unresolved": [], "errored": [],
                    "replayed_op_ids": []}
        # Stamp EVERY examined orphan up front — BEFORE the repair relocates it. The stamp query
        # matches by (subject_uuid, namespace); a successful rebind moves the assertion off its dead
        # subject_uuid onto the live survivor, so a stamp applied AFTER the repair would silently match
        # nothing for exactly the repaired rows (the common success case) and never advance their
        # frontier. Rebind only sets subject_uuid/rebound_at, so the stamp rides along on the moved
        # node. Stamping here (not per-outcome) keeps the contract literal: regardless of
        # repaired/unresolved/errored, an examined orphan is stamped so a bounded scan is eventually
        # complete and no sibling starves. Keyed by (subject_uuid, namespace) so tenant-A never stamps
        # tenant-B on a shared dead uuid.
        examined = [
            {"subject_uuid": str(item.get("subject_uuid") or ""), "namespace": item.get("namespace")}
            for item in work if str(item.get("subject_uuid") or "")
        ]
        if examined:
            self._assertions.mark_orphan_repair_attempted(examined, at=_utc_now_iso())
        merges = [op for op in (committed_merges or []) if str(op.get("op_id") or "")]
        downstream = self._downstream_index(merges)
        op_by_absorbed: dict[str, dict[str, Any]] = {}
        for op in merges:
            op_by_absorbed.setdefault(str(op.get("absorbed_uuid")), op)   # the op that absorbed it
        visited: set[tuple[str | None, str]] = set()
        replayed: list[str] = []
        chain_errored: list[str] = []
        repaired: list[str] = []
        unresolved: list[str] = []
        errored: list[str] = []
        for item in work:
            orphan = str(item.get("subject_uuid") or "")
            row_ns = item.get("namespace")
            if not orphan:
                continue
            if allowed_namespaces is not None and row_ns not in allowed_namespaces:
                unresolved.append(orphan)   # defensive: never touch a row outside the allowlist
                continue
            op = op_by_absorbed.get(orphan)
            if op is None:
                unresolved.append(orphan)   # no lineage -> cannot resolve a survivor
                continue
            # fail-closed survivor: the terminal survivor Entity must exist. Checked UUID-GLOBALLY,
            # matching assertion binding's identity semantics — canonical entities are not
            # tenant-scoped; namespace applies to the assertions and Views we then rebuild.
            terminal = self._terminal_survivor(op, op_by_absorbed)
            if not terminal or not self._assertions.entity_exists(terminal):
                unresolved.append(orphan)   # chain ends at a dead entity -> leave for an operator
                continue
            before_err = len(chain_errored)
            self._replay_merge_chain(
                op, downstream=downstream, visited=visited, namespace=row_ns,
                repaired=[], errored=chain_errored, downstream_replayed=replayed)
            if len(chain_errored) > before_err:
                errored.append(orphan)      # this orphan's chain hit a per-op failure
            else:
                repaired.append(orphan)
        return {"orphans": len(work), "repaired": repaired, "unresolved": unresolved,
                "errored": errored, "replayed_op_ids": sorted(op for _ns, op in visited)}
