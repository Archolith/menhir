"""Explicit erasure as a durable cross-store saga (CF-165).

Deletion used to be graph-only: ``delete_memory`` / ``delete_namespace`` issued Cypher and
touched no SQLite, so a deleted node's verbatim prior content stayed readable in the sidecar
-- which the sidecar's own docstring describes as designed to outlive the node.

Neo4j and the SQLite sidecar cannot commit one transaction together, so a bare sequence of
"delete graph, then purge SQLite" is not a fix: a crash between the two steps recreates the
finding, and reversing the order only moves the window. Erasure is therefore a durable
operation with a journaled intent, not a callback hanging off graph deletion.

Order of events, and why:

1. **Capture the subject set.** Once the graph partition is gone it can no longer be asked
   which UUIDs still need sidecar cleanup, so the inventory is captured while the graph can
   still answer.
2. **PREPARE the intent and the subject inventory in ONE SQLite transaction.** Both live in
   the same database file, so this is genuinely atomic. A PREPARED erasure whose subjects
   were not recorded would be unresumable, which is the failure this ordering removes.
3. **The intent is immediately a read veto.** From the moment PREPARE commits, those
   subjects are suppressed for readers even though their rows still exist. See
   ``services/erasure_veto.py``; the durability of the saga guarantees eventual completion,
   the veto covers the window before it.
4. **Delete the graph state.**
5. **Purge the sidecar** through the classification registry.
6. **Verify** no addressable content survives, then COMMIT.

A crash anywhere after step 2 leaves enough non-content state to resume: the journal row
says an erasure is unresolved, and the subject inventory says on whose behalf -- without
rediscovering anything from a graph that may already be gone.

**Sidecar-authoritative, deliberately.** An explicit erase of UUID X proceeds even when X is
already absent from the graph. A merge intentionally removes the absorbed node while keeping
its recovery snapshot in ``merge_audit``, so "the graph node is gone" is exactly the state in
which sidecar content most needs erasing. The old delete path returned "nothing to delete"
here and journaled nothing, which is the specific gap this closes: the outcome distinguishes
``graph_already_absent`` from ``nothing_to_erase``.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid as uuidlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from menhir.infrastructure.erasure_subjects import ErasureSubjectStore
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.telemetry.erasure_purge import (
    ErasureSubjects,
    count_residual_content,
    purge_content,
)
from menhir.services.saga_reconcile_outcomes import (
    DRIFTED,
    REPLAYED,
    SKIP,
    SKIPPED,
    WOULD_NEEDS_REVIEW,
    WOULD_REPLAY,
)
from menhir.services.saga_writer_heartbeat import owned_mutation

logger = logging.getLogger(__name__)

ERASURE_KIND = "EXPLICIT_ERASURE"

#: Outcome reasons. Distinguishing these is a requirement, not a nicety: an operator needs to
#: tell "erased, and the graph node was already gone" from "there was nothing anywhere".
NOTHING_TO_ERASE = "nothing_to_erase"
GRAPH_ALREADY_ABSENT = "graph_already_absent"
ERASED = "erased"
PREPARE_FAILED = "prepare_failed"
RESIDUAL_CONTENT = "residual_content_after_purge"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ErasureCoordinator:
    """Runs explicit erasure of a memory or a whole namespace as a journaled saga."""

    graph_adapter: Any
    journal: GraphOperationsJournal = field(default_factory=GraphOperationsJournal)
    subjects: ErasureSubjectStore = field(default_factory=ErasureSubjectStore)

    def __post_init__(self) -> None:
        self.journal._ensure_ready()
        self.subjects._ensure_ready()
        # The purge is registry-driven over the TELEMETRY tables, which only McpTelemetryStore
        # creates -- the journal's own schema pass does not. Without this, erasure raises
        # "no such table" on a database where the journal exists but the telemetry schema has
        # not been initialised yet.
        from menhir.infrastructure.telemetry.store import McpTelemetryStore

        McpTelemetryStore(db_path=self.journal.db_path)._ensure_ready()

    # ------------------------------------------------------------------ public entry points
    def erase_memory(self, node_uuid: str, *, dry_run: bool = False) -> dict[str, Any]:
        """Erase one memory node and every sidecar row addressable to it.

        Proceeds even when the graph node is already absent -- see the module docstring.
        """
        node_uuid = (node_uuid or "").strip()
        if not node_uuid:
            return {"reason": NOTHING_TO_ERASE, "subjects": 0}
        return self._run(
            subjects=[("NODE_UUID", node_uuid)],
            purge=ErasureSubjects(node_uuids=frozenset({node_uuid})),
            request={"targets": [node_uuid], "namespace": None},
            # Single-node erasure fences via the participant lock, so no competing merge or
            # delete can hold this uuid while the erasure is unresolved.
            target_uuid=node_uuid,
            target_key=None,
            delete_graph=lambda: (
                1 if self.graph_adapter.delete_memory(node_uuid) else 0
            ),
            dry_run=dry_run,
        )

    def erase_namespace(
        self, group_id: str, *, namespace: str | None = None, dry_run: bool = False
    ) -> dict[str, Any]:
        """Erase a whole namespace: its graph partition and all addressable sidecar content."""
        group_id = (group_id or "").strip()
        if not group_id:
            return {"reason": NOTHING_TO_ERASE, "subjects": 0}

        # Captured BEFORE deletion; afterwards the partition cannot be enumerated. Recorded as
        # normalized rows rather than a JSON blob in request_json, so a large namespace stays
        # bounded and resumable.
        member_uuids = self._capture_namespace_uuids(group_id, namespace)
        subject_rows: list[tuple[str, str]] = [("NAMESPACE", group_id)]
        if namespace and namespace != group_id:
            subject_rows.append(("NAMESPACE", namespace))
        subject_rows.extend(("NODE_UUID", u) for u in member_uuids)

        namespaces = {group_id} | ({namespace} if namespace else set())
        return self._run(
            subjects=subject_rows,
            purge=ErasureSubjects(
                node_uuids=frozenset(member_uuids),
                namespaces=frozenset(n for n in namespaces if n),
            ),
            request={
                # Deliberately NOT the member uuids: request_json stays bounded, and the
                # inventory table is the authority on membership.
                "targets": [],
                "namespace": group_id,
                "scoped_namespace": namespace,
                "member_count": len(member_uuids),
            },
            target_uuid=None,
            # A namespace erase is not a participant set, so it fences on its own key instead.
            target_key=f"erasure:namespace:{group_id}",
            delete_graph=lambda: int(
                self.graph_adapter.delete_namespace(group_id, namespace=namespace)
            ),
            dry_run=dry_run,
        )

    # ------------------------------------------------------------------ the saga
    def _run(
        self,
        *,
        subjects: list[tuple[str, str]],
        purge: ErasureSubjects,
        request: dict[str, Any],
        target_uuid: str | None,
        target_key: str | None,
        delete_graph: Any,
        dry_run: bool,
    ) -> dict[str, Any]:
        if dry_run:
            with self._connect() as conn:
                preview = purge_content(conn, purge, dry_run=True)
                conn.rollback()
            addressable = sum(preview.rows_affected.values())
            if not addressable:
                return {
                    "reason": NOTHING_TO_ERASE,
                    "dry_run": True,
                    "subjects": len(subjects),
                }
            return {
                "reason": ERASED,
                "dry_run": True,
                "subjects": len(subjects),
                "would_purge": preview.rows_affected,
                "unaddressable": list(preview.skipped_unaddressable),
            }

        op_id = uuidlib.uuid4().hex
        request = {
            "op_id": op_id,
            "kind": ERASURE_KIND,
            "requested_at": _utc_now_iso(),
            **request,
        }

        # PREPARE: intent + subject inventory, one transaction, one database file. Either both
        # are durable or neither is; a PREPARED erasure with no recorded subjects could not be
        # resumed after a crash.
        try:
            with self._connect() as conn:
                self.journal.prepare(
                    operation_kind=ERASURE_KIND,
                    request_json=_canonical(request),
                    target_uuid=target_uuid,
                    target_key=target_key,
                    op_id=op_id,
                    conn=conn,
                )
                self.subjects.record_subjects(op_id, subjects, conn=conn)
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s abstained: PREPARE failed: %s", ERASURE_KIND, exc)
            return {
                "reason": PREPARE_FAILED,
                "diagnostics": {"error": f"{type(exc).__name__}: {exc}"},
            }

        # From here the intent is durable, so readers already suppress these subjects.
        with owned_mutation(self.journal, op_id, operation_kind=ERASURE_KIND):
            return self._erase_and_verify(
                op_id=op_id, purge=purge, delete_graph=delete_graph
            )

    def _erase_and_verify(
        self,
        *,
        op_id: str,
        purge: ErasureSubjects,
        delete_graph: Any,
    ) -> dict[str, Any]:
        """Delete graph state, purge the sidecar, verify, then transition. Under the heartbeat.

        The graph delete is ALWAYS attempted and its own return value decides whether the graph
        held anything. An earlier version probed first with ``node_exists`` and skipped the
        delete when the probe said absent -- but the real graph adapter has no such method, so
        the probe's AttributeError fallback ("assume present") meant ``graph_already_absent``
        could never be reported outside tests, where the fake adapter did have it. Asking the
        mutation what it touched removes both the fake-only dependency and the window between
        probing and deleting.
        """
        try:
            graph_deleted = int(delete_graph() or 0)
        except Exception as exc:  # noqa: BLE001
            self.journal.record_attempt(op_id, error=f"{type(exc).__name__}: {exc}")
            raise

        # Purge and verification share one transaction: a verification that ran outside it could
        # observe a partially applied purge and report residue that is about to disappear.
        try:
            with self._connect() as conn:
                result = purge_content(conn, purge, dry_run=False)
                # Not a second dry_run: that counts rows the keys match, which a purge does
                # not change, so it could never confirm the content is gone.
                leftover = count_residual_content(conn, purge)
                self.subjects.mark_purged(op_id, conn=conn)
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            self.journal.record_attempt(op_id, error=f"{type(exc).__name__}: {exc}")
            raise

        # Every addressable row should now be redacted, so a second pass must find nothing. A
        # non-zero count means the purge did not cover what it claimed, which is a quarantine
        # case, not a success with a warning.
        if leftover:
            self.journal.mark_needs_review(
                op_id, observed_error=f"{RESIDUAL_CONTENT}: {sorted(leftover)}"
            )
            return {
                "reason": RESIDUAL_CONTENT,
                "op_id": op_id,
                "purged": result.rows_affected,
                "residual": leftover,
            }

        self.journal.mark_committed(op_id)
        # Three outcomes, not two. "The graph had nothing" and "nothing existed anywhere" are
        # different answers: the first still erased stored content, the second erased nothing at
        # all and must not report success, or delete_memory would claim an erasure that never
        # happened for a uuid that exists nowhere.
        purged_total = sum(result.rows_affected.values())
        if graph_deleted:
            reason = ERASED
        elif purged_total:
            reason = GRAPH_ALREADY_ABSENT
        else:
            reason = NOTHING_TO_ERASE
        return {
            "reason": reason,
            "op_id": op_id,
            "graph_deleted": graph_deleted,
            "purged": result.rows_affected,
            # Reported, never silently dropped: content the registry cannot address is content
            # this erasure did not remove.
            "unaddressable": list(result.skipped_unaddressable),
        }

    # ------------------------------------------------------------------ recovery
    def classify_prepared_row(
        self, row: "Mapping[str, Any]"
    ) -> tuple[str, dict[str, Any]]:
        """Decide what a PREPARED erasure row needs, without mutating anything."""
        op_id = str(row.get("op_id") or "")
        if not op_id:
            return WOULD_NEEDS_REVIEW, {"observed_error": "erasure row has no op_id"}
        try:
            subjects = self.subjects.fetch_subjects(op_id, unpurged_only=True)
        except Exception as exc:  # noqa: BLE001
            return WOULD_NEEDS_REVIEW, {
                "observed_error": f"subject inventory unreadable: {type(exc).__name__}: {exc}"
            }
        if not subjects:
            # PREPARE writes the intent and the inventory in one transaction, so a PREPARED row
            # with no unpurged subjects means the purge finished but the commit did not land.
            # Nothing left to erase; the row just needs its terminal state.
            return SKIP, {"reason": "no unpurged subjects"}
        return WOULD_REPLAY, {"subjects": len(subjects)}

    def replay_prepared_row(self, row: "Mapping[str, Any]") -> tuple[str, dict[str, Any]]:
        """Resume ONE crashed erasure. Unlike a delete replay, this really does re-execute.

        Re-running an erasure is safe in a way that re-running a delete is not: purging content
        that is already redacted is a no-op, and the durable subject inventory says exactly what
        to purge without asking a graph that may already be gone. That is what the inventory is
        for -- resuming from it is the design, not a fallback.

        **The caller must already hold the right to touch this row.** Ownership is deliberately
        not re-checked here; the only sound place for that check is inside the claim transaction
        the caller holds.
        """
        op_id = str(row.get("op_id") or "")
        outcome, diagnostics = self.classify_prepared_row(row)
        if outcome == SKIP:
            self.journal.mark_committed(op_id)
            return SKIPPED, dict(diagnostics)
        if outcome == WOULD_NEEDS_REVIEW:
            observed = str(diagnostics.get("observed_error"))
            self.journal.mark_needs_review(op_id, observed_error=observed)
            return DRIFTED, {"observed_error": observed}

        pending = self.subjects.fetch_subjects(op_id, unpurged_only=True)
        node_uuids = {
            str(r["subject_value"]) for r in pending if r["subject_type"] == "NODE_UUID"
        }
        namespaces = {
            str(r["subject_value"]) for r in pending if r["subject_type"] == "NAMESPACE"
        }
        purge = ErasureSubjects(
            node_uuids=frozenset(node_uuids), namespaces=frozenset(namespaces)
        )

        # Finish the graph side too: a crash before the graph delete leaves nodes whose erasure
        # intent is already committed. Both graph calls are no-ops when the target is gone.
        for namespace in sorted(namespaces):
            try:
                self.graph_adapter.delete_namespace(namespace)
            except Exception:  # noqa: BLE001
                logger.warning("erasure replay: namespace delete failed for %s", namespace,
                               exc_info=True)
        for node_uuid in sorted(node_uuids):
            try:
                self.graph_adapter.delete_memory(node_uuid)
            except Exception:  # noqa: BLE001
                logger.warning("erasure replay: node delete failed for %s", node_uuid,
                               exc_info=True)

        result = self._erase_and_verify(
            op_id=op_id, purge=purge, delete_graph=lambda: 0
        )
        if result.get("reason") == RESIDUAL_CONTENT:
            return DRIFTED, {
                "observed_error": f"{RESIDUAL_CONTENT}: {sorted(result.get('residual') or {})}"
            }
        return REPLAYED, {"purged": result.get("purged", {})}

    # ------------------------------------------------------------------ helpers
    def _connect(self) -> sqlite3.Connection:
        """One connection over the shared sidecar file, so intent and inventory are atomic."""
        return sqlite3.connect(self.journal.db_path)

    def _capture_namespace_uuids(self, group_id: str, namespace: str | None) -> list[str]:
        try:
            return [
                str(u)
                for u in (
                    self.graph_adapter.capture_namespace_uuids(group_id, namespace=namespace)
                    or []
                )
                if u
            ]
        except Exception:  # noqa: BLE001
            # Membership capture failing is not fatal: the namespace-keyed purge still reaches
            # namespace-keyed rows. It does mean uuid-keyed rows for members may be missed, so
            # it is logged loudly rather than swallowed.
            logger.warning(
                "erasure: could not capture namespace membership for %s; uuid-keyed sidecar "
                "rows for its members may survive",
                group_id,
                exc_info=True,
            )
            return []


def _canonical(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


__all__ = [
    "ERASED",
    "ERASURE_KIND",
    "ErasureCoordinator",
    "GRAPH_ALREADY_ABSENT",
    "NOTHING_TO_ERASE",
    "PREPARE_FAILED",
    "RESIDUAL_CONTENT",
]
