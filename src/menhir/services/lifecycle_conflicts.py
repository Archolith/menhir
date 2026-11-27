"""Lifecycle conflict scanning, confirmation, and stale-resolution operations."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import TYPE_CHECKING, Any
from uuid import uuid4

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
from menhir.infrastructure.observability import last_provider_quota_exhaustion
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
        are routed by RRF rank score (not a cosine; see
        CorrelationService.classify_pair):
        - 0.70–0.85: RELATES_TO edge (correlation, not conflict)
        - 0.85–0.95: pending_llm_review (a band real two-lane RRF scores never land in)
        - >=0.95: judge-gated merge, else pending_llm_review. The merge itself runs only when
          MENHIR_AUTO_MERGE_MODE is "on"; otherwise the confirmed pair is left alone (the
          scan treats it as a dropped pair, not a conflict)
        Conflict and merge candidates can first be dropped by the similarity
        floor or the shared-episode veto.

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
        page_size = max(1, min(limit, 2000))
        params: dict[str, Any] = {"limit": page_size}
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
            # The clamped page size, not the caller's limit: comparing with a limit above the cap
            # reported done on every full page, so paging stopped early.
            "done": len(candidates) < page_size,
        }

    async def confirm_pending_conflicts(
        self, *, limit: int = 20, verbose: bool = False, status: str = "pending_llm_review",
        namespace: str | None = None,
    ) -> dict:
        """Run LLM confirmation on similarity-flagged conflicts, one member at a time (#654).

        Selects up to ``limit`` groups that have a member in ``status`` and fetches every
        member of each. Each member in ``status`` is compared with the group's other members
        until one pair is confirmed:

        - a confirmed pair moves the member to ``unresolved`` (already-unresolved members
          stay put);
        - when every pair is rejected the member leaves the group as ``false_positive`` and
          each rejected pair is suppressed. A group left with one member is dissolved;
        - an LLM failure leaves the member in ``status`` for the next run.

        Pair verdicts are cached within a run, so a two-member group costs one LLM call and
        ``limit`` also caps the number of LLM calls. Members are written with
        compare-and-set, so a member resolved or reviewed concurrently is left alone.

        Returns counts of groups: ``confirmed`` (a member confirmed), ``cleared`` (a member
        removed), ``errors`` (a member left unreviewed), plus ``skipped_no_llm``. A group of
        three can count as both confirmed and cleared. ``verbose=True`` adds per-member
        ``details``.
        """
        if self.llm is None:
            return {"confirmed": 0, "cleared": 0, "skipped_no_llm": 1, "errors": 0}

        # The namespace filter is applied at group SELECTION and again on every per-member
        # write, so this loop never reads or mutates another silo's members.
        groups = await asyncio.to_thread(
            self.graph_adapter.list_conflict_groups,
            status=status,
            limit=limit,
            namespace=namespace,
            include_all_members=True,
            # Oldest first: with enrichment adding pairs (#655), newest-first would starve
            # the oldest pending groups indefinitely.
            oldest_first=True,
        )
        confirmed = cleared = errors = 0
        details: list[dict] = []
        verdicts: dict[frozenset[str], bool | None] = {}
        calls_left = max(1, int(limit))
        stop = False

        async def _verdict(a: dict, b: dict, group_id: str) -> tuple[bool | None, bool]:
            """Return (verdict, was_called). None means the LLM gave no answer."""
            nonlocal calls_left
            key = frozenset((str(a.get("uuid") or ""), str(b.get("uuid") or "")))
            if key in verdicts:
                return verdicts[key], False
            calls_left -= 1
            try:
                result = await self.llm.confirm_contradiction(
                    name_a=str(a.get("name") or ""),
                    content_a=str(a.get("content") or ""),
                    name_b=str(b.get("name") or ""),
                    content_b=str(b.get("content") or ""),
                )
            except Exception:  # LLM call may fail for many reasons; log and leave pending
                logger.warning("LLM contradiction check failed for group=%s", group_id, exc_info=True)
                result = None
            verdicts[key] = None if result is None else bool(result)
            return verdicts[key], True

        for group in groups:
            if stop:
                break
            group_id = str(group.get("group_id") or "")
            if not group_id:
                continue
            members = [m for m in (group.get("members") or []) if str(m.get("uuid") or "")]

            if len(members) < 2:
                # Spurious single-member group — clear without LLM call
                await asyncio.to_thread(
                    self.graph_adapter.resolve_conflict_group,
                    group_id, action="keep_both", resolution_status="false_positive",
                    namespace=namespace,
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

            group_confirmed = group_cleared = group_error = False
            removed: set[str] = set()
            for member in members:
                if str(member.get("status") or "") != status:
                    continue
                uuid = str(member["uuid"])
                if calls_left <= 0 and any(
                    frozenset((uuid, str(o["uuid"]))) not in verdicts
                    for o in members
                    if str(o["uuid"]) != uuid and str(o["uuid"]) not in removed
                ):
                    # Out of budget: the remaining members wait for the next run, untouched.
                    stop = True
                    break
                rejected: list[dict] = []
                partner: dict | None = None
                unanswered = False
                for other in members:
                    other_uuid = str(other["uuid"])
                    if other_uuid == uuid or other_uuid in removed:
                        continue
                    verdict, called = await _verdict(member, other, group_id)
                    if verdict is None:
                        unanswered = True
                        # CF-262 F5: under a standing chat quota every remaining pair fails the
                        # same way; stop and leave them pending. A later successful chat call
                        # clears the record.
                        quota = last_provider_quota_exhaustion() if called else None
                        if quota is not None and quota.kind == "chat":
                            logger.error(
                                "Conflict review stopped: provider quota exhausted (%s); remaining "
                                "groups stay pending", quota.summary(),
                            )
                            stop = True
                            break
                        continue
                    if verdict:
                        partner = other
                        break
                    rejected.append(other)

                if partner is not None:
                    if status != "unresolved":
                        applied = await asyncio.to_thread(
                            self.graph_adapter.set_conflict_member_status,
                            group_id, uuid,
                            from_status=status, to_status="unresolved", namespace=namespace,
                        )
                        if not applied:
                            # Moved by a concurrent review or resolution; theirs stands.
                            continue
                    group_confirmed = True
                    logger.info(
                        "LLM confirmed conflict group=%s member=%s (%r vs %r)",
                        group_id, uuid, member.get("name"), partner.get("name"),
                    )
                    if verbose:
                        details.append({
                            "group_id": group_id,
                            "result": "confirmed",
                            "node_a": member.get("name"),
                            "node_b": partner.get("name"),
                            "snippet_a": (member.get("content") or "")[:120],
                            "snippet_b": (partner.get("content") or "")[:120],
                        })
                elif unanswered:
                    # A member is removed only when every pair was rejected; a missing
                    # verdict leaves it in place.
                    group_error = True
                    if verbose:
                        details.append({
                            "group_id": group_id,
                            "result": "error",
                            "reason": "llm_no_response",
                            "node_a": member.get("name"),
                            "node_b": None,
                        })
                elif rejected:
                    outcome = await asyncio.to_thread(
                        self.graph_adapter.remove_conflict_member,
                        group_id, uuid,
                        from_status=status, resolution_status="false_positive",
                        namespace=namespace,
                        reviewed_uuids=[str(o["uuid"]) for o in rejected] + sorted(removed),
                    )
                    if not outcome.get("removed"):
                        continue
                    removed.add(uuid)
                    group_cleared = True
                    # Suppress only the pairs the LLM actually rejected.
                    for other in rejected:
                        self._record_suppression(
                            uuid, str(other["uuid"]),
                            status="false_positive", group_id=group_id,
                            action="keep_both", reviewed_by="llm",
                        )
                    logger.info("LLM cleared false-positive member=%s group=%s", uuid, group_id)
                    if verbose:
                        details.append({
                            "group_id": group_id,
                            "result": "cleared",
                            "node_a": member.get("name"),
                            "node_b": None,
                        })
                    if outcome.get("dissolved"):
                        break
                if stop:
                    break

            confirmed += int(group_confirmed)
            cleared += int(group_cleared)
            errors += int(group_error)

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
        get manual attention. They leave the unresolved queue with status
        ``auto-resolved``, which recall still flags as ``unsettled_conflict`` (#652):
        the contradiction was confirmed and never decided, so readers keep seeing it.

        Returns the count of groups resolved.
        """
        groups = self.graph_adapter.list_conflict_groups(
            status="unresolved",
            limit=limit,
            # Every member, so a group still holding a pending member can be skipped.
            include_all_members=True,
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
            # A pending member was never reviewed: auto-resolving it would label it a confirmed
            # contradiction and suppress its pairs. Wait until review settles the group. The
            # repository repeats this check under a lock.
            if any(str(m.get("status") or "unresolved") != "unresolved"
                   for m in (group.get("members") or [])):
                continue
            member_uuids = self.graph_adapter.auto_resolve_settled_conflict_group(group_id)
            if not member_uuids:
                continue
            # Blanket age-based resolution — suppress all pairs
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
