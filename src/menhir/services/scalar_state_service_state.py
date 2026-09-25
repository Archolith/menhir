"""Scalar_state projection half of the scalar_state_service facade.

`ScalarStateProjectionMixin` carries the pure read+fold entrypoints (`fold_entity`,
`current_authority`, `current_expiries`) and `rebuild_scalar_state`, the AUTHORITATIVE
upsert/reconcile loop over the entity's current Views. The mixin is composed into
`ScalarStateService` by the facade module; behavior is unchanged by the split.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from menhir.domain.scalar_state_fold import Expiry, FoldResult, fold_assertions
from menhir.infrastructure import consolidation_audit as _audit
from menhir.services.scalar_state_service_common import _retirement_reason, _slot_of_view

# Preserve the facade module's logger name so log filtering is unaffected by the split.
logger = logging.getLogger("menhir.services.scalar_state_service")


class ScalarStateProjectionMixin:
    """Pure fold + authoritative scalar_state rebuild methods for ScalarStateService."""

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
