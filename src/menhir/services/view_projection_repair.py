"""One idempotent pass over durable generic View projection-repair receipts.

Activation blocker: this service is intentionally not advertised as always-on.  The current runtime
does not expose a safe independent scheduler lease for this queue, and several erasure producers do
not yet persist every projection-identity field needed after namespace deletion.  Construct the
service explicitly and call :meth:`run_pending`; runtime registration should follow only after the
producer receipt contract and an independently owned scheduling hook are deployed together.
"""

from __future__ import annotations

from typing import Any, Protocol

from menhir.domain.event_history import EventLane
from menhir.infrastructure.view_projection_repair import ViewProjectionRepairClaim


class RepairStore(Protocol):
    def claim_pending(
        self, *, owner_id: str, limit: int, lease_seconds: int
    ) -> list[ViewProjectionRepairClaim]: ...

    def complete(self, claim: ViewProjectionRepairClaim) -> bool: ...

    def fail(self, claim: ViewProjectionRepairClaim, error: str) -> bool: ...

    def terminal_not_rebuildable(
        self, claim: ViewProjectionRepairClaim, reason: str
    ) -> bool: ...


class ScalarProjectionRebuilder(Protocol):
    def rebuild_scalar_state(
        self, subject_uuid: str, *, namespace: str | None = None, source: str = "scalar-state"
    ) -> dict[str, Any]: ...

    def rebuild_scalar_history(
        self, subject_uuid: str, *, namespace: str | None = None, source: str = "scalar-history"
    ) -> dict[str, Any]: ...


class EventProjectionRebuilder(Protocol):
    def rebuild_lane(self, lane: EventLane, *, source: str = "event-history") -> dict[str, Any]: ...


class ViewProjectionRepairService:
    """Claim, dispatch, and conditionally finalize one bounded queue pass."""

    _TERMINAL_KINDS = frozenset({"admission_audit"})

    def __init__(
        self,
        store: RepairStore,
        *,
        scalar_rebuilder: ScalarProjectionRebuilder,
        event_rebuilder: EventProjectionRebuilder,
    ) -> None:
        self._store = store
        self._scalar = scalar_rebuilder
        self._events = event_rebuilder

    def run_pending(
        self,
        *,
        owner_id: str,
        limit: int = 25,
        lease_seconds: int = 300,
    ) -> dict[str, Any]:
        """Run one bounded, idempotent claim/dispatch pass.

        Repeating the pass is safe: completed/terminal rows are not claimable, failures retain
        retry accounting, and all dispatched rebuild APIs are deterministic.  A stale owner cannot
        finalize after lease loss because every transition is token- and lease-conditional.
        """
        claims = self._store.claim_pending(
            owner_id=owner_id,
            limit=limit,
            lease_seconds=lease_seconds,
        )
        summary: dict[str, Any] = {
            "claimed": len(claims),
            "complete": 0,
            "failed": 0,
            "terminal_not_rebuildable": 0,
            "lost_claim": 0,
            "results": [],
        }
        for claim in claims:
            outcome, detail = self._dispatch(claim)
            transitioned = self._transition(claim, outcome, detail)
            if transitioned:
                summary[outcome] += 1
            else:
                summary["lost_claim"] += 1
            summary["results"].append({
                "repair_key": claim.repair_key,
                "view_kind": claim.view_kind,
                "outcome": outcome if transitioned else "lost_claim",
                "detail": detail,
            })
        return summary

    def _dispatch(self, claim: ViewProjectionRepairClaim) -> tuple[str, str]:
        kind = claim.view_kind.strip().lower()
        try:
            if kind in {"scalar_state", "scalar_history"}:
                missing = self._missing(claim, "view_key", "subject_uuid")
                if missing:
                    return "failed", self._missing_receipt_error(missing)
                if kind == "scalar_state":
                    result = self._scalar.rebuild_scalar_state(
                        claim.subject_uuid,
                        namespace=claim.namespace,
                        source="view-projection-repair",
                    )
                else:
                    result = self._scalar.rebuild_scalar_history(
                        claim.subject_uuid,
                        namespace=claim.namespace,
                        source="view-projection-repair",
                    )
                return self._rebuild_outcome(result)

            if kind == "timeline":
                event_lane = (
                    claim.view_subtype == "event_timeline"
                    or claim.source_family == "typed_event_assertions"
                    or bool(claim.predicate)
                )
                if claim.view_subtype == "legacy_timeline" or not event_lane:
                    return (
                        "terminal_not_rebuildable",
                        "legacy timeline has no deterministic source-log rebuild API",
                    )
                missing = self._missing(claim, "view_key", "subject_uuid", "predicate")
                if missing:
                    return "failed", self._missing_receipt_error(missing)
                result = self._events.rebuild_lane(
                    EventLane(
                        subject_uuid=claim.subject_uuid,
                        predicate=claim.predicate,
                        namespace=claim.namespace,
                        domain=claim.domain or None,
                    ),
                    source="view-projection-repair",
                )
                return self._rebuild_outcome(result)

            if kind == "counter":
                return (
                    "failed",
                    "counter repair requires honest namespace-wide reperception; "
                    "no safe deterministic rebuild API is registered",
                )

            if kind in self._TERMINAL_KINDS:
                return (
                    "terminal_not_rebuildable",
                    f"{kind} is an append-only legacy View without a deterministic rebuild API",
                )

            return (
                "terminal_not_rebuildable",
                f"unsupported View projection kind: {kind or '<missing>'}",
            )
        except Exception as exc:  # noqa: BLE001 - durable queue must record dispatch failures
            return "failed", f"dispatch failed: {type(exc).__name__}: {exc}"

    def _transition(
        self,
        claim: ViewProjectionRepairClaim,
        outcome: str,
        detail: str,
    ) -> bool:
        if outcome == "complete":
            return self._store.complete(claim)
        if outcome == "terminal_not_rebuildable":
            return self._store.terminal_not_rebuildable(claim, detail)
        return self._store.fail(claim, detail)

    @staticmethod
    def _rebuild_outcome(result: dict[str, Any]) -> tuple[str, str]:
        if bool(result.get("complete")):
            return "complete", "deterministic rebuild completed"
        reason = result.get("reason")
        if reason:
            return "failed", str(reason)
        for field in ("failed_slots", "stale_skipped", "abstained"):
            value = result.get(field)
            if value:
                return "failed", f"{field}: {value}"
        error = result.get("error")
        if error not in (None, False, True, ""):
            return "failed", str(error)
        return "failed", "rebuild returned complete=false"

    @staticmethod
    def _missing(claim: ViewProjectionRepairClaim, *fields: str) -> list[str]:
        return [name for name in fields if not str(getattr(claim, name, "") or "").strip()]

    @staticmethod
    def _missing_receipt_error(fields: list[str]) -> str:
        return "repair receipt missing required projection identity fields: " + ", ".join(fields)
