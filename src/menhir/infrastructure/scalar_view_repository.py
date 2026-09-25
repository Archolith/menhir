"""Scalar-state View persistence, authority, and retirement operations."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from menhir.domain.recall_visibility import default_recall_visibility_cypher

try:  # neo4j is a hard runtime dep; guard the import so unit imports without the driver still load.
    from neo4j.exceptions import ConstraintError as _Neo4jConstraintError
except Exception:  # pragma: no cover - driver always present in the running service
    _Neo4jConstraintError = ()  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: Placeholder for the supporting-event count inside a kind's `summary_template`. The unchanged-value
#: provenance refresh (plan D2) computes the count INSIDE Cypher (the union must be atomic), so the
#: summary cannot be pre-rendered in Python — it is rendered as a template here and the count is
#: substituted server-side in the same statement that writes the union.
from menhir.infrastructure.view_models import (
    AdmissionAuditKind,
    CounterKind,
    ScalarHistoryKind,
    ScalarStateKind,
    TimelineKind,
    ViewClass,
    ViewKind,
    _COUNT_TOKEN,
    _SHARED_STAMPS,
    _checked_template,
    _counter_retrieval_text,
    _counter_summary,
    _day,
    _fmt,
    _is_older,
    _label_for,
    _log_missing_episodes,
    _normalize_entries,
    _normalize_episode_uuids,
    normalize_history_entry,
    _now,
    _parse_dt,
    _render_timeline,
    _scalar_norm,
    _timeline_sig,
    _timeline_surface,
)
from menhir.infrastructure.scalar_view_repository_edges import ScalarViewEdgeOpsMixin
from menhir.infrastructure.scalar_view_repository_lifecycle import ScalarViewRetireOpsMixin
from menhir.infrastructure.scalar_view_repository_reads import ScalarViewReadOpsMixin

class ScalarViewRepositoryMixin(
    ScalarViewReadOpsMixin, ScalarViewEdgeOpsMixin, ScalarViewRetireOpsMixin
):
    # ------------------------------------------------------------------ counter (QuantState) API

    def record_counter(
        self, *, subject: str, counter: str, value: float, namespace: str | None = None,
        valid_at: str | None = None, source: str = "consolidation", source_confidence: float = 0.6,
        episode_uuids: list[str] | None = None, name_embedding: list[float] | None = None,
        audit: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Upsert the current value for (subject, counter) as a kind='counter' View. Return dict
        keeps `qs_key`/`value` for back-compat. `audit` is provenance-only node metadata (never
        signature, never embedding) — e.g. the perception gate's decision receipt."""
        res = self.record(
            "counter", subject=subject, namespace=namespace, source=source,
            source_confidence=source_confidence, name_embedding=name_embedding, audit_props=audit,
            counter=counter, value=float(value), valid_at=valid_at,
            episode_uuids=list(episode_uuids or []),
        )
        res["qs_key"] = res["view_key"]
        res["value"] = float(value)
        return res

    def record_scalar_state(
        self, *, subject: str, subject_uuid: str, attribute: str, scope: str, value_kind: str,
        unit: str, value: Any, display: str | None = None, namespace: str | None = None,
        valid_at: str | None = None, source: str = "scalar-state",
        source_confidence: float = 0.6, episode_uuids: list[str] | None = None,
        name_embedding: list[float] | None = None, audit: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Upsert the current value for an entity-anchored typed slot as a kind='scalar_state' View.
        Identity is `subject_uuid` (the resolved entity), while `subject` is the display surface.

        The `audit` receipt (contributor ids + effective tier) is passed as BOTH audit_props (the
        create / new-version path) AND refresh_props (the unchanged-VALUE path), so a same-value fold
        whose contributors/authority changed still updates the stamped snapshot instead of leaving a
        stale tier on the node. Authority's source of truth is still the event log (read at recall
        time via ScalarStateService), but the node snapshot never contradicts it."""
        res = self.record(
            "scalar_state", subject=subject, subject_uuid=subject_uuid, namespace=namespace,
            source=source, source_confidence=source_confidence, name_embedding=name_embedding,
            audit_props=audit, refresh_props=audit, authoritative=True,
            attribute=attribute, scope=scope, value_kind=value_kind, unit=unit,
            value=value, display=display, valid_at=valid_at,
            episode_uuids=list(episode_uuids or []),
        )
        return res

    def list_scalar_state_views(
        self, *, subject_uuid: str, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """Current kind='scalar_state' Views for one resolved entity (by view_subject_uuid) — the
        rebuild's reconciliation input: it retires any slot not in the freshly-folded desired set."""
        ns_filter = "AND n.group_id = $ns" if namespace is not None else ""
        rows = self.neo4j.execute(
            f"""
            MATCH (n:Entity {{view_kind:'scalar_state', view_subject_uuid:$u}})
            WHERE coalesce(n.view_current, true) {ns_filter}
            RETURN n.uuid AS uuid, n.view_key AS view_key, n.ss_attribute AS attribute,
                   n.ss_scope AS scope, n.ss_kind AS value_kind, n.ss_unit AS unit,
                   n.group_id AS namespace
            """,
            {"u": subject_uuid, "ns": (namespace or "")},
        )
        return [dict(r) for r in rows]

    def fetch_current_scalar_view_for_slot(
        self, *, subject_uuid: str, attribute: str, scope: str, value_kind: str, unit: str,
        namespace: str | None = None,
    ) -> dict[str, Any] | None:
        """The CURRENT scalar_state View for one slot (Phase 4a.4 deterministic injection).

        Resolves the authoritative current value by SLOT-KEYED lookup, INDEPENDENT of embedding rank --
        the recall injection uses this so the current View is present even when it did not win ranking
        (the G5 stale-counter failure). Returns uuid + folded `ss_value` + the retrieval-surface name +
        view_key, or None when the slot has no current View (abstained/expired -> current unknown, by
        design). Scoped by namespace (C.4.4) so it never returns another tenant's View.

        The direct getter remains authoritative. Its result includes ``recall_eligible`` so the
        recall pipeline can reject an ineligible projection without hiding it from inspection.
        """
        ns_filter = "AND n.group_id = $ns" if namespace is not None else ""
        rows = self.neo4j.execute(
            f"""
            MATCH (n:Entity {{view_kind:'scalar_state', view_subject_uuid:$u,
                              ss_attribute:$attr, ss_kind:$vk}})
            WHERE coalesce(n.view_current, true) {ns_filter}
              AND coalesce(n.ss_scope, '') = $scope AND coalesce(n.ss_unit, '') = $unit
            RETURN n.uuid AS uuid, n.ss_value AS value, n.name AS name, n.view_key AS view_key,
                   n.ss_attribute AS attribute, n.view_subject_uuid AS subject_uuid,
                   toString(n.valid_at) AS valid_at,
                   CASE WHEN {default_recall_visibility_cypher("n")}
                        THEN true ELSE false END AS recall_eligible
            LIMIT 1
            """,
            {"u": subject_uuid, "attr": attribute, "vk": value_kind,
             "scope": scope or "", "unit": unit or "", "ns": namespace or ""},
        )
        return dict(rows[0]) if rows else None

    def fetch_scalar_authority_contributors(
        self, *, view_uuid: str, limit: int = 8, offset: int = 0,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Return a bounded, relation-labeled provenance window for one scalar View (7.J).

        Ordering is narration-safe: current anchor first, live deltas chronologically, then excluded
        prior anchors newest-first. ``total`` + ``next_offset`` form the expand-by-View cursor without
        dumping an unbounded timeline into ordinary recall.
        """
        safe_limit = max(1, min(int(limit), 50))
        safe_offset = max(0, int(offset))
        # CF-113: push SKIP/LIMIT into the row stream BEFORE collect(), so the database bounds what
        # it materialises instead of collecting every contributor then slicing in memory. The order
        # below is TOTAL: relation_rank, then valid_at (directional), then `a.assertion_id` as a
        # unique tiebreaker, so a page boundary can never duplicate or skip a row. `total` comes
        # from a separate count() aggregation, not size(all_rows).
        total_rows = self.neo4j.execute(
            """
            MATCH (v:Entity {uuid: $view_uuid})
                  -[r:CURRENT_ANCHOR|CONTRIBUTED_TO|SUPERSEDED_ANCHOR]->(a:TypedAssertion)
            WHERE $namespace IS NULL OR v.group_id = $namespace
            RETURN count(*) AS total
            """,
            params={"view_uuid": view_uuid, "namespace": namespace},
        )
        total = int(total_rows[0].get("total") or 0) if total_rows else 0
        rows = self.neo4j.execute(
            """
            MATCH (v:Entity {uuid: $view_uuid})
                  -[r:CURRENT_ANCHOR|CONTRIBUTED_TO|SUPERSEDED_ANCHOR]->(a:TypedAssertion)
            WHERE $namespace IS NULL OR v.group_id = $namespace
            WITH r, a,
                 CASE type(r) WHEN 'CURRENT_ANCHOR' THEN 0
                              WHEN 'CONTRIBUTED_TO' THEN 1 ELSE 2 END AS relation_rank
            ORDER BY relation_rank ASC,
                     CASE WHEN type(r) = 'CONTRIBUTED_TO' THEN a.valid_at END ASC,
                     CASE WHEN type(r) = 'SUPERSEDED_ANCHOR' THEN a.valid_at END DESC,
                     a.assertion_id ASC
            SKIP $offset
            LIMIT $limit
            WITH collect({
                assertion_id: a.assertion_id, relation: type(r), operation: a.operation,
                value: coalesce(a.value, a.value_json), stated_span: a.stated_span,
                valid_at: toString(a.valid_at), evidence_tier: a.evidence_tier,
                episode_uuid: a.episode_uuid
            }) AS contributors
            RETURN contributors
            """,
            params={"view_uuid": view_uuid, "limit": safe_limit, "offset": safe_offset,
                    "namespace": namespace},
        )
        row = dict(rows[0]) if rows else {}
        end = safe_offset + len(row.get("contributors") or [])
        return {
            "contributors": [dict(c) for c in (row.get("contributors") or [])],
            "total": total,
            "next_offset": end if end < total else None,
        }

    def scalar_view_has_user_foundation(
        self, *, view_uuid: str, namespace: str | None = None,
    ) -> bool:
        """G14 slice 3 (decision 7.G/10.G basis gate): does this scalar_state View's head trace to an
        ADMITTED USER STATEMENT?

        The head is computed from its CURRENT_ANCHOR absolute (Phase 3 edge). A real SOURCE FOUNDATION
        exists iff that anchoring assertion was FOUNDED by a `declarant='user'` :TurnEvidence -- the
        admission captured at write time (ADR 0001), drawn by the slice-1 grounding Cypher. This is
        INDEPENDENT of `evidence_tier` (which stays an extraction-confidence signal only, 10.G): a
        probabilistic `agent`-tier extraction of an admitted user statement HAS a foundation here, while
        an `agent` extraction with no user admission (e.g. an Episodic fixture, no FOUNDS edge) does NOT.
        Namespace-scoped so a foundation never leaks across tenants. Returns False when the View has no
        CURRENT_ANCHOR (edges undrawn) or no user FOUNDS -- the safe advisory default."""
        ns_filter = "AND te.namespace = $ns" if namespace is not None else ""
        rows = self.neo4j.execute(
            f"""
            MATCH (v:Entity {{uuid: $view_uuid}})-[:CURRENT_ANCHOR]->(a:TypedAssertion)
            MATCH (te:TurnEvidence {{declarant: 'user'}})-[:FOUNDS]->(a)
            WHERE true {ns_filter}
            RETURN count(te) > 0 AS founded
            """,
            {"view_uuid": view_uuid, "ns": namespace or ""},
        )
        return bool(rows and rows[0].get("founded"))

    def assertions_have_user_foundation(
        self, *, assertion_ids: list[str], namespace: str | None = None,
    ) -> bool:
        """G13/G14: True if ANY of the given assertions was FOUNDED by a `declarant='user'`
        :TurnEvidence -- the basis for an EXPIRY VERDICT to LEAD (the "I used to own X" statement was
        user-declared, not a probabilistic guess). Unlike scalar_view_has_user_foundation (which routes
        through a View's CURRENT_ANCHOR), an expired slot has NO current View, so the foundation is read
        directly off the expiry's contributor assertions. Namespace-scoped; False on empty input."""
        if not assertion_ids:
            return False
        ns_filter = "AND te.namespace = $ns" if namespace is not None else ""
        rows = self.neo4j.execute(
            f"""
            MATCH (te:TurnEvidence {{declarant: 'user'}})-[:FOUNDS]->(a:TypedAssertion)
            WHERE a.assertion_id IN $ids {ns_filter}
            RETURN count(te) > 0 AS founded
            """,
            {"ids": list(assertion_ids), "ns": namespace or ""},
        )
        return bool(rows and rows[0].get("founded"))

    def retire_counters_superseded_by_scalar(self, *, namespace: str) -> int:
        """Phase 1 two-subsystem reconciliation (decision 7.A/10.A): retire counter Views that
        DUPLICATE a typed scalar slot.

        A stated-total counter (measure-keyed) and a typed `absolute` assertion (attribute-keyed) that
        were CO-EXTRACTED from the SAME episode with the SAME value and a numeric kind are the SAME fact
        under two keys; the typed ScalarStateView is authoritative, so the competing counter must stop
        surfacing in recall (it is the `rare coins=20` vs `owned=37` stale-counter bug). Bridge (7.I,
        Rule A -- deterministic, no fuzzy text match): counter.view_subject == assertion.subject_display
        AND assertion.episode_uuid is in the counter's episode_uuids AND toFloat(assertion.value) ==
        counter.view_value AND assertion.value_kind is numeric AND the assertion's slot has a CURRENT
        scalar_state View. Value is matched at the SHARED episode (the assertion may since be superseded
        -- the counter=20 matches the E1 absolute=20 even though the slot's current head is 37).

        ABSTAINS when a counter value-matches MORE THAN ONE distinct typed slot in its episodes (e.g.
        "I have 20 coins and 20 cards" -- ambiguous which quantity the counter is). Retires in-place
        (view_current=false, expired_at, retired) keeping the version for audit. Namespace-scoped and
        idempotent (a re-run finds no current counter to retire). Returns the count retired."""
        rows = self.neo4j.execute(
            """
            MATCH (c:Entity {view_kind:'counter', group_id:$ns})
            WHERE coalesce(c.view_current, true) AND c.view_value IS NOT NULL
            MATCH (a:TypedAssertion {namespace:$ns, operation:'absolute'})
            WHERE NOT coalesce(a.binding_pending, false)
              AND a.value_kind IN ['count', 'money', 'measurement']
              AND a.episode_uuid IN coalesce(c.episode_uuids, [])
              AND a.subject_display = c.view_subject
              AND toFloat(a.value) = toFloat(c.view_value)
            MATCH (v:Entity {view_kind:'scalar_state', view_subject_uuid: a.subject_uuid, group_id:$ns})
            WHERE coalesce(v.view_current, true)
              AND v.ss_attribute = a.attribute
              AND coalesce(v.ss_scope, '') = coalesce(a.scope, '')
              AND v.ss_kind = a.value_kind
              AND coalesce(v.ss_unit, '') = coalesce(a.unit, '')
            WITH c, collect(DISTINCT v.view_key) AS slots
            WHERE size(slots) = 1
            SET c.view_current = false, c.qs_current = false, c.expired_at = datetime(),
                c.retired = true, c.retired_reason = 'superseded_by_typed_scalar_slot',
                c.last_accessed = datetime()
            RETURN count(c) AS retired
            """,
            {"ns": namespace},
        )
        return int(rows[0].get("retired") or 0) if rows else 0

    # ------------------------------------------------------------------ scalar_history API

    def record_scalar_history(
        self, *, subject: str, subject_uuid: str, attribute: str, scope: str, value_kind: str,
        unit: str, entries: list[Any], history_signature: str,
        operation_counts: dict[str, int], entry_count: int,
        payload_entry_count: int | None = None, omitted_entry_count: int | None = None,
        first_valid_at: str, last_valid_at: str,
        namespace: str | None = None, source: str = "scalar-history",
        source_confidence: float = 0.6, episode_uuids: list[str] | None = None,
        name_embedding: list[float] | None = None, audit: dict[str, Any] | None = None,
        recallable: bool = False,
    ) -> dict[str, Any]:
        """Upsert the current scalar_history View for an entity-anchored typed slot.

        Identity mirrors scalar_state: `subject_uuid`-anchored with the same slot hash.
        Written authoritatively (replaces provenance on unchanged-signature path).

        ``recallable`` is deliberately explicit and fail-closed. The projection service passes its
        ``scalar_history_enabled`` setting through this sink call; direct or older callers that omit
        the signal therefore remain OPERATOR. Inferring recallability from source or payload shape
        would silently defeat feature rollback.
        """
        res = self.record(
            "scalar_history", subject=subject, subject_uuid=subject_uuid, namespace=namespace,
            source=source, source_confidence=source_confidence, name_embedding=name_embedding,
            audit_props=audit, refresh_props=audit, authoritative=True,
            attribute=attribute, scope=scope, value_kind=value_kind, unit=unit,
            entries=entries, history_signature=history_signature,
            operation_counts=operation_counts, entry_count=entry_count,
            history_entry_count=entry_count,
            payload_entry_count=(len(entries) if payload_entry_count is None else payload_entry_count),
            omitted_entry_count=(
                max(0, int(entry_count) - len(entries))
                if omitted_entry_count is None else omitted_entry_count
            ),
            first_valid_at=first_valid_at, last_valid_at=last_valid_at,
            recallable=bool(recallable),
            episode_uuids=list(episode_uuids or []),
        )
        return res

    def list_scalar_history_views(
        self, *, subject_uuid: str, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """Current kind='scalar_history' Views for one resolved entity (by view_subject_uuid).

        Used by the rebuild's reconciliation: retire any history View whose slot is absent
        from the freshly-projected desired set."""
        ns_filter = "AND n.group_id = $ns" if namespace is not None else ""
        rows = self.neo4j.execute(
            f"""
            MATCH (n:Entity {{view_kind:'scalar_history', view_subject_uuid:$u}})
            WHERE coalesce(n.view_current, true) {ns_filter}
            RETURN n.uuid AS uuid, n.view_key AS view_key, n.ss_attribute AS attribute,
                   n.ss_scope AS scope, n.ss_kind AS value_kind, n.ss_unit AS unit,
                   n.group_id AS namespace
            """,
            {"u": subject_uuid, "ns": (namespace or "")},
        )
        return [dict(r) for r in rows]

    def list_scalar_history_views_for_namespace(
        self, *, namespace: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Bounded, namespace-scoped history-slot discovery independent of authority search.

        The advisory history lane may run while scalar-state authority is disabled, so it cannot
        depend on an observation hit or an already-ranked entity to discover eligible slots.
        """
        rows = self.neo4j.execute(
            f"""
            MATCH (n:Entity {{view_kind:'scalar_history'}})
            WHERE coalesce(n.view_current, true)
              // group_id is the tenancy property, and the empty string is the DEFAULT
              // silo's group id -- so it must not be coalesced to the NAME 'default'.
              // Matches the convention of the sibling list_scalar_history_views above.
              AND n.group_id = $namespace
              AND {default_recall_visibility_cypher("n")}
            RETURN n.uuid AS uuid, n.view_subject AS subject,
                   n.view_subject_uuid AS subject_uuid,
                   n.ss_attribute AS attribute, n.ss_scope AS scope,
                   n.ss_kind AS value_kind, n.ss_unit AS unit,
                   n.group_id AS namespace
            ORDER BY n.view_subject_uuid, n.ss_attribute, n.ss_scope,
                     n.ss_kind, n.ss_unit, n.uuid
            LIMIT $limit
            """,
            {"namespace": (namespace or ""), "limit": max(1, min(int(limit), 500))},
        )
        return [dict(r) for r in rows]

    def list_scalar_history_entries(
        self, *, view_uuid: str, offset: int = 0, limit: int = 16,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Paginated HISTORY_ENTRY contributors for one scalar_history View.

        Ordered by ordinal (chronological). Returns {entries, total, next_offset}."""
        safe_limit = max(1, min(int(limit), 50))
        safe_offset = max(0, int(offset))
        # CF-113: push SKIP/LIMIT before collect() so only the page is materialised. `he.ordinal` is
        # unique per View (one HISTORY_ENTRY edge per ordinal), so `ORDER BY he.ordinal ASC` is a
        # TOTAL order within the fixed view_uuid -- a page boundary can never duplicate or skip a row.
        total_rows = self.neo4j.execute(
            """
            MATCH (v:Entity {uuid: $view_uuid})
                  -[he:HISTORY_ENTRY]->(a:TypedAssertion)
            WHERE $namespace IS NULL OR v.group_id = $namespace
            RETURN count(*) AS total
            """,
            {"view_uuid": view_uuid, "namespace": namespace},
        )
        total = int(total_rows[0].get("total") or 0) if total_rows else 0
        rows = self.neo4j.execute(
            """
            MATCH (v:Entity {uuid: $view_uuid})
                  -[he:HISTORY_ENTRY]->(a:TypedAssertion)
            WHERE $namespace IS NULL OR v.group_id = $namespace
            OPTIONAL MATCH (te:TurnEvidence {turn_id: a.episode_uuid})
            OPTIONAL MATCH (source_ep:Episodic)-[:ADMITTED_ON]->(te)
            WITH he, a, te, collect(DISTINCT source_ep.uuid) AS admitted_episode_uuids
            ORDER BY he.ordinal ASC
            SKIP $offset
            LIMIT $limit
            WITH collect({
                assertion_id: a.assertion_id, ordinal: he.ordinal,
                operation: he.operation, value: coalesce(a.value, a.value_json),
                stated_span: a.stated_span, valid_at: he.valid_at,
                evidence_tier: a.evidence_tier,
                episode_uuid: CASE WHEN te IS NULL THEN coalesce(a.episode_uuid, '')
                                   ELSE coalesce(admitted_episode_uuids[0], '') END,
                source_episode_uuid: CASE WHEN te IS NULL THEN coalesce(a.episode_uuid, '')
                                          ELSE coalesce(admitted_episode_uuids[0], '') END,
                turn_id: CASE WHEN te IS NULL THEN '' ELSE coalesce(te.turn_id, '') END
            }) AS entries
            RETURN entries
            """,
            {"view_uuid": view_uuid, "offset": safe_offset, "limit": safe_limit,
             "namespace": namespace},
        )
        row = dict(rows[0]) if rows else {}
        end = safe_offset + len(row.get("entries") or [])
        return {
            "entries": [dict(e) for e in (row.get("entries") or [])],
            "total": total,
            "next_offset": end if end < total else None,
        }
