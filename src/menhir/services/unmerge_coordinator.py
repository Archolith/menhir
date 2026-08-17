"""UnmergeCoordinator -- the exact, replayable inverse of a journaled merge (plan Phase 5).

The legacy `scripts/unmerge.py` recreated the absorbed node, then restored relationships, then fixed
provenance -- in FIVE separate statements, guarded by "skip if the node already exists". A crash
after statement one left a bare node with no edges, and every rerun then SKIPPED it: permanently
half-restored, silently. It also could not reverse the survivor's delta at all ("no pre-merge
survivor snapshot exists"), and its snapshot was lossy.

This coordinator replaces it. It restores from the Phase 4 lossless snapshot in ONE atomic
transaction and refuses to run at all unless it can be exact:

    LOAD       the forward ENTITY_MERGE operation + its versioned, checksummed snapshot
    GUARD      the graph must still be in the merge's after-state, the survivor must still hold
               exactly what the merge wrote, and every snapshot peer must still exist
    PREPARED   journal the ENTITY_UNMERGE before touching the graph (invariant 3)
    MUTATE     one atomic restore
    VERIFY     the complete after-state; only an exact match may COMMIT (invariant 5)
    REVERSED   mark the forward merge reversed

Invariant 9 is the spine: an unmerge may restore only when the current graph matches the forward
merge's expected after-state. Newer or conflicting state is PRESERVED and routed to NEEDS_REVIEW --
we never overwrite something a human or a later job changed. A missing peer is likewise a refusal,
never a fabrication: the default is all-or-nothing.
"""

from __future__ import annotations

import json
import logging
import uuid as uuidlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from menhir.domain import merge_delta as md
from menhir.domain import merge_snapshot as ms
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.services.merge_coordinator import (
    MergeDrift,
    _canonical,
    merge_state_fingerprint,
    pair_key,
)
from menhir.services.saga_reconcile_outcomes import (
    SKIP,
    WOULD_MARK_ALREADY_APPLIED,
    WOULD_NEEDS_REVIEW,
    WOULD_RESTORE,
    summarize_outcomes,
)

logger = logging.getLogger(__name__)

# Stable refusal reason codes (contract surface for operator tooling and tests).
NOT_A_COMMITTED_MERGE = "NOT_A_COMMITTED_MERGE"
NO_SNAPSHOT = "NO_SNAPSHOT"
GRAPH_NOT_IN_MERGE_AFTER_STATE = "GRAPH_NOT_IN_MERGE_AFTER_STATE"
SURVIVOR_CHANGED_SINCE_MERGE = "SURVIVOR_CHANGED_SINCE_MERGE"
MISSING_PEER = "MISSING_PEER"
PREPARE_FAILED = "PREPARE_FAILED"
ALREADY_RESTORED = "ALREADY_RESTORED"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class UnmergeCoordinator:
    """The ONLY sanctioned exact-unmerge entry point."""

    graph_adapter: Any
    journal: GraphOperationsJournal
    #: optional post-COMMIT hook, injected only when ScalarState (Piece C) is on. Called after an
    #: unmerge COMMITs (both first attempt and replay) to restore scalar assertions to the absorbed
    #: entity. None (default) -> no scalar coupling, byte-identical to before. Best-effort: it must
    #: never fail the unmerge (already committed; scalar reconciliation is repairable out of band).
    on_unmerge_committed: Any = None

    def __post_init__(self) -> None:
        self.journal._ensure_ready()

    def _fire_unmerge_hook(self, *, survivor_uuid: str, absorbed_uuid: str, merge_op_id: str,
                           unmerge_op_id: str) -> None:
        if self.on_unmerge_committed is None:
            return
        try:
            self.on_unmerge_committed(
                survivor_uuid=survivor_uuid, absorbed_uuid=absorbed_uuid,
                merge_op_id=merge_op_id, unmerge_op_id=unmerge_op_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "scalar-state unmerge restore failed for %s <- %s (merge op=%s, unmerge op=%s): %s; "
                "leaving for reconcile repair",
                survivor_uuid, absorbed_uuid, merge_op_id, unmerge_op_id, exc)

    # ------------------------------------------------------------------ public API
    def unmerge(self, merge_op_id: str, *, dry_run: bool = False) -> dict[str, Any]:
        """Reverse a COMMITTED ENTITY_MERGE exactly. Abstains rather than approximating.

        ``dry_run`` runs every guard and reports what WOULD be restored, without mutating.
        """
        merge_row = self.journal.get(merge_op_id)
        if (
            merge_row is None
            or merge_row.get("operation_kind") != "ENTITY_MERGE"
            or merge_row.get("state") != "COMMITTED"
        ):
            state = merge_row.get("state") if merge_row else None
            return {
                "restored": 0, "reason": NOT_A_COMMITTED_MERGE,
                "diagnostics": {"merge_op_id": merge_op_id, "state": state},
            }

        raw = merge_row.get("before_snapshot_json")
        if not raw:
            return {"restored": 0, "reason": NO_SNAPSHOT,
                    "diagnostics": {"merge_op_id": merge_op_id}}

        # Version + checksum are validated here; an unsupported or corrupt snapshot fails CLOSED
        # rather than being reinterpreted (plan section 2).
        body = ms.load_snapshot(ms.loads(raw))
        absorbed = ms.decode_node(body["absorbed"])
        survivor = ms.decode_node(body["survivor"])
        survivor_uuid, absorbed_uuid = survivor["uuid"], absorbed["uuid"]

        # --- GUARD 1: the graph must still be in the forward merge's after-state (invariant 9).
        observed = self.graph_adapter.fetch_merge_state(survivor_uuid, absorbed_uuid)
        merged_fp = merge_state_fingerprint(
            {"survivor_present": True, "absorbed_present": False, "lineage_recorded": True},
            op_id=merge_op_id,
        )
        restored_fp = merge_state_fingerprint(
            {"survivor_present": True, "absorbed_present": True, "lineage_recorded": False},
            op_id=merge_op_id,
        )
        observed_fp = merge_state_fingerprint(observed, op_id=merge_op_id)

        if observed_fp == restored_fp:
            # Already inverted (e.g. a crash after the restore but before COMMITTED). Idempotent.
            return {"restored": 0, "reason": ALREADY_RESTORED,
                    "diagnostics": {"merge_op_id": merge_op_id}}
        if observed_fp != merged_fp:
            return {
                "restored": 0, "reason": GRAPH_NOT_IN_MERGE_AFTER_STATE,
                "diagnostics": {"observed": observed, "merge_op_id": merge_op_id},
            }

        # --- GUARD 2: the survivor must still hold exactly what the merge wrote. If someone changed
        # it afterwards, that newer state is PRESERVED, not clobbered (invariant 9).
        current_survivor = self.graph_adapter.fetch_survivor_properties(survivor_uuid) or {}
        matches, differences = md.survivor_matches_merge_output(
            current_survivor, survivor["properties"], absorbed["properties"]
        )
        if not matches:
            return {
                "restored": 0, "reason": SURVIVOR_CHANGED_SINCE_MERGE,
                "diagnostics": {"differences": differences, "merge_op_id": merge_op_id},
            }

        # --- GUARD 3: every peer the snapshot references must still exist. We do not fabricate a
        # peer, and we do not silently drop the edge -- the default is all-or-nothing.
        rels = absorbed["relationships"]
        peer_uuids = [r["peer_uuid"] for r in rels if r.get("peer_uuid")]
        unidentified = [r for r in rels if not r.get("peer_uuid")]
        present = self.graph_adapter.peers_exist(peer_uuids)
        missing = sorted({u for u in peer_uuids if u not in present})
        if missing or unidentified:
            return {
                "restored": 0, "reason": MISSING_PEER,
                "diagnostics": {
                    "missing_peers": missing,
                    "peers_without_uuid": [
                        r.get("peer_identity") for r in unidentified
                    ],
                    "merge_op_id": merge_op_id,
                },
            }

        plan = self._build_restore_plan(survivor, absorbed)
        if dry_run:
            return {
                "restored": 0, "reason": "DRY_RUN",
                "would_restore": {
                    "absorbed_uuid": absorbed_uuid,
                    "labels": absorbed["labels"],
                    "properties": len(plan["absorbed_properties"]),
                    "out_relationships": len(plan["out_rels"]),
                    "in_relationships": len(plan["in_rels"]),
                    "rebound_episodes_to_remove": plan["rebound_episodes"],
                    "survivor_properties_restored": plan["survivor_properties"],
                },
            }

        op_id = uuidlib.uuid4().hex
        request = {
            "op_id": op_id,
            "merge_op_id": merge_op_id,
            "survivor_uuid": survivor_uuid,
            "absorbed_uuid": absorbed_uuid,
            "requested_at": _utc_now_iso(),
            "expected_before_sha256": merged_fp,
        }

        # --- PREPARED before any mutation (invariant 3). The forward merge is COMMITTED, so its pair
        # fence is released; this row re-fences the same pair for the duration of the unmerge.
        try:
            self.journal.prepare(
                operation_kind="ENTITY_UNMERGE",
                request_json=_canonical(request),
                target_uuid=absorbed_uuid,
                target_key=pair_key(survivor_uuid, absorbed_uuid),
                before_snapshot_json=raw,  # carry the same snapshot, so this row is self-contained
                expected_after_sha256=restored_fp,
                reverses_op_id=merge_op_id,
                op_id=op_id,
            )
        except Exception as exc:  # noqa: BLE001
            return {"restored": 0, "reason": PREPARE_FAILED, "diagnostics": {"error": str(exc)}}

        return self._apply(request, plan)

    # ------------------------------------------------------------------ saga body
    def _build_restore_plan(
        self, survivor: dict[str, Any], absorbed: dict[str, Any]
    ) -> dict[str, Any]:
        """Turn the decoded snapshot into the exact parameters of the atomic restore."""
        rels = absorbed["relationships"]
        out_rels = [
            {"type": r["type"], "peer_uuid": r["peer_uuid"], "properties": r["properties"]}
            for r in rels if r["direction"] == "out"
        ]
        in_rels = [
            {"type": r["type"], "peer_uuid": r["peer_uuid"], "properties": r["properties"]}
            for r in rels if r["direction"] == "in"
        ]

        # The MENTIONS the merge REBOUND onto the survivor = the absorbed node's mentioning episodes
        # MINUS the ones the survivor already had. Removing all of them would strip provenance the
        # survivor legitimately owned; removing none would leave an episode mentioning BOTH identities
        # (fabricated provenance that would then trip the co-mention veto).
        absorbed_eps = {
            r["peer_uuid"] for r in rels
            if r["type"] == "MENTIONS" and r["direction"] == "in" and r.get("peer_uuid")
        }
        survivor_eps = {
            r["peer_uuid"] for r in survivor["relationships"]
            if r["type"] == "MENTIONS" and r["direction"] == "in" and r.get("peer_uuid")
        }
        rebound = sorted(absorbed_eps - survivor_eps)

        return {
            "absorbed_labels": absorbed["labels"],
            "absorbed_properties": ms.restorable_properties(absorbed["properties"]),
            "out_rels": out_rels,
            "in_rels": in_rels,
            "survivor_properties": md.restorable_survivor_properties(survivor["properties"]),
            "rebound_episodes": rebound,
        }

    def _classify_replay(self, request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Classify a PREPARED unmerge row WITHOUT mutating anything.

        Returns (outcome, diagnostics): exactly the pre-mutation decision that live replay would
        make, so a dry-run can report it without touching the journal or the graph. This method is
        PURE -- it must never call mark_committed / mark_needs_review / _mark_merge_reversed /
        _fire_unmerge_hook / record_attempt / restore_merge_snapshot.
        """
        op_id = str(request["op_id"])
        survivor_uuid = str(request["survivor_uuid"])
        absorbed_uuid = str(request["absorbed_uuid"])
        merge_op_id = str(request["merge_op_id"])
        row = self.journal.get(op_id) or {}
        expected_after = row.get("expected_after_sha256")
        expected_before = request.get("expected_before_sha256")

        observed_fp = merge_state_fingerprint(
            self.graph_adapter.fetch_merge_state(survivor_uuid, absorbed_uuid), op_id=op_id
        )

        diagnostics = {
            "op_id": op_id,
            "survivor_uuid": survivor_uuid,
            "absorbed_uuid": absorbed_uuid,
            "merge_op_id": merge_op_id,
            "observed_fp": observed_fp,
            "expected_before": expected_before,
            "expected_after": expected_after,
        }

        if expected_after and observed_fp == expected_after:
            return (WOULD_MARK_ALREADY_APPLIED, diagnostics)
        if expected_before is None:
            diagnostics["observed_error"] = (
                "request has no expected_before_sha256; cannot verify precondition"
            )
            return (WOULD_NEEDS_REVIEW, diagnostics)
        if observed_fp != expected_before:
            diagnostics["observed_error"] = (
                f"precondition drift: observed={observed_fp} "
                f"expected_before={expected_before} expected_after={expected_after}"
            )
            return (WOULD_NEEDS_REVIEW, diagnostics)
        return (WOULD_RESTORE, diagnostics)

    def _apply(self, request: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
        outcome, diag = self._classify_replay(request)
        op_id = diag["op_id"]
        survivor_uuid = diag["survivor_uuid"]
        absorbed_uuid = diag["absorbed_uuid"]
        expected_after = diag["expected_after"]
        expected_before = diag["expected_before"]
        observed_fp = diag["observed_fp"]

        if outcome == WOULD_MARK_ALREADY_APPLIED:
            # A previous attempt already restored it and crashed before COMMITTED.
            self.journal.mark_committed(op_id)
            self._mark_merge_reversed(str(request["merge_op_id"]))
            self._fire_unmerge_hook(
                survivor_uuid=survivor_uuid, absorbed_uuid=absorbed_uuid,
                merge_op_id=str(request["merge_op_id"]), unmerge_op_id=op_id)
            return {"restored": 1, "replayed": True, "op_id": op_id}

        # --- GUARD 4: only the merge's exact before-state may be unmerged. A graph that drifted
        # since the snapshot is PRESERVED and routed to NEEDS_REVIEW, never overwritten (invariant 9).
        #
        # FAIL CLOSED: a request with no frozen precondition cannot be verified, so it must NOT be
        # applied. Waving it through would mutate a possibly-drifted graph with no check at all.
        if outcome == WOULD_NEEDS_REVIEW:
            self.journal.mark_needs_review(op_id, observed_error=diag["observed_error"])
            if expected_before is None:
                raise MergeDrift(
                    f"op {op_id} has no frozen precondition; NOT mutating (fail closed)"
                )
            raise MergeDrift(
                f"precondition drift for {survivor_uuid} <- {absorbed_uuid} (op {op_id}): the graph "
                f"is in neither the expected before- nor after-state; NOT restoring"
            )

        try:
            result = self.graph_adapter.restore_merge_snapshot(
                survivor_uuid=survivor_uuid,
                absorbed_uuid=absorbed_uuid,
                operation_id=op_id,
                **plan,
            )
        except Exception as exc:  # noqa: BLE001
            self.journal.record_attempt(op_id, error=f"{type(exc).__name__}: {exc}")
            raise

        after_fp = merge_state_fingerprint(
            self.graph_adapter.fetch_merge_state(survivor_uuid, absorbed_uuid), op_id=op_id
        )
        if expected_after and after_fp != expected_after:
            self.journal.mark_needs_review(
                op_id,
                observed_error=f"unmerge after-state mismatch expected={expected_after} "
                               f"actual={after_fp}",
            )
            raise MergeDrift(
                f"unmerge after-state drift for {survivor_uuid} <- {absorbed_uuid} (op {op_id})"
            )

        self.journal.mark_committed(op_id)
        self._mark_merge_reversed(str(request["merge_op_id"]))
        self._fire_unmerge_hook(
            survivor_uuid=survivor_uuid, absorbed_uuid=absorbed_uuid,
            merge_op_id=str(request["merge_op_id"]), unmerge_op_id=op_id)
        out = dict(result)
        out["op_id"] = op_id
        return out

    def _mark_merge_reversed(self, merge_op_id: str) -> None:
        try:
            self.journal.mark_reversed(merge_op_id)
        except Exception as exc:  # noqa: BLE001
            # The unmerge itself is COMMITTED and verified; failing to stamp the forward row is an
            # audit-hygiene problem, not a correctness one. Report, do not raise.
            logger.warning("could not mark merge %s REVERSED: %s", merge_op_id, exc)

    # ------------------------------------------------------------------ reconciliation
    def reconcile(self, *, limit: int = 500, dry_run: bool = False) -> dict[str, Any]:
        """Replay every ENTITY_UNMERGE left PREPARED by a crash, or report what WOULD happen.

        ``dry_run=True`` still parses each request, loads the before-snapshot, and builds the
        restore plan -- pure reads that must succeed -- then classifies each row via
        ``_classify_replay`` and mutates nothing. The classification is the observation contract
        (CF-20a); the counters stay at zero because nothing is replayed, and ``outcomes`` carries
        the per-row decision.
        """
        replayed = 0
        drifted = 0
        failed = 0
        scanned = 0
        outcomes: list[dict[str, Any]] = []
        for row in self.journal.list_by_state("PREPARED", limit=limit):
            scanned += 1
            op_id = str(row["op_id"])
            kind = row.get("operation_kind")
            if kind != "ENTITY_UNMERGE":
                if dry_run:
                    outcomes.append(
                        {"op_id": op_id, "operation_kind": kind, "outcome": SKIP}
                    )
                continue
            try:
                request = json.loads(row["request_json"])
                body = ms.load_snapshot(ms.loads(row["before_snapshot_json"]))
                plan = self._build_restore_plan(
                    ms.decode_node(body["survivor"]), ms.decode_node(body["absorbed"])
                )
            except (TypeError, ValueError, ms.SnapshotSchemaError) as exc:
                if dry_run:
                    outcomes.append(
                        {
                            "op_id": op_id,
                            "operation_kind": kind,
                            "outcome": WOULD_NEEDS_REVIEW,
                            "observed_error": f"unreplayable row: {exc}",
                        }
                    )
                else:
                    self.journal.mark_needs_review(
                        op_id, observed_error=f"unreplayable row: {exc}"
                    )
                    drifted += 1
                continue
            if dry_run:
                # A malformed row must not abort the scan -- see the note in MergeCoordinator.
                # This coordinator is the most exposed: _classify_replay reads merge_op_id
                # directly, so a legacy row written without it would otherwise raise KeyError
                # and take the rest of the preflight down with it.
                try:
                    outcome, diag = self._classify_replay(request)
                except Exception as exc:  # noqa: BLE001
                    outcomes.append({
                        "op_id": op_id,
                        "operation_kind": kind,
                        "outcome": WOULD_NEEDS_REVIEW,
                        "observed_error": f"unclassifiable row: {type(exc).__name__}: {exc}",
                    })
                    continue
                entry: dict[str, Any] = {
                    "op_id": op_id, "operation_kind": kind, "outcome": outcome
                }
                if "observed_error" in diag:
                    entry["observed_error"] = diag["observed_error"]
                outcomes.append(entry)
                continue
            try:
                self._apply(request, plan)
                replayed += 1
            except MergeDrift:
                drifted += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.warning("unmerge saga: replay of op %s failed: %s", op_id, exc)
        if dry_run:
            # replayed/drifted/failed stay 0 in dry-run: they count actions PERFORMED, and a
            # dry-run performs none. The forecast lives in counts/outcomes instead.
            return {
                "replayed": replayed,
                "drifted": drifted,
                "failed": failed,
                "dry_run": True,
                "scanned": scanned,
                "counts": summarize_outcomes(outcomes),
                "outcomes": outcomes,
            }
        return {"replayed": replayed, "drifted": drifted, "failed": failed}


__all__ = [
    "UnmergeCoordinator",
    "NOT_A_COMMITTED_MERGE",
    "NO_SNAPSHOT",
    "GRAPH_NOT_IN_MERGE_AFTER_STATE",
    "SURVIVOR_CHANGED_SINCE_MERGE",
    "MISSING_PEER",
    "PREPARE_FAILED",
    "ALREADY_RESTORED",
]
