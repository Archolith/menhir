"""MergeCoordinator -- the durable saga for a destructive entity merge (plan Phase 4).

The legacy path deleted the absorbed node and THEN wrote a best-effort, failure-swallowing telemetry
row. A crash (or a telemetry failure) between those two steps destroyed the node with no durable
snapshot: unrecoverable. This coordinator inverts that order and makes the operation replayable:

    ELIGIBLE   fail-closed policy recheck on current graph state (invariant 7)
    SNAPSHOT   complete, versioned, checksummed state of BOTH identities (invariant: lossless)
    PREPARED   commit the journal row -- WITH the snapshot -- before touching the graph (invariant 3)
    MUTATE     idempotent merge stamped with op_id
    VERIFY     compare the complete observed after-state to the one frozen at PREPARE
    COMMITTED  only on an exact match; anything else is NEEDS_REVIEW (invariant 5)
    RECONCILE  replay any row left PREPARED by a crash; drift is quarantined, never auto-repaired

If PREPARE fails, the graph is NOT mutated -- the merge abstains. That is the whole point: a merge
may only proceed once its recovery snapshot is durable.

Fencing: the journal's unresolved-key index is keyed on the normalized PAIR key, so while a merge is
PREPARED or NEEDS_REVIEW no competing merge of the same pair can be prepared (invariant 14).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid as uuidlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from menhir.domain import merge_eligibility as me
from menhir.domain import merge_snapshot as ms
from menhir.infrastructure.graph_operations import GraphOperationsJournal

logger = logging.getLogger(__name__)


#: Stable abstention reason code (invariant 15). Surfaced so observe-mode reporting can count
#: high-degree hubs that would become unmergeable BEFORE the limit is enforced in anger.
MERGE_SNAPSHOT_TOO_LARGE = "MERGE_SNAPSHOT_TOO_LARGE"


class MergeDrift(RuntimeError):
    """The graph is in neither the expected before-state nor the expected after-state."""


class MergeGraphAdapter(Protocol):
    """The narrow graph surface the coordinator needs."""

    def evaluate_merge_eligibility(self, survivor_uuid: str, absorbed_uuid: str) -> Any: ...
    def capture_merge_snapshot(
        self, survivor_uuid: str, absorbed_uuid: str, *, similarity: float | None = ...
    ) -> dict[str, Any]: ...
    def fetch_merge_state(self, survivor_uuid: str, absorbed_uuid: str) -> dict[str, Any]: ...
    def merge_entity(
        self, survivor_uuid: str, absorbed_uuid: str, *, similarity: float,
        operation_id: str | None = ...,
    ) -> dict[str, Any]: ...


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def pair_key(survivor_uuid: str, absorbed_uuid: str) -> str:
    """Order-independent pair identity, so a reversed-direction merge of the same two nodes is
    fenced by the same journal key."""
    return "::".join(sorted([str(survivor_uuid), str(absorbed_uuid)]))


def merge_state_fingerprint(state: dict[str, Any], *, op_id: str) -> str:
    """Fingerprint the material state of THIS pair's absorption.

    Deliberately keyed on pair-specific facts only: the absorbed node is gone AND the survivor's
    lineage records THIS absorbed uuid (``fetch_merge_state`` computes ``lineage_recorded`` as
    ``$absorbed IN survivor.merged_from``). Together those uniquely identify this exact absorption,
    and the journal's pair fence already prevents a competing operation from performing it.

    ``last_merge_op_id`` is deliberately NOT fingerprinted. It is a survivor-GLOBAL "who touched me
    last" stamp, so folding it in made the check over-strict: when a second merge absorbed a
    different node into the same survivor, it overwrote the stamp and the first op's after-state
    check failed -- quarantining a merge that had actually SUCCEEDED. It remains on the node (and in
    the diagnostics) as an idempotency aid and audit breadcrumb, never as a correctness gate.

    ``op_id`` is retained in the signature for call-site symmetry and future diagnostics.
    """
    return hashlib.sha256(
        _canonical(
            {
                "survivor_present": bool(state.get("survivor_present")),
                "absorbed_present": bool(state.get("absorbed_present")),
                "lineage_recorded": bool(state.get("lineage_recorded")),
            }
        ).encode("utf-8")
    ).hexdigest()


@dataclass
class MergeCoordinator:
    """The ONLY sanctioned destructive-merge entry point."""

    graph_adapter: MergeGraphAdapter
    journal: GraphOperationsJournal

    def __post_init__(self) -> None:
        self.journal._ensure_ready()

    # ------------------------------------------------------------------ public API
    def merge(
        self, *, survivor_uuid: str, absorbed_uuid: str, similarity: float
    ) -> dict[str, Any]:
        """Run the merge saga. Returns a structured result; never partially mutates.

        Abstains (``merged=0`` + ``reason``) when the pair is ineligible, when the snapshot cannot be
        built, or when PREPARE fails (e.g. the pair is fenced by an unresolved operation).
        """
        # (1) Fail-closed eligibility on CURRENT state. Discovery is not authorization.
        eligibility = self.graph_adapter.evaluate_merge_eligibility(survivor_uuid, absorbed_uuid)
        if not eligibility.allowed:
            return {"merged": 0, "reason": eligibility.reason_code,
                    "diagnostics": eligibility.diagnostics}

        op_id = uuidlib.uuid4().hex

        # (2) Complete recovery snapshot BEFORE anything destructive. If it cannot be built or
        # serialized, abstain -- never truncate, never merge without a durable inverse.
        try:
            snapshot = self.graph_adapter.capture_merge_snapshot(
                survivor_uuid, absorbed_uuid, similarity=similarity
            )
            # dumps() enforces the documented size ceiling and raises rather than truncating.
            snapshot_json = ms.dumps(snapshot)
        except ms.SnapshotTooLargeError as exc:
            # Invariant 15: a high-degree hub whose snapshot cannot be stored WHOLE is not merged.
            # Truncating would silently destroy the inverse the snapshot exists to guarantee.
            logger.warning(
                "merge %s <- %s abstained: snapshot too large (%d bytes > %d limit)",
                survivor_uuid, absorbed_uuid, exc.size_bytes, exc.limit,
            )
            return {
                "merged": 0,
                "reason": MERGE_SNAPSHOT_TOO_LARGE,
                "diagnostics": {"snapshot_bytes": exc.size_bytes, "limit_bytes": exc.limit},
            }
        except ms.SnapshotSchemaError as exc:
            logger.warning("merge %s <- %s abstained: snapshot failed: %s",
                           survivor_uuid, absorbed_uuid, exc)
            return {"merged": 0, "reason": "SNAPSHOT_FAILED", "diagnostics": {"error": str(exc)}}

        before = self.graph_adapter.fetch_merge_state(survivor_uuid, absorbed_uuid)
        request = {
            "op_id": op_id,
            "survivor_uuid": survivor_uuid,
            "absorbed_uuid": absorbed_uuid,
            "similarity": float(similarity),
            "requested_at": _utc_now_iso(),
            "expected_before_sha256": merge_state_fingerprint(before, op_id=op_id),
        }
        # The state a successful merge produces: absorbed gone, survivor present, lineage recorded,
        # and stamped with THIS op. Frozen at PREPARE so a replay can recognize its own work.
        expected_after = merge_state_fingerprint(
            {
                "survivor_present": True,
                "absorbed_present": False,
                "lineage_recorded": True,
                "last_merge_op_id": op_id,
            },
            op_id=op_id,
        )

        # (3) PREPARED -- the snapshot becomes durable HERE, before any mutation (invariant 3).
        try:
            self.journal.prepare(
                operation_kind="ENTITY_MERGE",
                request_json=_canonical(request),
                target_uuid=absorbed_uuid,
                target_key=pair_key(survivor_uuid, absorbed_uuid),
                before_snapshot_json=snapshot_json,
                expected_after_sha256=expected_after,
                op_id=op_id,
            )
        except Exception as exc:  # noqa: BLE001 -- includes GraphOperationError (pair fenced)
            logger.warning("merge %s <- %s abstained: PREPARE failed: %s",
                           survivor_uuid, absorbed_uuid, exc)
            return {"merged": 0, "reason": "PREPARE_FAILED", "diagnostics": {"error": str(exc)}}

        return self._apply(request)

    # ------------------------------------------------------------------ the saga body
    def _apply(self, request: dict[str, Any]) -> dict[str, Any]:
        """Mutate + verify for a PREPARED request. Used by BOTH the first attempt and replay, so a
        replay can never diverge from the original path."""
        op_id = str(request["op_id"])
        survivor_uuid = str(request["survivor_uuid"])
        absorbed_uuid = str(request["absorbed_uuid"])
        row = self.journal.get(op_id) or {}
        expected_after = row.get("expected_after_sha256")
        expected_before = request.get("expected_before_sha256")

        observed = self.graph_adapter.fetch_merge_state(survivor_uuid, absorbed_uuid)
        observed_fp = merge_state_fingerprint(observed, op_id=op_id)

        # Already applied by an attempt that crashed before COMMITTED: the survivor carries THIS
        # op's marker, so replaying is a no-op.
        if expected_after and observed_fp == expected_after:
            self.journal.mark_committed(op_id)
            return {"merged": 1, "replayed": True, "op_id": op_id}

        # Neither expected state -> quarantine. A background replay must never paper over a graph
        # that some other writer changed.
        if observed_fp != expected_before:
            self.journal.mark_needs_review(
                op_id,
                observed_error=(
                    f"merge drift: observed={observed_fp} expected_before={expected_before} "
                    f"expected_after={expected_after} state={observed}"
                ),
            )
            raise MergeDrift(
                f"merge {survivor_uuid} <- {absorbed_uuid} (op {op_id}): the graph is in neither "
                "the expected before- nor after-state; NOT mutating"
            )

        # Expected before-state -> mutate. merge_entity re-checks eligibility itself and repeats the
        # material predicates inside the mutation, so a change in this window still fails closed.
        try:
            result = self.graph_adapter.merge_entity(
                survivor_uuid, absorbed_uuid, similarity=float(request["similarity"]),
                operation_id=op_id,
            )
        except Exception as exc:  # noqa: BLE001
            self.journal.record_attempt(op_id, error=f"{type(exc).__name__}: {exc}")
            raise

        if not result.get("merged"):
            # The repository abstained at its own gate -- e.g. a node became COMPRESSED between
            # PREPARE and MUTATE, so the in-mutation WHERE failed closed. Do NOT take that claim on
            # trust: re-read the graph and prove it is untouched before declaring the op harmless.
            reason = str(result.get("reason") or "REPOSITORY_ABSTAINED")
            post = self.graph_adapter.fetch_merge_state(survivor_uuid, absorbed_uuid)
            post_fp = merge_state_fingerprint(post, op_id=op_id)
            if post_fp == expected_before:
                # Graph is exactly as it was: nothing to adjudicate and nothing to replay. FAILED is
                # terminal and RELEASES the pair fence -- quarantining here would lock the pair out
                # forever over a benign, non-destructive abstention.
                self.journal.mark_failed(op_id, reason=f"repository abstained: {reason}")
                return {"merged": 0, "reason": reason, "op_id": op_id}
            # It claimed to abstain but the graph moved: that is exactly the ambiguity NEEDS_REVIEW
            # exists for.
            self.journal.mark_needs_review(
                op_id,
                observed_error=(
                    f"repository abstained ({reason}) but the graph is not at the expected "
                    f"before-state: observed={post_fp} expected_before={expected_before} state={post}"
                ),
            )
            raise MergeDrift(
                f"merge {survivor_uuid} <- {absorbed_uuid} (op {op_id}): repository abstained but "
                "the graph changed; quarantined"
            )

        # VERIFY the complete after-state. Only an exact match may COMMIT (invariant 5).
        after = self.graph_adapter.fetch_merge_state(survivor_uuid, absorbed_uuid)
        actual = merge_state_fingerprint(after, op_id=op_id)
        if expected_after and actual != expected_after:
            self.journal.mark_needs_review(
                op_id,
                observed_error=f"after-state mismatch expected={expected_after} actual={actual} "
                               f"state={after}",
            )
            raise MergeDrift(
                f"merge after-state drift for {survivor_uuid} <- {absorbed_uuid} (op {op_id})"
            )

        self.journal.mark_committed(op_id)
        out = dict(result)
        out["op_id"] = op_id
        return out

    # ------------------------------------------------------------------ reconciliation
    def reconcile(self, *, limit: int = 500) -> dict[str, int]:
        """Replay every ENTITY_MERGE left PREPARED by a crash. Drift is reported, never repaired."""
        replayed = 0
        drifted = 0
        failed = 0
        for row in self.journal.list_by_state("PREPARED", limit=limit):
            if row.get("operation_kind") != "ENTITY_MERGE":
                continue
            op_id = str(row["op_id"])
            try:
                request = json.loads(row["request_json"])
            except (TypeError, ValueError):
                self.journal.mark_needs_review(op_id, observed_error="unparseable request_json")
                drifted += 1
                continue
            try:
                self._apply(request)
                replayed += 1
            except MergeDrift:
                drifted += 1
                logger.warning("merge saga: op %s drifted; left NEEDS_REVIEW", op_id)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.warning("merge saga: replay of op %s failed: %s", op_id, exc)
        return {"replayed": replayed, "drifted": drifted, "failed": failed}

    # ------------------------------------------------------------------ recovery reads
    def load_snapshot(self, op_id: str) -> dict[str, Any]:
        """Load and VALIDATE the recovery snapshot for a merge (version + checksum). Phase 5 (exact
        unmerge) consumes this; exposed here so the snapshot's durability is testable now."""
        row = self.journal.get(op_id)
        if row is None:
            raise MergeDrift(f"no operation {op_id!r}")
        raw = row.get("before_snapshot_json")
        if not raw:
            raise ms.SnapshotSchemaError(f"operation {op_id!r} has no recovery snapshot")
        return ms.load_snapshot(ms.loads(raw))


__all__ = [
    "MergeCoordinator",
    "MergeDrift",
    "MERGE_SNAPSHOT_TOO_LARGE",
    "merge_state_fingerprint",
    "pair_key",
    "me",
]
