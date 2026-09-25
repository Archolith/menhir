"""Lifecycle, merge, revision, and operation-stat telemetry operations."""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from menhir.infrastructure.telemetry.helpers import _json_default, _span_days, _utc_now_iso
from menhir.infrastructure.telemetry.lifecycle_store_stats import TelemetryRetentionStatsMixin

logger = logging.getLogger(__name__)

#: Ceiling for an archived field value in `memory_revisions`. This is the ONLY surviving copy of
#: a node body once decay compresses it, so the old 2,000-character cap silently discarded the
#: tail of anything longer on the success path (CF-101). Sized to hold a whole memory body
#: rather than a preview; the truncation marker remains for the pathological case.
_MAX_REVISION_VALUE_LEN = 100_000


class MergeAuditUnavailable(RuntimeError):
    """The merge-audit read failed, so its result is unknown rather than empty (CF-205).

    Exists so a caller cannot accidentally treat a failed read as "no snapshot recorded". Any
    handler catching this must report the merge's recoverability as UNKNOWN; reporting it as
    unrecoverable is the exact wrong answer, because it discards recovery material that may
    well be sitting in the sidecar.
    """


# Facade: retention pruning and stats aggregation live in lifecycle_store_stats.py and are
# inherited via TelemetryRetentionStatsMixin so every symbol stays importable from here.
class TelemetryLifecycleStoreMixin(TelemetryRetentionStatsMixin):
    def record_lifecycle_event(
        self,
        *,
        recorded_at: str,
        component: str,
        event: str,
        state: str,
        episode_uuid: str | None,
        details_json: str | None,
    ) -> None:
        self._ensure_ready()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO lifecycle_events (
                    recorded_at,
                    phase,
                    event,
                    status,
                    episode_uuid,
                    details_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    recorded_at,
                    component,
                    event,
                    state,
                    episode_uuid,
                    details_json,
                ),
            )
            conn.commit()

    def fetch_recent_lifecycle_events(
        self,
        *,
        limit: int = 50,
        component: str | None = None,
        episode_uuid: str | None = None,
    ) -> list[dict[str, Any]]:
        self._ensure_ready()
        clauses: list[str] = []
        params: list[Any] = []
        if component:
            clauses.append("phase = ?")
            params.append(component)
        if episode_uuid:
            clauses.append("episode_uuid = ?")
            params.append(episode_uuid)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT id,
                       recorded_at,
                       phase AS component,
                       event,
                       status AS state,
                       episode_uuid,
                       details_json
                FROM lifecycle_events
                {where}
                ORDER BY recorded_at DESC, id DESC
                LIMIT ?
                """,
                (*params, max(1, min(limit, 200))),
            ).fetchall()
            return [dict(row) for row in rows]

    # "demote"/"delete" and their triggers back the F5 demote-with-TTL lifecycle
    # (LifecycleService: consolidation demote + TTL-expiry delete). They were added
    # to the caller but missing here, so every demote/delete telemetry write raised
    # ValueError and was silently dropped (the audit "chain of custody" for deletes).
    _VALID_ACTIONS = {"compress", "rehydrate", "gone", "demote", "delete"}
    _VALID_TRIGGERS = {
        "scheduler",
        "manual",
        "decay_sweep",
        "consolidation_demote",
        "demote_ttl_expiry",
    }
    _VALID_FIELDS = {"content", "summary", "scope", "freshness"}
    _VALID_CHANGED_BY = {"ingest", "consolidation", "conflict_resolution", "decay"}

    def record_lifecycle_action(
        self,
        *,
        action: str,
        node_uuid: str,
        session_id: str | None = None,
        trigger: str,
        before_freshness: str | None = None,
        after_freshness: str | None = None,
        llm_used: bool = False,
        duration_ms: int | None = None,
        notes: str | None = None,
    ) -> None:
        """Persist a compress/rehydrate/gone/demote/delete lifecycle action."""

        if action not in self._VALID_ACTIONS:
            raise ValueError(
                f"Invalid lifecycle action {action!r}; expected one of {self._VALID_ACTIONS}"
            )
        if trigger not in self._VALID_TRIGGERS:
            raise ValueError(
                f"Invalid trigger {trigger!r}; expected one of {self._VALID_TRIGGERS}"
            )
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO lifecycle_actions (
                        recorded_at, action, node_uuid, session_id, trigger,
                        before_freshness, after_freshness, llm_used, duration_ms, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _utc_now_iso(),
                        action,
                        node_uuid,
                        session_id,
                        trigger,
                        before_freshness,
                        after_freshness,
                        1 if llm_used else 0,
                        duration_ms,
                        notes,
                    ),
                )
                conn.commit()
        except sqlite3.Error:
            logger.warning(
                "Failed to record lifecycle action action=%s node=%s",
                action,
                node_uuid,
                exc_info=True,
            )

    def record_merge(
        self,
        *,
        survivor_uuid: str,
        absorbed_uuid: str,
        similarity: float | None,
        snapshot_json: str,
        survivor_namespace: str | None = None,
        absorbed_namespace: str | None = None,
    ) -> None:
        """Persist a merge to the durable sidecar audit.

        ``snapshot_json`` is the same audit entry written to ``survivor.merge_audit``
        in the graph (absorbed node's properties, relationships, and MENTIONS
        provenance). Unlike the graph property, this row survives deletion of the
        survivor, so an absorbed node stays recoverable even after decay or orphan
        cleanup removes the node that absorbed it.
        """
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO merge_audit (
                        recorded_at, survivor_uuid, absorbed_uuid, similarity, snapshot_json,
                        survivor_namespace, absorbed_namespace
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _utc_now_iso(),
                        survivor_uuid,
                        absorbed_uuid,
                        float(similarity) if similarity is not None else None,
                        snapshot_json,
                        survivor_namespace,
                        absorbed_namespace,
                    ),
                )
                conn.commit()
        except sqlite3.Error:
            logger.warning(
                "Failed to record merge audit survivor=%s absorbed=%s",
                survivor_uuid,
                absorbed_uuid,
                exc_info=True,
            )

    def fetch_merge_audit(
        self,
        *,
        survivor_uuid: str | None = None,
        absorbed_uuid: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read merge history from the durable sidecar.

        Filter by survivor (what did this node absorb?) or by absorbed uuid (what
        happened to this node?). With neither, returns the most recent merges.
        """
        self._ensure_ready()
        clauses: list[str] = []
        params: list[Any] = []
        if survivor_uuid:
            clauses.append("survivor_uuid = ?")
            params.append(survivor_uuid)
        if absorbed_uuid:
            clauses.append("absorbed_uuid = ?")
            params.append(absorbed_uuid)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    f"""
                    SELECT recorded_at, survivor_uuid, absorbed_uuid, similarity, snapshot_json
                    FROM merge_audit
                    {where}
                    ORDER BY recorded_at DESC
                    LIMIT ?
                    """,
                    tuple(params),
                ).fetchall()
                out = [dict(r) for r in rows]
        except sqlite3.Error as exc:
            # CF-205: this used to log and return []. An empty list is the caller's evidence that
            # NO snapshot exists, and both consumers turn that into a confident verdict --
            # ``legacy_unmerge_coordinator`` reports "this merge is NOT recoverable", and
            # ``merge_recoverability`` silently omits the merge from its degraded lane. A read
            # that failed is not evidence of absence, and here the wrong answer is the one that
            # discards recovery material. Raising is loud and correct; the caller may catch it
            # and report "unknown", but it can no longer mistake it for "none".
            #
            # Made reachable by the CF-1 logger fix: before that, this handler raised NameError
            # on the unbound logger, so the swallow never actually completed.
            logger.warning("Failed to fetch merge audit", exc_info=True)
            raise MergeAuditUnavailable(
                "merge audit read failed; this is not evidence that no snapshot exists"
            ) from exc

        # snapshot_json holds the absorbed node's content, and this read is keyed on uuids with
        # no graph-existence check -- the row outlives the node by design, which is what made it
        # readable after deletion. An erasure of EITHER participant suppresses it, matching the
        # two-party rule the purge uses; matching one side would serve the other's copy.
        from menhir.infrastructure.erasure_subjects import suppressed_node_uuids

        candidates = [r.get("survivor_uuid") for r in out]
        candidates += [r.get("absorbed_uuid") for r in out]
        suppressed = suppressed_node_uuids(self.db_path, candidates)
        if not suppressed:
            return out
        return [
            r
            for r in out
            if r.get("survivor_uuid") not in suppressed
            and r.get("absorbed_uuid") not in suppressed
        ]

    def record_memory_revision(
        self,
        *,
        node_uuid: str,
        field: str,
        old_value: str | None,
        new_value: str | None,
        changed_by: str,
        episode_uuid: str | None = None,
    ) -> None:
        """Persist a per-memory field revision for audit/debugging."""

        if field not in self._VALID_FIELDS:
            raise ValueError(
                f"Invalid revision field {field!r}; expected one of {self._VALID_FIELDS}"
            )
        if changed_by not in self._VALID_CHANGED_BY:
            raise ValueError(
                f"Invalid changed_by {changed_by!r}; expected one of {self._VALID_CHANGED_BY}"
            )
        max_val_len = _MAX_REVISION_VALUE_LEN
        if old_value and len(old_value) > max_val_len:
            old_value = old_value[:max_val_len] + "...[truncated]"
        if new_value and len(new_value) > max_val_len:
            new_value = new_value[:max_val_len] + "...[truncated]"
        self._ensure_ready()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO memory_revisions (
                        recorded_at, node_uuid, field, old_value, new_value,
                        changed_by, episode_uuid
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _utc_now_iso(),
                        node_uuid,
                        field,
                        old_value,
                        new_value,
                        changed_by,
                        episode_uuid,
                    ),
                )
                conn.commit()
        except sqlite3.Error:
            logger.warning(
                "Failed to record memory revision node=%s field=%s",
                node_uuid,
                field,
                exc_info=True,
            )
