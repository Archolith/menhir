"""Durable :TypedEventAssertion event log (Event History Phase 2B.1).

This repository is the DURABLE ASSERTION LOG ONLY for grounded categorical occurrences
(``menhir.domain.event_history.TypedEventAssertion``). It is NOT a View writer and runs no LLM:
every write is deterministic given a ``TypedEventAssertion`` value object. The timeline View and
``TimelineKind`` are later phases; here we only persist, supersede, and read back assertions.

Identity mirrors the three-level contract in ``event_history``:

* ``source_key`` — BINDING-STABLE locator (episode + span + ordinal, no subject_uuid). The head
  ``:TypedEventAssertionHead`` MERGEs on this, so a newer perceiver can correct the predicate/object
  on the same source span and still supersede via the same head, and a merge re-bind does not fork it.
* ``assertion_key`` — fully-interpreted identity (source_key + perceiver_version + namespace +
  predicate + object_key + domain + valid_at + time_basis), which exact replay dedups on.
* ``lane`` (``EventLane``) — namespace + subject_uuid + predicate + domain (normalized), the fold
  selection scope.

Authority/ordering are NEVER derived from ``learned_at`` or input order; valid world time is the
only temporal authority. Invalid/nonblank ``valid_at``/``learned_at`` are retained for audit as raw
strings (``valid_at_raw``/``learned_at_raw``) with a nullable parsed Neo4j temporal on the canonical
properties (``valid_at``/``learned_at``) parsed in Python — never passed through ``datetime()`` in
Cypher, so an unparseable source stamp cannot crash the statement.

Supersession is strict-rank (``perceiver_rank``), like ``TypedAssertion``. A different assertion
replaces CURRENT only when its perceiver_rank is strictly greater; same/lower-version disagreement
stays non-current audit. Exact replay is idempotent and may monotonically upgrade the evidence tier,
never downgrade. A ``source_key`` already bound to a different real subject fails closed with
``binding_mismatch`` and never moves CURRENT or adds an owner/provenance.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Iterable

from menhir.domain.event_history import EventLane, TypedEventAssertion
from menhir.domain.temporal import parse_iso8601
from menhir.domain.typed_assertion import evidence_rank, perceiver_rank

#: identity-contract version stamped on every head + assertion at ON CREATE. Fresh-only contract;
#: bump only on a real identity change. Deliberately independent of the scalar
#: ``typed_assertion.IDENTITY_VERSION`` (these are separate identity spaces).
EVENT_IDENTITY_VERSION: int = 1

#: sort sentinel for a nonblank-but-unparseable ``valid_at``: sorts AFTER any parseable world time,
#: so a malformed source stamp can never outrank a real one. UTC-aware for comparability with
#: ``parse_iso8601`` output.
_FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)


def _norm(value: str | None) -> str:
    """Blank-tolerant, trimmed, lowercased normalization for lane/identity text components."""
    return (value or "").strip().lower()


def _canonical(value: str) -> datetime | None:
    """Canonicalize a raw timestamp to a UTC-aware ``datetime`` for the canonical temporal property.
    Returns None when absent/unparseable (the raw string is preserved verbatim on ``*_raw`` for
    audit). Parsed stamps are normalized to UTC so ordering/comparison is offset-independent."""
    dt = parse_iso8601(value)
    return dt.astimezone(timezone.utc) if dt is not None else None


#: shared RETURN projection for every read. Raw source strings live on ``*_raw``; the canonical
#: ``valid_at``/``learned_at`` hold a nullable Neo4j temporal (or NULL). Hydration prefers ``*_raw``.
_EVENT_FIELDS = (
    "a.assertion_id AS assertion_id, a.source_key AS source_key, "
    "a.assertion_key AS assertion_key, a.subject_uuid AS subject_uuid, "
    "a.subject_display AS subject_display, a.predicate AS predicate, "
    "a.object_key AS object_key, a.object_display AS object_display, "
    "a.object_uuid AS object_uuid, a.domain AS domain, a.namespace AS namespace, "
    "a.valid_at_raw AS valid_at_raw, a.learned_at_raw AS learned_at_raw, "
    "a.valid_at AS valid_at, a.learned_at AS learned_at, "
    "a.stated_span AS stated_span, a.span_start AS span_start, a.span_end AS span_end, "
    "a.claim_ordinal AS claim_ordinal, a.episode_uuid AS episode_uuid, "
    "a.turn_evidence_uuid AS turn_evidence_uuid, a.time_basis AS time_basis, "
    "a.evidence_tier AS evidence_tier, a.perceiver_version AS perceiver_version, "
    "coalesce(a.metadata, '{}') AS metadata"
)


#: ONE atomic record statement: merge the head (the lock), observe the existing CURRENT for the
#: binding-mismatch guard, merge the assertion, HAS_VERSION, deterministic current-pointer behavior,
#: supersession, monotonic tier upgrade, pending->bound adoption, provenance, and binding_pending.
#: There is no read-before-write preflight — the head is DB-unique on the binding-stable source_key.
_RECORD_CYPHER = """
MERGE (h:TypedEventAssertionHead {source_key: $source_key})
  ON CREATE SET h.created_at = datetime(), h.identity_version = $identity_version,
                h.subject_uuid = $subject_uuid
// already-bound identity-mismatch — decided from the head's EXISTING current owner BEFORE any node
// create / CURRENT move / supersession / provenance, so a fresh higher-version node cannot escape the
// guard by lacking a binding_pending property yet. A claim durably bound to A must never be re-bound
// or re-owned by a record presenting a different subject (re-binding is the merge path's job).
WITH h
OPTIONAL MATCH (h)-[cr:CURRENT]->(cur:TypedEventAssertion)
WITH h, cr, cur,
     (cur IS NOT NULL AND NOT coalesce(cur.binding_pending, true)
      AND cur.subject_uuid <> $subject_uuid) AS binding_mismatch
MERGE (a:TypedEventAssertion {assertion_key: $assertion_key})
  ON CREATE SET
    a.assertion_id = randomUuid(), a.recorded_at = datetime(),
    a.identity_version = $identity_version,
    a.source_key = $source_key,
    a.subject_uuid = $subject_uuid, a.subject_display = $subject_display,
    a.predicate = $predicate_norm, a.domain = $domain_norm, a.object_key = $object_key_norm,
    a.object_display = $object_display, a.object_uuid = $object_uuid,
    a.namespace = $namespace_norm,
    // raw source strings are retained verbatim for audit; canonical temporal props carry a nullable
    // parsed value (None -> null) — never passed through datetime($raw), so unparseable stamps can't
    // throw.
    a.valid_at_raw = $valid_at_raw, a.valid_at = $valid_at_parsed,
    a.learned_at_raw = $learned_at_raw, a.learned_at = $learned_at_parsed,
    a.stated_span = $stated_span, a.span_start = $span_start, a.span_end = $span_end,
    a.claim_ordinal = $claim_ordinal,
    a.episode_uuid = $episode_uuid, a.turn_evidence_uuid = $turn_evidence_uuid,
    a.time_basis = $time_basis,
    a.evidence_tier = $evidence_tier, a.evidence_rank = $evidence_rank,
    a.perceiver_version = $perceiver_version, a.perceiver_rank = $perceiver_rank,
    a.metadata = $metadata_json,
    a.superseded = true, a.binding_pending = true, a._created = true
  ON MATCH SET a._created = false
MERGE (h)-[:HAS_VERSION]->(a)
// monotonic evidence-tier upgrade on an idempotent re-write (user confirms an agent value); never
// downgrades, and NEVER on a mismatch (a mismatched higher-tier rewrite must not upgrade A's claim).
FOREACH (_ IN CASE WHEN (NOT binding_mismatch) AND $evidence_rank > coalesce(a.evidence_rank, -1)
                   THEN [1] ELSE [] END |
    SET a.evidence_tier = $evidence_tier, a.evidence_rank = $evidence_rank)
WITH h, a, cr, cur, binding_mismatch,
     ((NOT binding_mismatch) AND cur IS NOT NULL AND cur.assertion_key <> $assertion_key
      AND $perceiver_rank > coalesce(cur.perceiver_rank, -1)) AS will_supersede
// a becomes current: no prior current, OR it strictly out-ranks it, OR it IS the current (idempotent)
// — but NEVER on a binding_mismatch (a mismatched version stays a non-current superseded audit node).
FOREACH (_ IN CASE WHEN (NOT binding_mismatch)
                        AND (cur IS NULL OR will_supersede
                             OR (cur IS NOT NULL AND cur.assertion_key = $assertion_key))
                   THEN [1] ELSE [] END |
    SET a.superseded = false
    MERGE (h)-[:CURRENT]->(a))
// replace + supersede the prior current when out-ranked (will_supersede is already mismatch-gated).
FOREACH (c IN CASE WHEN will_supersede THEN [cur] ELSE [] END |
    SET c.superseded = true, c.superseded_by = a.assertion_id, c.expired_at = datetime()
    MERGE (a)-[:SUPERSEDES]->(c)
    DELETE cr)
WITH h, a, will_supersede, binding_mismatch
// atomic provenance: resolve foundation by Episodic uuid OR TurnEvidence turn_id; resolve subject
// Entity; optionally resolve object Entity. binding_pending is true unless a foundation AND subject
// exist. Provenance edges (and the object link) are drawn ONLY when no binding mismatch.
OPTIONAL MATCH (e:Episodic {uuid: $episode_uuid})
OPTIONAL MATCH (te:TurnEvidence {turn_id: $turn_evidence_uuid})
OPTIONAL MATCH (n:Entity {uuid: $subject_uuid})
OPTIONAL MATCH (obj:Entity {uuid: $object_uuid})
WITH h, a, will_supersede, binding_mismatch, e, te, n, obj,
     ((e IS NOT NULL OR te IS NOT NULL) AND n IS NOT NULL) AS fully_bound,
     coalesce(a.binding_pending, true) AS was_pending
// pending -> bound adoption: assertion_key omits subject_uuid, so an idempotent replay of a still
// pending (sentinel) claim with a now-resolved real subject lands on the SAME node; adopt the
// resolved identity onto both the assertion and its head so the subject_uuid-keyed lane retrieves it.
// NEVER on a mismatch. (projection_pending belongs to later orchestration — not written here.)
FOREACH (_ IN CASE WHEN (NOT binding_mismatch) AND fully_bound AND was_pending THEN [1] ELSE [] END |
    SET a.subject_uuid = $subject_uuid, a.subject_display = $subject_display,
        h.subject_uuid = $subject_uuid)
// binding_pending is recomputed only when NOT a mismatch — a mismatch must never de-authorize A's
// bound claim by recomputing pending against the presented (wrong or unresolved) subject.
FOREACH (_ IN CASE WHEN binding_mismatch THEN [] ELSE [1] END |
    SET a.binding_pending = ((e IS NULL AND te IS NULL) OR n IS NULL))
// provenance links only when NOT a mismatch, so an already-bound claim can never acquire a second
// owner via a record. Missing object Entity alone does NOT make binding_pending (obj is optional).
FOREACH (_ IN CASE WHEN (NOT binding_mismatch) AND e IS NOT NULL THEN [1] ELSE [] END |
    MERGE (e)-[:GROUNDS]->(a))
FOREACH (_ IN CASE WHEN (NOT binding_mismatch) AND te IS NOT NULL THEN [1] ELSE [] END |
    MERGE (te)-[:FOUNDS]->(a))
FOREACH (_ IN CASE WHEN (NOT binding_mismatch) AND n IS NOT NULL THEN [1] ELSE [] END |
    MERGE (n)-[:HAS_EVENT_ASSERTION]->(a))
FOREACH (_ IN CASE WHEN (NOT binding_mismatch) AND obj IS NOT NULL THEN [1] ELSE [] END |
    MERGE (a)-[:EVENT_OBJECT]->(obj))
RETURN a.assertion_id AS assertion_id, coalesce(a._created, false) AS created,
       will_supersede AS superseded_prior, coalesce(a.binding_pending, false) AS binding_pending,
       binding_mismatch AS binding_mismatch
"""


class TypedEventAssertionRepository:
    """Neo4j durable event-assertion log (Event History Phase 2B.1). Small, self-contained Cypher
    store; no View writes, no LLM."""

    def __init__(self, neo4j: Any) -> None:
        self._neo4j = neo4j

    # ---- write -------------------------------------------------------------------------------

    def record_event_assertion(self, assertion: TypedEventAssertion) -> dict[str, Any]:
        """Persist one assertion in ONE atomic statement (head lock + mismatch guard + supersede +
        provenance + binding_pending). Returns exactly ``{assertion_id, created, superseded_prior,
        binding_pending, binding_mismatch}``."""
        rows = self._neo4j.execute(_RECORD_CYPHER, params=self._to_params(assertion))
        row = rows[0] if rows else {}
        return {
            "assertion_id": str(row.get("assertion_id") or ""),
            "created": bool(row.get("created")),
            "superseded_prior": bool(row.get("superseded_prior")),
            "binding_pending": bool(row.get("binding_pending")),
            "binding_mismatch": bool(row.get("binding_mismatch")),
        }

    def _to_params(self, assertion: TypedEventAssertion) -> dict[str, Any]:
        """Persist every field needed to reconstruct the assertion. Raw source stamps stay verbatim
        on ``*_raw``; the canonical temporal props carry a nullable parsed value (None when the raw
        string is absent/unparseable) so Cypher ``datetime()`` is never handed a bad raw string."""
        return {
            "source_key": assertion.source_key,
            "assertion_key": assertion.assertion_key,
            "identity_version": EVENT_IDENTITY_VERSION,
            "subject_uuid": assertion.subject_uuid,
            "subject_display": assertion.subject_display,
            "predicate_norm": _norm(assertion.predicate),
            "object_key_norm": _norm(assertion.object_key),
            "object_display": assertion.object_display,
            "object_uuid": assertion.object_uuid,
            "domain_norm": _norm(assertion.domain),
            "namespace_norm": _norm(assertion.namespace),
            "valid_at_raw": assertion.valid_at,
            "valid_at_parsed": _canonical(assertion.valid_at),
            "learned_at_raw": assertion.learned_at,
            "learned_at_parsed": _canonical(assertion.learned_at),
            "stated_span": assertion.stated_span,
            "span_start": assertion.span_start,
            "span_end": assertion.span_end,
            "claim_ordinal": assertion.claim_ordinal,
            "episode_uuid": assertion.episode_uuid,
            "turn_evidence_uuid": assertion.turn_evidence_uuid,
            "time_basis": assertion.time_basis,
            "evidence_tier": assertion.evidence_tier,
            "evidence_rank": evidence_rank(assertion.evidence_tier),
            "perceiver_version": assertion.perceiver_version,
            "perceiver_rank": perceiver_rank(assertion.perceiver_version),
            "metadata_json": json.dumps(assertion.metadata, sort_keys=True, default=str),
        }

    # ---- read --------------------------------------------------------------------------------

    def assertions_for_lane(
        self,
        lane: EventLane,
        *,
        include_superseded: bool = False,
        materializable_only: bool = False,
    ) -> list[TypedEventAssertion]:
        """Reconstructed assertions in one lane. Lane match is EXACT normalized namespace +
        subject_uuid + predicate + domain. Default excludes superseded versions but retains
        binding-pending audit rows unless ``materializable_only=True``. Deterministic order: valid
        world times ascending first (then assertion_key), invalid world times LAST — never by
        ``learned_at``."""
        params = {
            "namespace": _norm(lane.namespace),
            "subject_uuid": _norm(lane.subject_uuid),
            "predicate": _norm(lane.predicate),
            "domain": _norm(lane.domain),
        }
        preds = [
            "toLower(trim(coalesce(a.namespace, ''))) = $namespace",
            "toLower(trim(coalesce(a.subject_uuid, ''))) = $subject_uuid",
            "toLower(trim(coalesce(a.predicate, ''))) = $predicate",
            "toLower(trim(coalesce(a.domain, ''))) = $domain",
        ]
        if not include_superseded:
            preds.append("NOT coalesce(a.superseded, false)")
        if materializable_only:
            preds.append("NOT coalesce(a.binding_pending, false)")
        rows = self._neo4j.execute(
            f"""
            MATCH (a:TypedEventAssertion)
            WHERE {' AND '.join(preds)}
            RETURN {_EVENT_FIELDS}
            """,
            params=params,
        )
        events = [self._hydrate(dict(r)) for r in rows]
        return self._sort_by_valid(events)

    def assertions_for_subject_predicate(
        self,
        subject_uuid: str,
        predicate: str,
        *,
        namespace: str | None = None,
        include_superseded: bool = False,
        materializable_only: bool = True,
    ) -> list[TypedEventAssertion]:
        """Reconstructed assertions for an exact normalized ``subject_uuid`` + ``predicate`` across
        ALL actual domain lanes (optionally namespace-scoped). No domain filter and no domain
        override — the fold later groups by each real domain. Default excludes superseded AND
        binding-pending. Deterministic order reuses ``_sort_by_valid``: valid world times ascending
        first (then assertion_key), invalid times LAST — never by ``learned_at``/input order."""
        if not subject_uuid or not subject_uuid.strip():
            raise ValueError("subject_uuid must be non-blank")
        if not predicate or not predicate.strip():
            raise ValueError("predicate must be non-blank")
        params = {
            "subject_uuid": _norm(subject_uuid),
            "predicate": _norm(predicate),
        }
        preds = [
            "toLower(trim(coalesce(a.subject_uuid, ''))) = $subject_uuid",
            "toLower(trim(coalesce(a.predicate, ''))) = $predicate",
        ]
        if namespace is not None:
            params["namespace"] = _norm(namespace)
            preds.append("toLower(trim(coalesce(a.namespace, ''))) = $namespace")
        if not include_superseded:
            preds.append("NOT coalesce(a.superseded, false)")
        if materializable_only:
            preds.append("NOT coalesce(a.binding_pending, false)")
        rows = self._neo4j.execute(
            f"""
            MATCH (a:TypedEventAssertion)
            WHERE {' AND '.join(preds)}
            RETURN {_EVENT_FIELDS}
            """,
            params=params,
        )
        events = [self._hydrate(dict(r)) for r in rows]
        return self._sort_by_valid(events)

    def assertion_by_key(self, assertion_key: str) -> TypedEventAssertion | None:
        """Reconstruct the assertion with ``assertion_key``, or None."""
        rows = self._neo4j.execute(
            f"""
            MATCH (a:TypedEventAssertion {{assertion_key: $assertion_key}})
            RETURN {_EVENT_FIELDS}
            """,
            params={"assertion_key": assertion_key},
        )
        return self._hydrate(dict(rows[0])) if rows else None

    def current_for_source(self, source_key: str) -> TypedEventAssertion | None:
        """Reconstruct the CURRENT assertion for a binding-stable source_key (through the head), or
        None if the source claim has no current version."""
        rows = self._neo4j.execute(
            f"""
            MATCH (h:TypedEventAssertionHead {{source_key: $source_key}})-[:CURRENT]->(a:TypedEventAssertion)
            RETURN {_EVENT_FIELDS}
            """,
            params={"source_key": source_key},
        )
        return self._hydrate(dict(rows[0])) if rows else None

    # ---- schema ------------------------------------------------------------------------------

    def activate(self) -> dict[str, Any]:
        """Bring the event-assertion DDL online. Idempotent (every statement is IF (NOT) EXISTS):
        unique ``source_key`` on the head, unique ``assertion_key``/``assertion_id`` on the
        assertion, a unique ``group_id`` on the :EventConsolidationWatermark cursor, plus
        lane/source/episode/valid-time indexes. No destructive migration or purge."""
        queries = [
            "CREATE CONSTRAINT typed_event_assertion_head_source_key_unique IF NOT EXISTS FOR (h:TypedEventAssertionHead) REQUIRE h.source_key IS UNIQUE",
            "CREATE CONSTRAINT typed_event_assertion_key_unique IF NOT EXISTS FOR (a:TypedEventAssertion) REQUIRE a.assertion_key IS UNIQUE",
            "CREATE CONSTRAINT typed_event_assertion_id_unique IF NOT EXISTS FOR (a:TypedEventAssertion) REQUIRE a.assertion_id IS UNIQUE",
            "CREATE CONSTRAINT event_consolidation_watermark_group_id_unique IF NOT EXISTS FOR (w:EventConsolidationWatermark) REQUIRE w.group_id IS UNIQUE",
            "CREATE INDEX typed_event_assertion_lane_idx IF NOT EXISTS FOR (a:TypedEventAssertion) ON (a.namespace, a.subject_uuid, a.predicate, a.domain)",
            "CREATE INDEX typed_event_assertion_source_idx IF NOT EXISTS FOR (a:TypedEventAssertion) ON (a.source_key)",
            "CREATE INDEX typed_event_assertion_episode_idx IF NOT EXISTS FOR (a:TypedEventAssertion) ON (a.episode_uuid)",
            "CREATE INDEX typed_event_assertion_valid_at_idx IF NOT EXISTS FOR (a:TypedEventAssertion) ON (a.valid_at)",
        ]
        for query in queries:
            self._neo4j.execute(query)
        return {"queries_executed": len(queries)}

    # ---- hydration ---------------------------------------------------------------------------

    @staticmethod
    def _int_or(value: Any, default: int) -> int:
        """Coerce a stored int, defaulting ONLY on a genuinely absent value (None/blank) — never on
        a real zero. Without this, a known span offset of ``0`` would be misread as absent."""
        if value is None or value == "":
            return default
        return int(value)

    def _hydrate(self, row: dict[str, Any]) -> TypedEventAssertion:
        """Reconstruct a ``TypedEventAssertion`` from a read row. Prefers the raw source string
        (``*_raw``), falling back to ``toString()`` of the parsed canonical temporal only when the
        raw is absent — so an unparseable-but-nonblank stamp is preserved exactly for audit."""
        raw_valid = str(row.get("valid_at_raw") or "")
        if not raw_valid:
            raw_valid = str(row.get("valid_at") or "")
        raw_learned = str(row.get("learned_at_raw") or "")
        if not raw_learned:
            raw_learned = str(row.get("learned_at") or "")
        metadata = row.get("metadata")
        try:
            metadata = json.loads(metadata) if isinstance(metadata, str) else dict(metadata or {})
        except (TypeError, ValueError):
            metadata = {}
        return TypedEventAssertion(
            subject_uuid=str(row.get("subject_uuid") or ""),
            subject_display=str(row.get("subject_display") or ""),
            predicate=str(row.get("predicate") or ""),
            object_key=str(row.get("object_key") or ""),
            object_display=str(row.get("object_display") or ""),
            valid_at=raw_valid,
            learned_at=raw_learned,
            stated_span=str(row.get("stated_span") or ""),
            episode_uuid=str(row.get("episode_uuid") or ""),
            object_uuid=row.get("object_uuid"),
            domain=row.get("domain"),
            namespace=row.get("namespace"),
            turn_evidence_uuid=row.get("turn_evidence_uuid"),
            span_start=self._int_or(row.get("span_start"), -1),
            span_end=self._int_or(row.get("span_end"), -1),
            claim_ordinal=self._int_or(row.get("claim_ordinal"), 0),
            time_basis=str(row.get("time_basis") or "explicit"),
            evidence_tier=str(row.get("evidence_tier") or "agent"),
            perceiver_version=str(row.get("perceiver_version") or "v1"),
            metadata=metadata,
        )

    @staticmethod
    def _sort_by_valid(events: Iterable[TypedEventAssertion]) -> list[TypedEventAssertion]:
        """Deterministic lane order: parseable valid world times ascending first, then assertion_key;
        unparseable valid times LAST, always. ``learned_at`` and input order never participate."""

        def key(event: TypedEventAssertion) -> tuple[tuple[int, datetime], str]:
            dt = parse_iso8601(event.valid_at)
            return ((0, dt) if dt is not None else (1, _FAR_FUTURE)), event.assertion_key

        return sorted(events, key=key)
