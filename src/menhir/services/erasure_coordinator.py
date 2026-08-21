"""Explicit erasure as a durable cross-store saga (CF-165).

Neo4j and the SQLite sidecar cannot commit one transaction together, so explicit erasure is
journaled. Subject membership is captured before graph deletion; PREPARE and the durable subject
inventory commit atomically; the graph and sidecar are then erased; addressable residue is verified
before COMMIT. A PREPARED operation is therefore resumable after a crash without rediscovering
subjects from a graph that may already be gone.
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
    count_unaddressable_content,
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
from menhir.clock import utc_now_iso as _utc_now_iso

logger = logging.getLogger(__name__)

ERASURE_KIND = "EXPLICIT_ERASURE"

NOTHING_TO_ERASE = "nothing_to_erase"
GRAPH_ALREADY_ABSENT = "graph_already_absent"
ERASED = "erased"
PREPARE_FAILED = "prepare_failed"
RESIDUAL_CONTENT = "residual_content_after_purge"
MEMBERSHIP_CAPTURE_FAILED = "membership_capture_failed"
ERASED_INCOMPLETE = "erased_incomplete"

# Boolean compatibility callers cannot communicate incompleteness. They may therefore report
# success ONLY for complete outcomes. This deliberately excludes ERASED_INCOMPLETE: the E2E trace
# proved that current target content can become unaddressable, so "incomplete is only unrelated
# historical corpus residue" is not a valid authorization/presentation assumption.
DELETION_SUCCEEDED_REASONS = frozenset({ERASED, GRAPH_ALREADY_ABSENT})




def _subjects_for_uuids(
    uuids: "frozenset[str] | set[str]", *, namespaces: "frozenset[str]" = frozenset()
) -> ErasureSubjects:
    """Build the purge subject set from captured graph UUIDs.

    A graph UUID is stored under both ``node_uuid`` and ``episode_uuid`` sidecar columns, so the
    same captured set deliberately fills both buckets. ``session_ids`` stays empty: after feedback
    minimization no content-bearing registry entry depends on a session-only subject, and inventing
    a session->namespace mapping would cross ownership boundaries.
    """
    frozen = frozenset(uuids)
    return ErasureSubjects(
        node_uuids=frozen,
        episode_uuids=frozen,
        namespaces=frozenset(namespaces),
    )


@dataclass
class ErasureCoordinator:
    """Runs explicit erasure of a memory or whole namespace as a journaled saga."""

    graph_adapter: Any
    journal: GraphOperationsJournal = field(default_factory=GraphOperationsJournal)
    subjects: ErasureSubjectStore = field(default_factory=ErasureSubjectStore)

    def __post_init__(self) -> None:
        self.journal._ensure_ready()
        self.subjects._ensure_ready()
        from menhir.infrastructure.telemetry.store import McpTelemetryStore

        McpTelemetryStore(db_path=self.journal.db_path)._ensure_ready()

    # ------------------------------------------------------------------ public entry points
    def erase_memory(self, node_uuid: str, *, dry_run: bool = False) -> dict[str, Any]:
        """Erase one memory node and every sidecar row addressable to it."""
        node_uuid = (node_uuid or "").strip()
        if not node_uuid:
            return {"reason": NOTHING_TO_ERASE, "subjects": 0}
        return self._run(
            subjects=[("NODE_UUID", node_uuid)],
            purge=_subjects_for_uuids({node_uuid}),
            request={"targets": [node_uuid], "namespace": None},
            target_uuid=node_uuid,
            target_key=None,
            delete_graph=lambda: 1 if self.graph_adapter.delete_memory(node_uuid) else 0,
            dry_run=dry_run,
        )

    def erase_namespace(
        self, group_id: str, *, namespace: str | None = None, dry_run: bool = False
    ) -> dict[str, Any]:
        """Erase a namespace graph partition, TurnEvidence, and addressable sidecar content."""
        group_id = (group_id or "").strip()
        if not group_id:
            return {"reason": NOTHING_TO_ERASE, "subjects": 0}

        member_uuids = self._capture_namespace_uuids(group_id, namespace)
        if member_uuids is None:
            return {
                "reason": MEMBERSHIP_CAPTURE_FAILED,
                "namespace": group_id,
                "scoped_namespace": namespace,
                "diagnostics": {
                    "error": "namespace membership could not be enumerated; erasure abstained "
                    "rather than deleting the graph that is the only source of the subject set"
                },
            }

        subject_rows: list[tuple[str, str]] = [("NAMESPACE", group_id)]
        if namespace and namespace != group_id:
            subject_rows.append(("NAMESPACE", namespace))
        subject_rows.extend(("NODE_UUID", u) for u in member_uuids)
        namespaces = {group_id} | ({namespace} if namespace else set())

        return self._run(
            subjects=subject_rows,
            purge=_subjects_for_uuids(
                set(member_uuids), namespaces=frozenset(n for n in namespaces if n)
            ),
            request={
                "targets": [],
                "namespace": group_id,
                "scoped_namespace": namespace,
                "member_count": len(member_uuids),
            },
            target_uuid=None,
            target_key=f"erasure:namespace:{group_id}",
            delete_graph=lambda: self._delete_namespace_graph(group_id, namespace=namespace),
            dry_run=dry_run,
        )

    # ------------------------------------------------------------------ saga body
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
                stranded = count_unaddressable_content(conn)
            addressable = sum(preview.rows_affected.values())
            if not addressable:
                return {
                    "reason": NOTHING_TO_ERASE,
                    "dry_run": True,
                    "subjects": len(subjects),
                    "unaddressable_rows": stranded,
                }
            return {
                "reason": ERASED_INCOMPLETE if stranded else ERASED,
                "dry_run": True,
                "subjects": len(subjects),
                "would_purge": preview.rows_affected,
                "unaddressable": sorted({*preview.skipped_unaddressable, *stranded}),
                "unaddressable_rows": stranded,
            }

        op_id = uuidlib.uuid4().hex
        request = {
            "op_id": op_id,
            "kind": ERASURE_KIND,
            "requested_at": _utc_now_iso(),
            **request,
        }

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

        with owned_mutation(self.journal, op_id, operation_kind=ERASURE_KIND):
            return self._erase_and_verify(
                op_id=op_id,
                purge=purge,
                delete_graph=delete_graph,
            )

    def _erase_and_verify(
        self,
        *,
        op_id: str,
        purge: ErasureSubjects,
        delete_graph: Any,
    ) -> dict[str, Any]:
        """Delete graph state, purge the sidecar, verify, then transition."""
        try:
            graph_deleted = int(delete_graph() or 0)
        except Exception as exc:  # noqa: BLE001
            self.journal.record_attempt(op_id, error=f"{type(exc).__name__}: {exc}")
            raise

        try:
            with self._connect() as conn:
                result = purge_content(conn, purge, dry_run=False)
                leftover = count_residual_content(conn, purge)
                self.subjects.mark_purged(op_id, conn=conn)
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            self.journal.record_attempt(op_id, error=f"{type(exc).__name__}: {exc}")
            raise

        if leftover:
            self.journal.mark_needs_review(
                op_id,
                observed_error=f"{RESIDUAL_CONTENT}: {sorted(leftover)}",
            )
            return {
                "reason": RESIDUAL_CONTENT,
                "op_id": op_id,
                "purged": result.rows_affected,
                "residual": leftover,
            }

        try:
            with self._connect() as conn:
                stranded = count_unaddressable_content(conn)
            stranded_known = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "erasure %s: unaddressable census failed, refusing to report completeness: %s",
                op_id,
                exc,
            )
            stranded, stranded_known = {}, False

        self.journal.mark_committed(op_id)
        purged_total = sum(result.rows_affected.values())
        if graph_deleted:
            reason = ERASED
        elif purged_total:
            reason = GRAPH_ALREADY_ABSENT
        else:
            reason = NOTHING_TO_ERASE

        if reason in (ERASED, GRAPH_ALREADY_ABSENT) and (stranded or not stranded_known):
            reason = ERASED_INCOMPLETE

        return {
            "reason": reason,
            "op_id": op_id,
            "graph_deleted": graph_deleted,
            "purged": result.rows_affected,
            "unaddressable": sorted({*result.skipped_unaddressable, *stranded}),
            "unaddressable_rows": stranded,
            "unaddressable_known": stranded_known,
            # Generic, not session-specific: any future content class whose key dimensions are
            # absent from this erasure is surfaced rather than silently treated as covered.
            "not_covered": list(result.skipped_no_subjects),
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
            return SKIP, {"reason": "no unpurged subjects"}
        return WOULD_REPLAY, {"subjects": len(subjects)}

    def replay_prepared_row(self, row: "Mapping[str, Any]") -> tuple[str, dict[str, Any]]:
        """Resume one crashed erasure from its durable subject inventory.

        Graph deletion is part of the same fail-closed replay body as sidecar purge. The old replay
        caught graph-delete exceptions, logged them, and then committed the sidecar purge anyway;
        that could terminally mark an erasure whose graph state was still present.
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
            str(r["subject_value"])
            for r in pending
            if r["subject_type"] == "NODE_UUID"
        }
        namespaces = {
            str(r["subject_value"])
            for r in pending
            if r["subject_type"] == "NAMESPACE"
        }
        purge = _subjects_for_uuids(node_uuids, namespaces=frozenset(namespaces))

        result = self._erase_and_verify(
            op_id=op_id,
            purge=purge,
            delete_graph=lambda: self._replay_graph_deletes(namespaces, node_uuids),
        )
        if result.get("reason") == RESIDUAL_CONTENT:
            return DRIFTED, {
                "observed_error": (
                    f"{RESIDUAL_CONTENT}: {sorted(result.get('residual') or {})}"
                )
            }
        return REPLAYED, {"purged": result.get("purged", {})}

    # ------------------------------------------------------------------ helpers
    def _delete_namespace_graph(self, group_id: str, *, namespace: str | None) -> int:
        """Delete the graph partition and its raw TurnEvidence inside the durable saga.

        ``MemoryGraphAdapter.delete_namespace`` handles the group_id/scalar/event graph but
        TurnEvidence is keyed by its logical namespace. For every non-default silo the Graphiti
        group id is the namespace itself, so a direct coordinator call that omits the redundant
        ``namespace=`` argument must still purge that same TurnEvidence partition. Running both
        deletes here means a crash after either one leaves this erasure PREPARED and replayable
        instead of leaving raw prompts outside the journal.
        """
        deleted = int(self.graph_adapter.delete_namespace(group_id, namespace=namespace) or 0)
        logical_namespace = str(namespace or group_id).strip()
        purge_turn_evidence = getattr(self.graph_adapter, "purge_turn_evidence", None)
        if logical_namespace and callable(purge_turn_evidence):
            deleted += int(purge_turn_evidence(logical_namespace) or 0)
        return deleted

    def _replay_graph_deletes(self, namespaces: set[str], node_uuids: set[str]) -> int:
        """Re-run every graph-side delete and propagate any failure to keep PREPARED unresolved."""
        deleted = 0
        purge_turn_evidence = getattr(self.graph_adapter, "purge_turn_evidence", None)
        for namespace in sorted(namespaces):
            deleted += int(self.graph_adapter.delete_namespace(namespace) or 0)
            if callable(purge_turn_evidence):
                deleted += int(purge_turn_evidence(namespace) or 0)
        for node_uuid in sorted(node_uuids):
            deleted += 1 if self.graph_adapter.delete_memory(node_uuid) else 0
        return deleted

    def _connect(self) -> sqlite3.Connection:
        """One connection over the shared sidecar file, so intent and inventory are atomic."""
        return sqlite3.connect(self.journal.db_path)

    def _capture_namespace_uuids(
        self, group_id: str, namespace: str | None
    ) -> list[str] | None:
        """Enumerate the partition UUIDs, or ``None`` when membership could not be read."""
        try:
            return [
                str(u)
                for u in (
                    self.graph_adapter.capture_namespace_uuids(
                        group_id,
                        namespace=namespace,
                    )
                    or []
                )
                if u
            ]
        except AttributeError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning(
                "erasure: could not capture namespace membership for %s; abstaining before "
                "any graph deletion",
                group_id,
                exc_info=True,
            )
            return None


def _canonical(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


__all__ = [
    "DELETION_SUCCEEDED_REASONS",
    "ERASED",
    "ERASED_INCOMPLETE",
    "ERASURE_KIND",
    "ErasureCoordinator",
    "GRAPH_ALREADY_ABSENT",
    "MEMBERSHIP_CAPTURE_FAILED",
    "NOTHING_TO_ERASE",
    "PREPARE_FAILED",
    "RESIDUAL_CONTENT",
]
