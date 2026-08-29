"""Counter, timeline, metric, and generic View query operations."""

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
    _normalize_event_entries,
    _now,
    _parse_dt,
    _render_timeline,
    _scalar_norm,
    _timeline_sig,
    _timeline_surface,
)

class ViewQueryRepositoryMixin:
    def fetch_counter(self, *, subject: str, counter: str, namespace: str | None = None
                      ) -> dict[str, Any] | None:
        return self._fetch_current("counter", self._key(namespace, subject, counter))

    def list_counters(self, *, namespace: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        ns_filter = "AND n.group_id = $ns" if namespace is not None else ""
        rows = self.neo4j.execute(
            f"MATCH (n:Entity {{view_kind:'counter'}}) WHERE coalesce(n.view_current, true) {ns_filter} "
            "RETURN n.view_subject AS subject, n.qs_counter AS counter, n.view_value AS value, "
            "toString(n.valid_at) AS valid_at ORDER BY n.view_value DESC LIMIT $limit",
            {"ns": (namespace or ""), "limit": int(limit)},
        )
        return [dict(r) for r in rows]

    def history(self, *, subject: str, counter: str, namespace: str | None = None
                ) -> list[dict[str, Any]]:
        """Full value history for a counter (current + superseded), newest first."""
        rows = self.neo4j.execute(
            "MATCH (n:Entity {view_key:$k}) "
            "RETURN n.view_value AS value, coalesce(n.view_current, true) AS current, "
            "toString(n.valid_at) AS valid_at, toString(n.expired_at) AS expired_at "
            "ORDER BY n.valid_at DESC",
            {"k": self._key(namespace, subject, counter)},
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ timeline API

    def record_timeline(
        self, *, subject: str, entries: list[dict[str, Any]], namespace: str | None = None,
        source: str = "consolidation", source_confidence: float = 0.6,
        name_embedding: list[float] | None = None,
    ) -> dict[str, Any]:
        """Upsert the chronological View for `subject` from dated `entries`
        (`{when: ISO, what: str, episode_uuid?: str}`). Normalizes once, then records via the
        generic path — same node shape and supersession as a counter; only the value slot differs."""
        norm = _normalize_entries(entries)
        res = self.record(
            "timeline", subject=subject, namespace=namespace, source=source,
            source_confidence=source_confidence, name_embedding=name_embedding, entries=norm,
        )
        res["count"] = int(res.get("view_value") or 0)
        return res

    def fetch_timeline(self, *, subject: str, namespace: str | None = None) -> dict[str, Any] | None:
        """Current timeline View for `subject`: subject, count, parsed entries, valid_at."""
        return self._fetch_current("timeline", self._key(namespace, subject, "timeline"))

    # ------------------------------------------------------------------ event timeline API

    def record_event_timeline(
        self, *, subject: str, subject_uuid: str, predicate: str, entries: list[dict[str, Any]],
        domain: str | None = None, namespace: str | None = None,
        source: str = "event-history", source_confidence: float = 0.6,
        name_embedding: list[float] | None = None,
    ) -> dict[str, Any]:
        """Upsert the entity-anchored, predicate/domain event lane for `subject`.

        Identity is `subject_uuid` (like scalar_state), the lane discriminator is the collision-safe
        `timeline:event:` key, and the write is authoritative. Event entries are normalized EXACTLY
        once by the dedicated `_normalize_event_entries` (never the legacy `_normalize_entries`), and
        a blank `subject_uuid`/`predicate` fails closed so two lanes never over-merge."""
        if not subject_uuid or not str(subject_uuid).strip():
            raise ValueError("event timeline requires a non-blank subject_uuid")
        if not predicate or not str(predicate).strip():
            raise ValueError("event timeline requires a non-blank predicate")
        norm = _normalize_event_entries(entries)
        res = self.record(
            "timeline", subject=subject, subject_uuid=subject_uuid, namespace=namespace,
            source=source, source_confidence=source_confidence, name_embedding=name_embedding,
            entries=norm, predicate=predicate, domain=domain, authoritative=True,
        )
        res["count"] = int(res.get("view_value") or 0)
        return res

    def fetch_event_timeline(
        self, *, subject_uuid: str, predicate: str, domain: str | None = None,
        namespace: str | None = None,
    ) -> dict[str, Any] | None:
        """Current event-lane timeline View for (subject_uuid, predicate[, domain]). Blank
        subject_uuid/predicate fail closed (a blank predicate would look up a legacy subject-only
        timeline)."""
        if not subject_uuid or not str(subject_uuid).strip():
            raise ValueError("event timeline requires a non-blank subject_uuid")
        if not predicate or not str(predicate).strip():
            raise ValueError("event timeline requires a non-blank predicate")
        kind = self.KINDS["timeline"]
        disc = kind.key_discriminator({"predicate": predicate, "domain": domain})
        return self._fetch_current(
            "timeline", self._key(namespace, subject_uuid, disc, subject_uuid=subject_uuid)
        )

    def list_event_timeline_views(
        self, *, subject_uuid: str, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """Current Entity event-lane timeline Views for one resolved entity (by view_subject_uuid),
        i.e. only timeline rows with a nonblank `view_predicate`. Optional exact namespace filter.
        Deterministic predicate/domain/view_key order — the rebuild's reconciliation input."""
        if not subject_uuid or not str(subject_uuid).strip():
            raise ValueError("event timeline requires a non-blank subject_uuid")
        ns_filter = "AND n.group_id = $ns" if namespace is not None else ""
        rows = self.neo4j.execute(
            f"""
            MATCH (n:Entity {{view_kind:'timeline', view_subject_uuid:$u}})
            WHERE coalesce(n.view_current, true) {ns_filter}
              AND n.view_predicate IS NOT NULL AND trim(coalesce(n.view_predicate, '')) <> ''
            RETURN n.uuid AS uuid, n.view_key AS view_key, n.view_subject AS subject,
                   n.view_subject_uuid AS subject_uuid, n.view_predicate AS predicate,
                   n.view_domain AS domain, n.view_value AS count, toString(n.valid_at) AS valid_at
            ORDER BY n.view_predicate, n.view_domain, n.view_key
            """,
            {"u": subject_uuid, "ns": (namespace or "")},
        )
        return [dict(r) for r in rows]

    def retire_event_timeline(self, *, view_key: str) -> bool:
        """Expire a current event-lane timeline View with NO replacement. Retires IN PLACE only a
        current kind='timeline' row carrying a nonblank `view_predicate`, so a caller mistake can
        never retire a legacy subject-only timeline. Returns True if a version was retired."""
        rows = self.neo4j.execute(
            """
            MATCH (n:Entity {view_kind:'timeline', view_key:$k})
            WHERE coalesce(n.view_current, true)
              AND n.view_predicate IS NOT NULL AND trim(coalesce(n.view_predicate, '')) <> ''
            SET n.view_current = false, n.qs_current = false, n.expired_at = datetime(),
                n.retired = true, n.last_accessed = datetime()
            RETURN count(n) AS retired
            """,
            {"k": view_key},
        )
        return bool(rows and int(rows[0].get("retired") or 0) > 0)

    def draw_event_timeline_entries(
        self, *, view_uuid: str, entries: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Atomically rewrite a View's EVENT_HISTORY_ENTRY edges to the exact ordered assertion set.

        The ENTIRE event-entry set is validated/normalized by `_normalize_event_entries` BEFORE any
        existing edge is deleted, so an invalid required field never destroys the current projection.
        Edges run from the View to :TypedEventAssertion nodes matched by `assertion_key`; `ordinal` is
        the normalized order index and the edge carries valid_at/time_basis/evidence_tier.

        An empty normalized set clears existing edges and returns drawn=0/expected=0. A nonempty set
        returns BOTH `drawn` and `expected` so callers can fail closed when some assertions are
        missing — drawn<expected is surfaced, never concealed. Never uses `learned_at`."""
        norm = _normalize_event_entries(entries)
        if not norm:
            self.neo4j.execute(
                """
                MATCH (v:Entity {uuid: $view_uuid, view_kind:'timeline'})
                WHERE v.view_predicate IS NOT NULL AND trim(coalesce(v.view_predicate, '')) <> ''
                OPTIONAL MATCH (v)-[r:EVENT_HISTORY_ENTRY]->()
                DELETE r
                """,
                {"view_uuid": view_uuid},
            )
            return {"drawn": 0, "expected": 0}
        entry_params = [
            {
                "assertion_key": e["assertion_key"],
                "ordinal": i,
                "valid_at": e["when"],
                "time_basis": e["time_basis"],
                "evidence_tier": e["evidence_tier"],
            }
            for i, e in enumerate(norm)
        ]
        rows = self.neo4j.execute(
            """
            MATCH (v:Entity {uuid: $view_uuid, view_kind:'timeline'})
            WHERE v.view_predicate IS NOT NULL AND trim(coalesce(v.view_predicate, '')) <> ''
            OPTIONAL MATCH (v)-[r:EVENT_HISTORY_ENTRY]->()
            DELETE r
            WITH DISTINCT v
            UNWIND $entries AS entry
            MATCH (a:TypedEventAssertion {assertion_key: entry.assertion_key})
            MERGE (v)-[he:EVENT_HISTORY_ENTRY {ordinal: entry.ordinal}]->(a)
            SET he.valid_at = entry.valid_at, he.time_basis = entry.time_basis,
                he.evidence_tier = entry.evidence_tier
            RETURN count(*) AS drawn
            """,
            {"view_uuid": view_uuid, "entries": entry_params},
        )
        drawn = int(rows[0].get("drawn") or 0) if rows else 0
        return {"drawn": drawn, "expected": len(norm)}

    def list_event_timeline_entries(
        self, *, view_uuid: str, offset: int = 0, limit: int = 50,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Paginated EVENT_HISTORY_ENTRY contributors for one event-lane timeline View.

        Ordered by ordinal. Each entry projects assertion_key/source_key/predicate/domain/object_key/
        object_display, quote from stated_span, valid_at with a raw-string fallback, and
        episode_uuid/turn_evidence_uuid/time_basis/evidence_tier. Namespace filter is EXACT on the
        View. limit is bounded to 1..100 and offset is nonnegative; returns {entries, total,
        next_offset}. Never reads `learned_at`."""
        safe_limit = max(1, min(int(limit), 100))
        safe_offset = max(0, int(offset))
        rows = self.neo4j.execute(
            """
            MATCH (v:Entity {uuid: $view_uuid, view_kind:'timeline'})
                  -[he:EVENT_HISTORY_ENTRY]->(a:TypedEventAssertion)
            WHERE v.view_predicate IS NOT NULL AND trim(coalesce(v.view_predicate, '')) <> ''
              AND ($namespace IS NULL OR v.group_id = $namespace)
            WITH he, a
            ORDER BY he.ordinal ASC
            WITH collect({
                assertion_key: a.assertion_key,
                source_key: a.source_key,
                predicate: a.predicate,
                domain: a.domain,
                object_key: a.object_key,
                object_display: a.object_display,
                quote: a.stated_span,
                valid_at: coalesce(a.valid_at_raw, toString(a.valid_at)),
                episode_uuid: a.episode_uuid,
                turn_evidence_uuid: a.turn_evidence_uuid,
                time_basis: a.time_basis,
                evidence_tier: a.evidence_tier
            }) AS all_rows
            RETURN all_rows[$offset..($offset + $limit)] AS entries, size(all_rows) AS total
            """,
            {"view_uuid": view_uuid, "offset": safe_offset, "limit": safe_limit,
             "namespace": namespace},
        )
        row = dict(rows[0]) if rows else {}
        total = int(row.get("total", 0) or 0)
        end = safe_offset + len(row.get("entries") or [])
        return {
            "entries": [dict(e) for e in (row.get("entries") or [])],
            "total": total,
            "next_offset": end if end < total else None,
        }

    # ------------------------------------------------------------------ Metric API (operator-only)

    def record_metric(
        self, *, subject: str, counter: str, value: float, receipt_op_id: str,
        namespace: str | None = None, valid_at: str | None = None,
        source: str = "instrumentation", source_confidence: float = 0.6,
        audit: dict[str, Any] | None = None, node_uuid: str | None = None,
    ) -> dict[str, Any]:
        """Graph-only Metric write primitive (plan A3/A6).

        NOT called by producers directly -- the MetricWriteCoordinator (step 3) wraps it in the
        saga with a committed receipt. A Metric is a :Metric node, excluded from semantic recall
        by label, carrying type='METRIC' and NO name_embedding or episode provenance.
        `receipt_op_id` ties the node to its immutable metric_receipts row.
        """
        # Identity-stamp contract (plan A3/C2): a Metric with no receipt op id is an unreceipted
        # instrumentation node -- its value cannot be explained and the saga's after-state
        # fingerprint (which stamps metric_last_receipt_op_id) has nothing to point at. Reject it
        # here rather than write a provenance-less node. Defense in depth: the coordinator always
        # supplies a real op id, but record_metric is the graph-only primitive and must self-guard.
        if not (receipt_op_id and str(receipt_op_id).strip()):
            raise ValueError(
                "METRIC writes require a non-empty receipt_op_id (provenance/identity stamp): a "
                "Metric node must reference its immutable metric_receipts row (plan A3/C2)."
            )
        kind = self.KINDS["counter"]
        payload: dict[str, Any] = {"counter": counter, "value": float(value), "valid_at": valid_at}
        key = self._key(namespace, subject, kind.key_discriminator(payload))
        name, summary = kind.surface(subject, payload)
        # type='METRIC' overrides the CREATE's inline 'SEMANTIC' via SET n += $extra; the receipt
        # pointer is provenance-only (not in the signature, so it never triggers supersession).
        props = {
            **kind.write_props(subject, key, payload),
            "type": "METRIC",
            "metric_last_receipt_op_id": receipt_op_id,
        }
        res = self._write_version(
            kind=kind.name, key=key, subject=subject, name=name, summary=summary,
            sig=kind.signature(payload), extra_props=props, audit_props=audit,
            namespace=namespace, valid_at=kind.valid_at(payload) or _now(),
            source=source, source_confidence=source_confidence,
            episode_uuids=[], name_embedding=None,
            require_newer=kind.lww_register, view_class=ViewClass.METRIC,
            node_uuid=node_uuid,
            # Same-value fold -> no new version, but it IS a new receipt: repoint the node at it
            # (plan A4). Omitting this made every unchanged rewrite false-drift into NEEDS_REVIEW.
            refresh_props={"metric_last_receipt_op_id": receipt_op_id},
        )
        res["value"] = float(value)
        res["receipt_op_id"] = receipt_op_id
        return res

    def fetch_metric(self, *, subject: str, counter: str, namespace: str | None = None
                     ) -> dict[str, Any] | None:
        """Current Metric value for (subject, counter). Label-scoped to :Metric."""
        return self._fetch_current(
            "counter", self._key(namespace, subject, counter), view_class=ViewClass.METRIC
        )

    def fetch_metric_state(self, *, view_key: str) -> dict[str, Any] | None:
        """Full PROTECTED state of the current Metric for a key -- the saga's precondition read.

        Unlike fetch_metric (which returns the counter's public projection), this returns
        everything the operation owns and must fingerprint: identity, value, label set, type
        stamp, currentness, and the receipt pointer. Without labels/type here, a fingerprint
        cannot detect a Metric whose label or type was corrupted (plan E3).
        """
        # Cardinality-aware read (plan E, Phase 2). Returns the newest current version AND the
        # total count of current versions for the key. `current_count` is fingerprinted, so a graph
        # holding TWO current versions can never match the expected after-state (which asserts
        # exactly one) -- it routes to NEEDS_REVIEW instead. A bare LIMIT 1 hid that: it silently
        # returned the newest of a duplicate set and the saga read it as clean.
        #
        # The atomic create-and-supersede in _write_version means a crash can no longer PRODUCE two
        # currents; this is defense in depth so any PRE-EXISTING duplicate is caught, not papered
        # over. ORDER BY created_at DESC, uuid DESC keeps the chosen row deterministic.
        rows = self.neo4j.execute(
            "MATCH (n:Metric {view_key:$k}) WHERE coalesce(n.view_current, true) "
            "WITH n ORDER BY n.created_at DESC, n.uuid DESC "
            "WITH collect(n) AS cur "
            "RETURN size(cur) AS current_count, "
            "cur[0].uuid AS uuid, cur[0].view_value AS value, cur[0].type AS type, "
            "labels(cur[0]) AS labels, coalesce(cur[0].view_current, true) AS view_current, "
            "cur[0].metric_last_receipt_op_id AS receipt_op_id",
            {"k": view_key},
        )
        if not rows or int(rows[0].get("current_count") or 0) == 0:
            return None
        return dict(rows[0])

    def metric_history(self, *, subject: str, counter: str, namespace: str | None = None
                       ) -> list[dict[str, Any]]:
        """Full value history for a Metric (current + superseded), newest first."""
        rows = self.neo4j.execute(
            "MATCH (n:Metric {view_key:$k}) "
            "RETURN n.view_value AS value, coalesce(n.view_current, true) AS current, "
            "toString(n.valid_at) AS valid_at, toString(n.expired_at) AS expired_at, "
            "n.metric_last_receipt_op_id AS receipt_op_id "
            "ORDER BY n.valid_at DESC",
            {"k": self._key(namespace, subject, counter)},
        )
        return [dict(r) for r in rows]

    def list_metrics(self, *, namespace: str | None = None, source: str | None = None,
                     current_only: bool = True, limit: int = 100) -> list[dict[str, Any]]:
        """Operator listing of Metric nodes. The ONLY listing that returns Metrics -- the generic
        list_views / list_counters are label-scoped to :Entity, so instrumentation never leaks
        into a fact listing (plan A4)."""
        filters: list[str] = []
        if current_only:
            filters.append("coalesce(n.view_current, true)")
        if namespace is not None:
            filters.append("n.group_id = $ns")
        if source is not None:
            filters.append("n.source = $source")
        where = ("WHERE " + " AND ".join(filters)) if filters else ""
        rows = self.neo4j.execute(
            f"MATCH (n:Metric {{view_kind:'counter'}}) {where} "
            "RETURN n.uuid AS uuid, n.view_subject AS subject, n.qs_counter AS counter, "
            "n.view_value AS value, n.source AS source, toString(n.valid_at) AS valid_at, "
            "n.metric_last_receipt_op_id AS receipt_op_id "
            "ORDER BY n.view_value DESC LIMIT $limit",
            {"ns": (namespace or ""), "source": source, "limit": int(limit)},
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ generic read

    def list_views(self, *, kind: str | None = None, namespace: str | None = None,
                   limit: int = 100, include_operator: bool = False) -> list[dict[str, Any]]:
        """List current FACT Views, newest first.

        The default is safe for recall/bootstrap/context consumers and therefore requires the shared
        FACT/RECALL/current/live-provenance predicate. Operator tooling may opt in explicitly to list
        stamped OPERATOR Views as well; direct getters remain the authoritative inspection surface.
        """
        kind_filter = "AND n.view_kind = $kind" if kind is not None else ""
        ns_filter = "AND n.group_id = $ns" if namespace is not None else ""
        visibility = (
            "n.view_class = 'FACT' "
            "AND n.view_audience IN ['RECALL', 'OPERATOR'] "
            "AND coalesce(n.view_current, n.qs_current, false) "
            "AND NOT coalesce(n.retired, false)"
            if include_operator
            else default_recall_visibility_cypher("n")
        )
        rows = self.neo4j.execute(
            f"MATCH (n:Entity {{is_view:true}}) WHERE {visibility} "
            f"{kind_filter} {ns_filter} "
            "RETURN n.uuid AS uuid, n.view_kind AS kind, n.view_subject AS subject, "
            "n.view_value AS value, n.name AS name, n.group_id AS namespace, "
            "n.view_class AS view_class, n.view_subtype AS view_subtype, "
            "n.view_audience AS view_audience, "
            "toString(n.valid_at) AS valid_at ORDER BY n.valid_at DESC LIMIT $limit",
            {"kind": kind, "ns": (namespace or ""), "limit": int(limit)},
        )
        return [dict(r) for r in rows]
