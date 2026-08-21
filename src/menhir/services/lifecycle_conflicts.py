"""Lifecycle conflict scanning, confirmation, and stale-resolution operations."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from uuid import uuid4

# Optional async callback: (processed, total, current_node_name) -> None
ProgressCallback = Callable[[int, int, str], Awaitable[None]]

from menhir.domain.memory_types import get_policy
from menhir.domain.models import FreshnessState, NodeScope
from menhir.domain.namespace import namespace_to_group_ids
from menhir.domain.temporal import parse_iso8601
from menhir.domain.utils import days_ago
from menhir.config import MemorySettings
from menhir.infrastructure.cypher import Cypher
from menhir.infrastructure.graphiti_client import GraphitiClient
from menhir.infrastructure.llm import LLMAdapter

if TYPE_CHECKING:
    from menhir.core.bootstrap import UnavailableGraphitiClient, UnavailableLLMAdapter
    from menhir.services.correlation_service import CorrelationService
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.pending_actions import PendingActionStore
from menhir.infrastructure.telemetry import record_lifecycle_action, record_memory_revision, record_mcp_event
from menhir.infrastructure.telemetry.store import telemetry_store

logger = logging.getLogger(__name__)

from menhir.services.lifecycle_models import (
    CONSOLIDATION_BATCH_SIZE,
    DECAY_BATCH_SIZE,
    DEMOTE_TTL_DAYS,
    ORPHAN_MAX_AGE_HOURS,
    PERSISTENT_EDGE_PROMOTE_THRESHOLD,
    SHARPNESS_COSINE_FLOOR,
    SHARPNESS_PROMOTE_THRESHOLD,
    SIMILARITY_CONFLICT_THRESHOLD,
    ConsolidationResult,
    DecayResult,
    ProgressCallback,
    _DEFAULT_COMPRESS_DAYS,
    _DEFAULT_COMPRESS_EDGE_COUNT,
    _DEFAULT_GONE_DAYS,
    _DEFAULT_GONE_EDGE_COUNT,
    _DEFAULT_GONE_SHARPNESS,
)

class LifecycleConflictMixin:
    async def scan_for_conflicts(
        self, *, limit: int = 150, cursor: str | None = None,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Run a similarity scan across persistent entity nodes to detect conflicts.

        Fetches up to `limit` PERSISTENT Entity nodes and checks each for
        high-similarity neighbours via `_check_contradictions_batch`. Pairs
        are routed by similarity:
        - 0.70–0.85: RELATES_TO edge (correlation, not conflict)
        - 0.85–0.95: pending_llm_review (genuine conflict candidate)
        - >0.95: merged (near-duplicate absorption)

        Only the conflict-range pairs increment the returned count.

        Args:
            limit: Max nodes to scan per batch.
            cursor: Resume token — pass ``next_cursor`` from the previous result
                to continue where the last scan left off.
            namespace: Optional silo to restrict the candidate set to. Opt-in --
                ``None``/empty scans every silo, which is the existing behavior. Pairing
                was already namespace-scoped per candidate (see the ``group_ids`` argument
                below), so this bounds WHICH nodes are scanned, not which may be paired.

        Returns dict with scanned, new_conflicts, next_cursor, done.
        """
        ns = str(namespace).strip() if namespace is not None else ""
        query = (
            Cypher()
            .match("(n:Entity)")
            .where(
                "n.scope = 'PERSISTENT'",
                "n.freshness <> 'GONE'",
                "n.conflict_group_id IS NULL",
            )
            .where_if(cursor is not None, "n.uuid > $cursor")
            .where_if(bool(ns), "coalesce(n.namespace, 'default') = $namespace")
            .return_raw("n.uuid AS uuid, n.name AS name, coalesce(n.summary, n.content, '') AS content, coalesce(n.namespace, 'default') AS namespace")
            .order_by("n.uuid")
            .limit()
            .build()
        )
        params: dict[str, Any] = {"limit": max(1, min(limit, 2000))}
        if cursor is not None:
            params["cursor"] = cursor
        if ns:
            params["namespace"] = ns
        rows = await asyncio.to_thread(self.graph_adapter.neo4j.execute, query, params=params)
        candidates = [dict(r) for r in rows]
        new_conflicts = await self._check_contradictions_batch(candidates)
        next_cursor = str(candidates[-1]["uuid"]) if candidates else None
        return {
            "scanned": len(candidates),
            "new_conflicts": new_conflicts,
            "next_cursor": next_cursor,
            "done": len(candidates) < limit,
        }

    async def confirm_pending_conflicts(
        self, *, limit: int = 20, verbose: bool = False, status: str = "pending_llm_review",
        namespace: str | None = None,
    ) -> dict:
        """Run LLM confirmation on similarity-flagged conflicts.

        Fetches groups with the given status (default: 'pending_llm_review'),
        calls the LLM to decide if each pair genuinely contradicts, then either
        promotes to 'unresolved' (surfaced to the user) or clears as
        'false_positive'.

        When called with status='unresolved', the LLM re-evaluates groups
        that are already in the unresolved queue. Confirmed contradictions
        stay unresolved; non-contradictions are resolved as keep_both with
        status 'false_positive'.

        Single-member spurious groups are cleared immediately without an LLM call.
        Groups where the LLM call fails are left in their current status for retry.

        Returns a dict with counts: confirmed, cleared, skipped_no_llm, errors.
        When verbose=True also includes a 'details' list with per-group results.
        """
        if self.llm is None:
            return {"confirmed": 0, "cleared": 0, "skipped_no_llm": 1, "errors": 0}

        # The namespace filter is applied at group SELECTION, which is what bounds every
        # mutation below: `resolve_conflict_group` acts by conflict_group_id on the whole
        # group, so the only way to keep this loop inside one silo is to never hand it a
        # group from another. Groups are namespace-homogeneous by construction (see
        # `list_conflict_groups`), so a selected group's members are the caller's own.
        groups = await asyncio.to_thread(
            self.graph_adapter.list_conflict_groups,
            status=status,
            limit=limit,
            namespace=namespace,
        )
        confirmed = cleared = errors = 0
        details: list[dict] = []

        for group in groups:
            group_id = str(group.get("group_id") or "")
            if not group_id:
                continue
            members = list(group.get("members") or [])

            if len(members) < 2:
                # Spurious single-member group — clear without LLM call
                await asyncio.to_thread(
                    self.graph_adapter.resolve_conflict_group,
                    group_id, action="keep_both", resolution_status="false_positive",
                )
                cleared += 1
                logger.info("Cleared single-member spurious conflict group=%s", group_id)
                if verbose:
                    details.append({
                        "group_id": group_id,
                        "result": "cleared",
                        "reason": "single_member",
                        "node_a": members[0].get("name") if members else None,
                        "node_b": None,
                    })
                continue

            a, b = members[0], members[1]
            name_a = str(a.get("name") or "")
            name_b = str(b.get("name") or "")
            try:
                is_conflict = await self.llm.confirm_contradiction(
                    name_a=name_a,
                    content_a=str(a.get("content") or ""),
                    name_b=name_b,
                    content_b=str(b.get("content") or ""),
                )
            except Exception:  # LLM call may fail for many reasons; log and skip
                logger.warning("LLM contradiction check failed for group=%s", group_id, exc_info=True)
                errors += 1
                if verbose:
                    details.append({
                        "group_id": group_id,
                        "result": "error",
                        "node_a": name_a,
                        "node_b": name_b,
                    })
                continue

            if is_conflict is None:
                # LLM failed or ambiguous — leave pending for retry
                errors += 1
                if verbose:
                    details.append({
                        "group_id": group_id,
                        "result": "error",
                        "reason": "llm_no_response",
                        "node_a": name_a,
                        "node_b": name_b,
                    })
                continue

            if is_conflict:
                await asyncio.to_thread(self.graph_adapter.set_conflict_group_status, group_id, "unresolved")
                confirmed += 1
                logger.info("LLM confirmed conflict group=%s (%r vs %r)", group_id, name_a, name_b)
                if verbose:
                    details.append({
                        "group_id": group_id,
                        "result": "confirmed",
                        "node_a": name_a,
                        "node_b": name_b,
                        "snippet_a": (a.get("content") or "")[:120],
                        "snippet_b": (b.get("content") or "")[:120],
                    })
            else:
                await asyncio.to_thread(
                    self.graph_adapter.resolve_conflict_group,
                    group_id, action="keep_both", resolution_status="false_positive",
                )
                # Record suppression for the reviewed pair only
                self._record_suppression(
                    str(a.get("uuid", "")), str(b.get("uuid", "")),
                    status="false_positive", group_id=group_id,
                    action="keep_both", reviewed_by="llm",
                )
                cleared += 1
                logger.info("LLM cleared false positive group=%s (%r vs %r)", group_id, name_a, name_b)
                if verbose:
                    details.append({
                        "group_id": group_id,
                        "result": "cleared",
                        "node_a": name_a,
                        "node_b": name_b,
                        "snippet_a": (a.get("content") or "")[:120],
                        "snippet_b": (b.get("content") or "")[:120],
                    })

        result: dict = {"confirmed": confirmed, "cleared": cleared, "skipped_no_llm": 0, "errors": errors}
        if verbose:
            result["details"] = details
        return result

    def auto_resolve_stale_conflicts(
        self,
        *,
        max_age_days: int = 14,
        limit: int = 50,
    ) -> int:
        """Auto-resolve conflict groups older than max_age_days using keep_both.

        Groups that have gone unresolved past the age threshold are unlikely to
        get manual attention. Resolving them as keep_both acknowledges both nodes
        as distinct valid facts and clears the conflict so they no longer appear
        in the unresolved queue.

        Returns the count of groups resolved.
        """
        groups = self.graph_adapter.list_conflict_groups(
            status="unresolved",
            limit=limit,
            # Ask for the OLDEST unresolved groups and push the age cutoff into the
            # database (CF-120). The newest-first default would return only fresh groups
            # that the filter below drops, so the stale backlog the job exists to drain
            # would never be fetched -- returning 0 forever while it grows.
            created_before=datetime.now(timezone.utc) - timedelta(days=max_age_days),
            oldest_first=True,
        )
        resolved = 0
        now = datetime.now(timezone.utc)
        for group in groups:
            created_at = group.get("created_at")
            if created_at is None:
                continue
            # `parse_iso8601` is the canonical read-back parser and handles all three shapes this
            # field actually arrives in: a neo4j.time.DateTime from the live driver, a stdlib
            # datetime, and an ISO string from a fake adapter.
            #
            # The hand-rolled version here branched on `hasattr(created_at, "tzinfo")` to decide
            # whether to parse -- but a neo4j.time.DateTime HAS tzinfo and is NOT a stdlib
            # datetime, so parsing was skipped and `now - created_at` raised TypeError. The bare
            # `except` swallowed it and `continue`d, skipping EVERY group: on the live driver this
            # job was a total no-op, not merely starved of old rows (CF-120).
            parsed = parse_iso8601(created_at)
            if parsed is None:
                # Fail closed: a timestamp we cannot age is left alone rather than auto-resolved.
                continue
            # Redundant guard: the cutoff is already pushed into Cypher, but a graph_adapter
            # that ignores the new parameters must still not resolve anything young.
            if (now - parsed).total_seconds() < max_age_days * 86400:
                continue
            group_id = str(group.get("group_id") or "")
            if not group_id:
                continue
            result = self.graph_adapter.resolve_conflict_group(
                group_id,
                action="keep_both",
                resolution_status="auto-resolved",
            )
            # Blanket age-based resolution — suppress all pairs
            member_uuids = result.get("member_uuids", [])
            for i, uuid_a in enumerate(member_uuids):
                for uuid_b in member_uuids[i + 1:]:
                    self._record_suppression(
                        uuid_a, uuid_b,
                        status="auto-resolved", group_id=group_id,
                        action="keep_both", reviewed_by="auto",
                    )
            resolved += 1
        return resolved

    def _record_suppression(
        self,
        uuid_a: str,
        uuid_b: str,
        *,
        status: str,
        group_id: str,
        action: str,
        reviewed_by: str,
    ) -> None:
        """Best-effort write of a conflict suppression row to SQLite."""
        if not uuid_a or not uuid_b:
            return
        try:
            telemetry_store.record_conflict_resolution(
                uuid_a=uuid_a, uuid_b=uuid_b,
                status=status, group_id=group_id,
                action=action, reviewed_by=reviewed_by,
            )
        except Exception:
            logger.debug("Failed to record conflict suppression for %s <-> %s", uuid_a, uuid_b, exc_info=True)
