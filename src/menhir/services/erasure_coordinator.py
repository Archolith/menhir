"""Explicit erasure as a durable cross-store saga (CF-165).

Neo4j and the SQLite sidecar cannot commit one transaction together, so explicit erasure is
journaled. Subject membership is captured before graph deletion; PREPARE and the durable subject
inventory commit atomically; the graph and sidecar are then erased; addressable residue is verified
before COMMIT. A PREPARED operation is therefore resumable after a crash without rediscovering
subjects from a graph that may already be gone.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid as uuidlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from menhir.domain.namespace import normalize_namespace
from menhir.infrastructure.erasure_fence import (
    ERASURE_MARKER_LABEL,
    SealResult,
    release_memory,
    release_orphaned_erasing_ops,
    seal_memory,
    seal_namespace,
)
from menhir.infrastructure.erasure_subjects import ErasureSubjectStore
from menhir.infrastructure import operation_owner as oo
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.telemetry import connect_telemetry_db
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
    def erase_memory(
        self,
        node_uuid: str,
        *,
        dry_run: bool = False,
        caller_namespace: str | None = None,
        _follow_late_twin: bool = True,
    ) -> dict[str, Any]:
        """Erase one memory -- the node, its `:Episodic` twin, and every sidecar row addressable to them.

        A memory has two `:Episodic` nodes (Menhir's queue node and the Graphiti episode it resolves
        to); both carry the verbatim content, so erasing either erases both (audit Q1 / #119). The
        twins are resolved BEFORE prepare and recorded as subjects, so a crash after deleting one is
        replayed for all of them; each node runs the unchanged single-node graph cascade. A twin
        lookup failure propagates (fail closed) rather than erasing only half the memory.

        The captured evidence carrying the memory's words (its admitted turns, their projection
        episodes and the projections' twins) is resolved and erased the same way. Left behind, a
        turn is re-perceived by the next Phase 3 pass and the erased claim returns (#170 item 5).
        """
        node_uuid = (node_uuid or "").strip()
        if not node_uuid:
            return {"reason": NOTHING_TO_ERASE, "subjects": 0}
        scope = (caller_namespace or "").strip()
        if scope:
            # CF-326: the transport guards look up Entity/Episodic only, so a scoped caller holding
            # a foreign TurnEvidence turn_id got through. This check sits below every transport;
            # nothing is prepared, sealed or purged for a foreign target.
            keys_lookup = getattr(self.graph_adapter, "erase_target_namespace_keys", None)
            keys = list(keys_lookup(node_uuid) or []) if callable(keys_lookup) else []
            if keys and keys != [normalize_namespace(scope)]:
                return {
                    "reason": ERASED_INCOMPLETE,
                    "incomplete_reasons": ["foreign_target"],
                    "purged": {},
                }
        twin_lookup = getattr(self.graph_adapter, "episodic_twin_uuids", None)
        twins = [str(t) for t in (twin_lookup(node_uuid) if callable(twin_lookup) else []) if t]
        evidence_lookup = getattr(self.graph_adapter, "erasure_evidence_uuids", None)
        evidence: list[str] = []
        if callable(evidence_lookup):
            for base in [node_uuid, *twins]:
                found = (
                    evidence_lookup(base, caller_namespace=scope)
                    if scope
                    else evidence_lookup(base)
                )
                evidence.extend(str(u) for u in (found or []) if u)
        targets: list[str] = []
        for target in [node_uuid, *twins, *evidence]:
            if target not in targets:
                targets.append(target)
        # Derived facts/entities restating the memory (#423); fail closed like the twin lookup.
        preview_lookup = getattr(self.graph_adapter, "erasure_derived_preview", None)
        preview = preview_lookup(targets, twin_found=bool(twins)) if callable(preview_lookup) else {}
        if not isinstance(preview, dict):
            preview = {}
        derived = _new_derived()
        caller_namespace = (caller_namespace or "").strip() or None
        derived["caller_namespace"] = caller_namespace
        derived["twin_missing"] = bool(preview.get("twin_missing"))
        # Only entities fenced at PREPARE may be deleted; a merge holding one makes PREPARE fail.
        fence = sorted({str(u) for u in preview.get("entities_delete") or [] if u} - set(targets))
        derived["deletable"] = set(fence)
        derived_uuids = sorted(
            {str(u) for k in ("entities_delete", "edges_delete") for u in preview.get(k) or [] if u}
            - set(targets)
        )
        outcome = self._run(
            # DERIVED_UUID is purge-only: replay must never delete a derived node by uuid.
            subjects=[("NODE_UUID", target) for target in targets]
            + [("DERIVED_UUID", u) for u in derived_uuids],
            purge=_subjects_for_uuids(set(targets) | set(derived_uuids)),
            request={"targets": targets, "fence_uuids": fence, "namespace": None,
                     "caller_namespace": caller_namespace},
            target_uuid=node_uuid,
            target_key=None,
            delete_graph=lambda closure=(): self._delete_memory_targets(
                list(dict.fromkeys([*targets, *closure])), derived
            ),
            dry_run=dry_run,
            derived=derived,
            seal=self._seal_for(
                lambda neo4j, op_id: seal_memory(
                    neo4j, op_id, targets, caller_namespace=scope or None
                )
            ),
        )
        # A twin linked after the targets were resolved keeps the queue node as a flagged
        # tombstone (B-1). Erase it now; the sweep's pending retry covers a crash before this.
        pending = getattr(self.graph_adapter, "erasure_compensation_pending", None)
        if _follow_late_twin and not dry_run and callable(pending) and pending(node_uuid):
            follow = self.erase_memory(
                node_uuid, caller_namespace=caller_namespace, _follow_late_twin=False
            )
            clear = getattr(self.graph_adapter, "clear_erasure_compensation", None)
            if callable(clear):
                clear(node_uuid)
            outcome = {**outcome, "late_twin_erased": follow.get("reason")}
            # CF-325: a follow-up that is itself erased_incomplete left residue; the outer reply
            # must not stay a success class over it.
            if follow.get("reason") not in DELETION_SUCCEEDED_REASONS:
                outcome["reason"] = ERASED_INCOMPLETE
                outcome["incomplete_reasons"] = list(
                    dict.fromkeys(
                        [
                            *outcome.get("incomplete_reasons", []),
                            "late_twin_erase_failed",
                            *(follow.get("incomplete_reasons") or []),
                        ]
                    )
                )
        return outcome

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
            delete_graph=lambda closure=(): self._delete_namespace_graph(
                group_id, namespace=namespace
            ),
            dry_run=dry_run,
            seal=self._seal_for(
                lambda neo4j, op_id: seal_namespace(neo4j, op_id, str(namespace or group_id))
            ),
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
        derived: dict[str, Any] | None = None,
        seal: Any = None,
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
                **({"incomplete_reasons": ["unaddressable_content"]} if stranded else {}),
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
                derived=derived,
                seal=seal,
            )

    def _fence_neo4j(self) -> Any:
        """The graph repository the erasure fence runs on, or None when the adapter has none."""
        neo4j = getattr(self.graph_adapter, "neo4j", None)
        return neo4j if callable(getattr(neo4j, "execute_write", None)) else None

    def _seal_for(self, seal_fn: Any) -> Any:
        """Bind ``seal_fn(neo4j, op_id)`` to this coordinator's repository (None without one)."""
        neo4j = self._fence_neo4j()
        if neo4j is None:
            return None
        return lambda op_id: seal_fn(neo4j, op_id)

    def _sealed_closure_uuids(self, op_id: str) -> set[str]:
        """Every subject this op's seal marked (empty without a fence repository)."""
        neo4j = self._fence_neo4j()
        if neo4j is None:
            return set()
        rows = neo4j.execute(
            f"MATCH (m:{ERASURE_MARKER_LABEL} {{op_id: $op_id}}) "
            "RETURN DISTINCT m.subject_uuid AS uuid",
            params={"op_id": op_id},
        )
        return {str(r["uuid"]) for r in rows if r.get("uuid")}

    def release_orphaned_erasing_ops(self) -> list[str]:
        """Release fence entries whose journal row is terminal or absent (scheduler sweep)."""
        neo4j = self._fence_neo4j()
        if neo4j is None:
            return []
        return release_orphaned_erasing_ops(neo4j, self.journal)

    def _erase_and_verify(
        self,
        *,
        op_id: str,
        purge: ErasureSubjects,
        delete_graph: Any,
        derived: dict[str, Any] | None = None,
        seal: Any = None,
        release_fence: bool = False,
    ) -> dict[str, Any]:
        """Seal, delete graph state, release, purge the sidecar, verify, then transition."""
        if derived is None:
            derived = _new_derived()
        incomplete_reasons: list[str] = []
        sealed: SealResult | None = None
        try:
            if seal is not None:
                result_seal = seal(op_id)
                sealed = result_seal if isinstance(result_seal, SealResult) else None
            closure: list[str] = []
            if sealed is not None:
                closure = [u for u in sealed.closure_uuids if u not in purge.node_uuids]
                if closure:
                    # Durable before the deletes: a replay must reach what the seal found.
                    with self._connect() as conn:
                        self.subjects.record_subjects(
                            op_id, [("NODE_UUID", u) for u in closure], conn=conn
                        )
                        conn.commit()
                    purge = _subjects_for_uuids(
                        set(purge.node_uuids) | set(closure), namespaces=frozenset(purge.namespaces)
                    )
                if sealed.unfenced_inflight:
                    incomplete_reasons.append("unfenced_inflight")
            graph_deleted = int(delete_graph(closure) or 0)
        except Exception as exc:  # noqa: BLE001
            self.journal.record_attempt(
                op_id,
                error=f"{type(exc).__name__}: {exc}",
                owner_token=oo.process_owner_token(),
            )
            raise
        finally:
            if sealed is not None or release_fence:
                neo4j = self._fence_neo4j()
                if neo4j is not None:
                    try:
                        release_memory(neo4j, op_id)
                    except Exception:  # noqa: BLE001
                        # Markers stay and the orphan sweep releases the epoch later.
                        logger.warning("erasure %s: fence release failed", op_id, exc_info=True)
                        incomplete_reasons.append("fence_release_failed")

        # Derived/merge uuids found during the graph delete join the purge set. They exist only in
        # memory and the graph delete already removed what found them, so record them before
        # anything that can fail and leave the op PREPARED: replay cannot rediscover them (F2).
        extra = set(derived["extra_uuids"]) - set(purge.node_uuids)
        if extra:
            with self._connect() as conn:
                self.subjects.record_subjects(
                    op_id, [("DERIVED_UUID", u) for u in sorted(extra)], conn=conn
                )
                conn.commit()
            purge = _subjects_for_uuids(
                set(purge.node_uuids) | extra, namespaces=frozenset(purge.namespaces)
            )

        try:
            # #527: a queued observability row for this subject must land before the purge,
            # not after it. Timing out raises, so the erasure stays PREPARED and retryable.
            from menhir.infrastructure.telemetry.async_writer import TELEMETRY_WRITER
            from menhir.infrastructure.telemetry.helpers import _SQLITE_BUSY_TIMEOUT_S

            TELEMETRY_WRITER.drain_strict(_SQLITE_BUSY_TIMEOUT_S * 2)
            with self._connect() as conn:
                # This erasure's own row is still PREPARED (the purge skips unresolved rows), but
                # a recorded attempt's exception text can quote the content being erased.
                conn.execute(
                    "UPDATE graph_operations SET last_error = NULL WHERE op_id = ?", (op_id,)
                )
                # Op-keyed: the purge and census read subjects by subquery over erasure_subjects,
                # never as one bound variable per member (40k-member erases). Rows are blanked,
                # not deleted; secure_delete plus scheduled compaction removes the old bytes.
                op_purge = ErasureSubjects(op_id=op_id)
                result = purge_content(conn, op_purge, dry_run=False)
                leftover = count_residual_content(conn, op_purge)
                self.subjects.mark_purged(op_id, conn=conn)
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            self.journal.record_attempt(
                op_id,
                error=f"{type(exc).__name__}: {exc}",
                owner_token=oo.process_owner_token(),
            )
            raise

        # Best effort: fold the WAL so purged pages do not linger in it.
        import time as _time

        wal_checkpoint_complete = False
        try:
            checkpoint_conn = self._connect()
            try:
                # TRUNCATE waits out the busy handler on every attempt while a reader holds an old
                # snapshot; the default 5 s handler times 6 attempts past the caller's 30 s timeout.
                checkpoint_conn.execute("PRAGMA busy_timeout=250")
                for delay in (0.1, 0.2, 0.4, None):
                    row = checkpoint_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    if row is not None and int(row[0]) == 0:
                        wal_checkpoint_complete = True
                        break
                    if delay is None:
                        break
                    _time.sleep(delay)
            finally:
                checkpoint_conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("erasure %s: wal_checkpoint(TRUNCATE) failed: %s", op_id, exc)
        if not wal_checkpoint_complete:
            logger.warning("erasure %s: WAL checkpoint did not complete (busy reader/writer)", op_id)

        if leftover:
            self.journal.mark_needs_review(
                op_id,
                observed_error=f"{RESIDUAL_CONTENT}: {sorted(leftover)}",
                owner_token=oo.process_owner_token(),
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

        self.journal.mark_committed(op_id, owner_token=oo.process_owner_token())
        purged_total = sum(result.rows_affected.values())
        if graph_deleted:
            reason = ERASED
        elif purged_total:
            reason = GRAPH_ALREADY_ABSENT
        else:
            reason = NOTHING_TO_ERASE

        derived_summary = {
            "entities_deleted": len(derived["entities_deleted"]),
            "entities_flagged": len(derived["entities_flagged"]),
            "edges_deleted": len(derived["edges_deleted"]),
            "edges_flagged": len(derived["edges_flagged"]),
            "unknown_provenance": int(derived["unknown_provenance"]),
            "twin_missing": bool(derived["twin_missing"]),
            "merge_audits_tombstoned": int(derived["merge_audits_tombstoned"]),
            "merge_audits_unscrubbed": int(derived["merge_audits_unscrubbed"]),
        }
        for flag, why in (
            (stranded, "unaddressable_content"),
            (not stranded_known, "unaddressable_census_failed"),
            (derived_summary["unknown_provenance"] > 0, "unknown_provenance"),
            # An unparseable merge_audit entry naming the target may still hold its text.
            (derived_summary["merge_audits_unscrubbed"] > 0, "merge_audits_unscrubbed"),
            (derived_summary["twin_missing"], "twin_missing"),
            (not wal_checkpoint_complete, "wal_checkpoint_incomplete"),
        ):
            if flag and reason in (ERASED, GRAPH_ALREADY_ABSENT):
                incomplete_reasons.append(why)
        if incomplete_reasons:
            reason = ERASED_INCOMPLETE

        return {
            **({"incomplete_reasons": incomplete_reasons} if reason == ERASED_INCOMPLETE else {}),
            "wal_checkpoint_complete": wal_checkpoint_complete,
            "derived": derived_summary,
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
            self.journal.mark_committed(op_id, owner_token=oo.process_owner_token())
            return SKIPPED, dict(diagnostics)
        if outcome == WOULD_NEEDS_REVIEW:
            observed = str(diagnostics.get("observed_error"))
            self.journal.mark_needs_review(
                op_id, observed_error=observed, owner_token=oo.process_owner_token()
            )
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
        derived_uuids = {
            str(r["subject_value"]) for r in pending if r["subject_type"] == "DERIVED_UUID"
        }
        # CF-334: the seal commits before its closure is recorded here, so a crash in between
        # leaves closure-only twins known only to their markers. Read them back by op id.
        sealed_closure = self._sealed_closure_uuids(op_id) - node_uuids - derived_uuids
        if sealed_closure:
            self.subjects.record_subjects(
                op_id, [("NODE_UUID", u) for u in sorted(sealed_closure)]
            )
            node_uuids |= sealed_closure
        purge = _subjects_for_uuids(
            node_uuids | derived_uuids, namespaces=frozenset(namespaces)
        )

        # CF-271/F5: the replay mutates the graph and sidecar exactly as the first attempt does, so it
        # holds the same ownership heartbeat and revocation predicate the dispatcher's claim began.
        derived = _new_derived()
        # Still fenced: the row is PREPARED, so its participant locks (incl. fence_uuids) hold.
        derived["deletable"] = set(_request_fence_uuids(row))
        derived["caller_namespace"] = _request_caller_namespace(row)
        with owned_mutation(self.journal, op_id, operation_kind=ERASURE_KIND):
            result = self._erase_and_verify(
                op_id=op_id,
                purge=purge,
                delete_graph=lambda closure=(): self._replay_graph_deletes(
                    namespaces, node_uuids, derived=derived
                ),
                derived=derived,
                release_fence=True,
            )
        neo4j = self._fence_neo4j()
        if neo4j is not None:
            try:
                release_orphaned_erasing_ops(neo4j, self.journal)
            except Exception:  # noqa: BLE001
                logger.warning("orphaned erasing-op release failed", exc_info=True)
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

    def _delete_memory_targets(self, targets: list[str], derived: dict[str, Any]) -> int:
        """Delete each target with the whole erasure's uuids in scope, then scrub merge audits."""
        detailed = getattr(self.graph_adapter, "delete_memory_detailed", None)
        deleted = 0
        for target in targets:
            result = (
                detailed(
                    target,
                    erased_uuids=list(targets),
                    deletable_entity_uuids=sorted(derived.get("deletable") or ()),
                    **({"scope_namespace": derived["caller_namespace"]}
                       if derived.get("caller_namespace") else {}),
                )
                if callable(detailed)
                else None
            )
            if isinstance(result, dict):
                touched = bool(result.get("touched"))
                _fold_derived(derived, result)
            else:
                touched = bool(self.graph_adapter.delete_memory(target))
            deleted += 1 if touched else 0
        scrub = getattr(self.graph_adapter, "scrub_merge_audit_for_episodes", None)
        if callable(scrub):
            scrubbed = scrub(
                list(targets),
                sorted(derived["entities_deleted"]),
                **({"namespace": derived["caller_namespace"]}
                   if derived.get("caller_namespace") else {}),
            )
            if isinstance(scrubbed, dict):
                derived["merge_audits_tombstoned"] += int(
                    scrubbed.get("merge_audits_tombstoned") or 0
                )
                derived["merge_audits_unscrubbed"] += int(
                    scrubbed.get("merge_audits_unscrubbed") or 0
                )
                derived["extra_uuids"].update(
                    str(u) for u in scrubbed.get("merge_absorbed_uuids") or [] if u
                )
        return deleted

    def _replay_graph_deletes(
        self,
        namespaces: set[str],
        node_uuids: set[str],
        *,
        derived: dict[str, Any] | None = None,
    ) -> int:
        """Re-run every graph-side delete and propagate any failure to keep PREPARED unresolved."""
        deleted = 0
        purge_turn_evidence = getattr(self.graph_adapter, "purge_turn_evidence", None)
        for namespace in sorted(namespaces):
            deleted += int(self.graph_adapter.delete_namespace(namespace) or 0)
            if callable(purge_turn_evidence):
                deleted += int(purge_turn_evidence(namespace) or 0)
        if node_uuids:
            deleted += self._delete_memory_targets(
                sorted(node_uuids), derived if derived is not None else _new_derived()
            )
        return deleted

    def _connect(self) -> sqlite3.Connection:
        """One connection over the shared sidecar file, so intent and inventory are atomic."""
        # The shared seam sets secure_delete=ON, temp_store=MEMORY, WAL and the busy timeout.
        return connect_telemetry_db(self.journal.db_path)

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


def _new_derived() -> dict[str, Any]:
    """Mutable accumulator for derived-cleanup results across one erasure's graph deletes."""
    return {
        "entities_deleted": [],
        "entities_flagged": [],
        "edges_deleted": [],
        "edges_flagged": [],
        "unknown_provenance": 0,
        "twin_missing": False,
        "merge_audits_tombstoned": 0,
        "merge_audits_unscrubbed": 0,
        "extra_uuids": set(),
        "deletable": set(),
    }


def _request_fence_uuids(row: Mapping[str, Any]) -> list[str]:
    """Derived entities this erasure fenced at PREPARE (request_json.fence_uuids)."""
    try:
        request = json.loads(str(row.get("request_json") or "{}"))
    except (TypeError, ValueError):
        return []
    if not isinstance(request, dict):
        return []
    return [str(u) for u in request.get("fence_uuids") or [] if u]


def _request_caller_namespace(row: Mapping[str, Any]) -> str | None:
    """The namespace a memory erasure was scoped to (request_json.caller_namespace), if any."""
    try:
        request = json.loads(str(row.get("request_json") or "{}"))
    except (TypeError, ValueError):
        return None
    if not isinstance(request, dict):
        return None
    return str(request.get("caller_namespace") or "").strip() or None


def _fold_derived(derived: dict[str, Any], result: Mapping[str, Any]) -> None:
    for key in ("entities_deleted", "entities_flagged", "edges_deleted", "edges_flagged"):
        for item in result.get(f"derived_{key}_uuids") or []:
            if item and item not in derived[key]:
                derived[key].append(str(item))
    derived["unknown_provenance"] += int(result.get("derived_unknown_provenance") or 0)
    # Deleted derived items must be in the purge set, whether or not the preview saw them.
    derived["extra_uuids"].update(
        str(u)
        for key in ("derived_entities_deleted_uuids", "derived_edges_deleted_uuids")
        for u in result.get(key) or []
        if u
    )


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
