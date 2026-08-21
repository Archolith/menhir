"""LEGACY_ENTITY_UNMERGE -- degraded, manifest-gated recovery of a pre-journal merge (plan Phase 5).

Merges performed before the Phase 4 lossless snapshot left only a lossy audit entry. They can be
PARTIALLY reversed. They can never be exactly reversed. The danger this lane is built to avoid is
handing back a partially-restored graph and letting an operator believe it is repaired.

So the contract is deliberately hostile to accidents:

* **Manifest-gated.** It refuses to touch anything not named in an explicit ``absorbed_uuids``
  manifest. There is no "reverse everything" mode.
* **Acknowledged degradation.** The caller must pass ``acknowledge_degraded=True``. Without it the
  operation reports what it *would* do, and what it *cannot* do, and stops.
* **It never claims exactness.** Every result carries ``exact: False`` and the explicit list of state
  classes the snapshot structurally cannot restore.
* **Journaled, owned and atomic** exactly like the exact lane: PREPARED before mutation, the
  restore performed inside ``owned_mutation`` so the writer's claim is held and its revocation
  predicate published, one atomic restore, after-state verified before COMMITTED. The ownership
  half was missing until CF-232 -- this bullet asserted it while the code did not do it.

Lineage-only records (a merge with no snapshot at all) cannot enter this lane. They are
nonrecoverable, and the inventory reports them as such rather than inventing a before-state.
"""

from __future__ import annotations

import logging
import uuid as uuidlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from menhir.domain import legacy_snapshot as ls
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.services.saga_writer_heartbeat import owned_mutation
from menhir.services.merge_coordinator import (
    MergeDrift,
    _canonical,
    merge_state_fingerprint,
    pair_key,
)

logger = logging.getLogger(__name__)

NOT_IN_MANIFEST = "NOT_IN_MANIFEST"
DEGRADATION_NOT_ACKNOWLEDGED = "DEGRADATION_NOT_ACKNOWLEDGED"
NO_LEGACY_SNAPSHOT = "NO_LEGACY_SNAPSHOT"
MALFORMED_SNAPSHOT = "MALFORMED_SNAPSHOT"
SURVIVOR_MISSING = "SURVIVOR_MISSING"
ALREADY_PRESENT = "ALREADY_PRESENT"
MISSING_PEER = "MISSING_PEER"
PREPARE_FAILED = "PREPARE_FAILED"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class LegacyUnmergeCoordinator:
    """Degraded recovery for merges that predate the lossless snapshot."""

    graph_adapter: Any
    journal: GraphOperationsJournal
    telemetry_store: Any = None

    def __post_init__(self) -> None:
        self.journal._ensure_ready()
        if self.telemetry_store is None:
            from menhir.infrastructure.telemetry import telemetry_store

            self.telemetry_store = telemetry_store

    # ------------------------------------------------------------------ public API
    def unmerge_legacy(
        self,
        absorbed_uuid: str,
        *,
        manifest: list[str],
        acknowledge_degraded: bool = False,
    ) -> dict[str, Any]:
        """Partially reverse ONE historical merge, from an approved manifest.

        Returns a result that ALWAYS carries ``exact: False`` and ``degradations`` -- the classes of
        state this snapshot cannot restore. A caller cannot get a restoration out of this method
        without being told, in the same breath, what it did not get back.
        """
        if absorbed_uuid not in set(manifest or []):
            return {
                "restored": 0, "exact": False, "reason": NOT_IN_MANIFEST,
                "diagnostics": {"absorbed_uuid": absorbed_uuid},
            }

        rows = self.telemetry_store.fetch_merge_audit(absorbed_uuid=absorbed_uuid, limit=1)
        if not rows:
            return {
                "restored": 0, "exact": False, "reason": NO_LEGACY_SNAPSHOT,
                "diagnostics": {
                    "absorbed_uuid": absorbed_uuid,
                    "note": "lineage-only: no snapshot exists, so this merge is NOT recoverable",
                },
            }

        try:
            entry = ls.parse(rows[0]["snapshot_json"])
        except ls.LegacySnapshotError as exc:
            return {
                "restored": 0, "exact": False, "reason": MALFORMED_SNAPSHOT,
                "diagnostics": {"absorbed_uuid": absorbed_uuid, "error": str(exc)},
            }

        survivor_uuid = str(entry["survivor_uuid"])
        degradations = ls.degradations(entry)

        state = self.graph_adapter.fetch_merge_state(survivor_uuid, absorbed_uuid)
        if not state.get("survivor_present"):
            return {
                "restored": 0, "exact": False, "reason": SURVIVOR_MISSING,
                "degradations": degradations,
                "diagnostics": {"survivor_uuid": survivor_uuid},
            }
        if state.get("absorbed_present"):
            return {
                "restored": 0, "exact": False, "reason": ALREADY_PRESENT,
                "degradations": degradations,
                "diagnostics": {"absorbed_uuid": absorbed_uuid},
            }

        out_rels, in_rels = ls.relationships(entry)
        # Episodes that mentioned the absorbed node come back as incoming MENTIONS edges.
        in_rels = in_rels + [
            {"type": "MENTIONS", "peer_uuid": ep, "properties": {}}
            for ep in ls.absorbed_episodes(entry)
        ]

        peer_uuids = [r["peer_uuid"] for r in out_rels + in_rels]
        present = self.graph_adapter.peers_exist(peer_uuids)
        missing = sorted({u for u in peer_uuids if u not in present})
        if missing:
            return {
                "restored": 0, "exact": False, "reason": MISSING_PEER,
                "degradations": degradations,
                "diagnostics": {"missing_peers": missing},
            }

        plan = {
            # The legacy snapshot never recorded labels. :Entity is the only defensible assumption
            # (it is what the old restore did) and OMITS_NODE_LABELS says so out loud.
            "absorbed_labels": ["Entity"],
            "absorbed_properties": ls.absorbed_properties(entry),
            "out_rels": out_rels,
            "in_rels": in_rels,
            # The survivor's pre-merge state was never captured, so there is nothing to restore.
            # An empty map makes `SET s += $props` a no-op -- we do NOT guess at prior values.
            "survivor_properties": {},
            "rebound_episodes": ls.rebound_episodes(entry),
        }

        if not acknowledge_degraded:
            return {
                "restored": 0, "exact": False, "reason": DEGRADATION_NOT_ACKNOWLEDGED,
                "degradations": degradations,
                "would_restore": {
                    "absorbed_uuid": absorbed_uuid,
                    "survivor_uuid": survivor_uuid,
                    "labels": plan["absorbed_labels"],
                    "properties": sorted(plan["absorbed_properties"]),
                    "out_relationships": len(out_rels),
                    "in_relationships": len(in_rels),
                    "survivor_restored": False,
                },
            }

        op_id = uuidlib.uuid4().hex
        merged_fp = merge_state_fingerprint(
            {"survivor_present": True, "absorbed_present": False, "lineage_recorded": True},
            op_id=op_id,
        )
        restored_fp = merge_state_fingerprint(
            {"survivor_present": True, "absorbed_present": True, "lineage_recorded": False},
            op_id=op_id,
        )
        request = {
            "op_id": op_id,
            "survivor_uuid": survivor_uuid,
            "absorbed_uuid": absorbed_uuid,
            "degradations": degradations,
            "requested_at": _utc_now_iso(),
            "expected_before_sha256": merged_fp,
        }

        try:
            self.journal.prepare(
                operation_kind="LEGACY_ENTITY_UNMERGE",
                request_json=_canonical(request),
                target_uuid=absorbed_uuid,
                target_key=pair_key(survivor_uuid, absorbed_uuid),
                before_snapshot_json=rows[0]["snapshot_json"],
                expected_after_sha256=restored_fp,
                op_id=op_id,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "restored": 0, "exact": False, "reason": PREPARE_FAILED,
                "degradations": degradations, "diagnostics": {"error": str(exc)},
            }

        # CF-232: the mutation window runs under `owned_mutation`, exactly as the five sibling
        # coordinators do. It holds the writer's claim AND publishes the revocation predicate that
        # `Neo4jRepository.execute` consults; without it the lease was never renewed during the
        # restore and `_revocation` stayed at its default `None`, so every statement dispatched
        # unconditionally.
        #
        # This lane was safe only by a mechanism in ANOTHER module -- `saga_reconcile_dispatcher`
        # maps LEGACY_ENTITY_UNMERGE to a disposition that always quarantines and never replays, so
        # there was no concurrent writer to protect against. That is a fact about the dispatcher,
        # not about this code, and the module docstring asserted the opposite mechanism. The safety
        # argument now lives where the mutation does.
        #
        # `LEGACY_ENTITY_UNMERGE` is already registered in `SAGA_STATEMENT_COUNTS` at 1, which is
        # correct: `correlation_queries.restore_merge_snapshot` issues exactly one `execute`.
        with owned_mutation(self.journal, op_id, operation_kind="LEGACY_ENTITY_UNMERGE"):
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
            if after_fp != restored_fp:
                self.journal.mark_needs_review(
                    op_id, observed_error=f"legacy unmerge after-state mismatch: {after_fp}"
                )
                raise MergeDrift(f"legacy unmerge after-state drift (op {op_id})")

            self.journal.mark_committed(op_id)
        out = dict(result)
        out.update({
            "op_id": op_id,
            # Non-negotiable: this lane NEVER reports an exact round trip.
            "exact": False,
            "degradations": degradations,
            "survivor_restored": False,
        })
        return out


__all__ = [
    "LegacyUnmergeCoordinator",
    "NOT_IN_MANIFEST",
    "DEGRADATION_NOT_ACKNOWLEDGED",
    "NO_LEGACY_SNAPSHOT",
    "MALFORMED_SNAPSHOT",
    "SURVIVOR_MISSING",
    "ALREADY_PRESENT",
    "MISSING_PEER",
    "PREPARE_FAILED",
]
