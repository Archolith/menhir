"""Menhir policy on the fork's ``EdgeInvalidationHook`` seam (#168 P1).

Safety property: information whose source is not human-approved (``GATE_APPROVED_HUMAN_SOURCES``)
never ends a stored edge whose first-writer source is human-approved. The authority is the
CAUSE edge's source, not the episode's: a stored human fact may supersede another human fact
even when an agent episode is what triggers the re-check. Every other pair keeps upstream
behaviour (INVALIDATE).

Durable facts the decision rests on:
* cause source: the cause edge's stored ``r.source`` when the graph has it; otherwise the cause
  was extracted by this call and carries this ``add_episode`` call's ``source_description``,
  bound per call in ``GraphitiClient.add_episode`` (``invalidation_guard_state``). Tasks copy
  the context when created, so concurrent calls cannot see each other's state.
* target source: ``r.source`` read from Neo4j by edge uuid, never the in-memory copy (a resolved
  duplicate has its attributes recomputed). ``GUARD_PRESERVED_EDGE_ATTRIBUTES`` keeps that stored
  value through the duplicate's full-replace save, so a paraphrase cannot reset the first writer.
* an edge with no stored source (new in this call, or not stamped yet) is not human-protected.

Any error answers KEEP (fail closed): a stored human edge is never ended on a failed lookup.

Statement order (2a, ``statement_order``; always on since the #168 freeze, kept as a seam): after the
authority check, on NEWER_CANDIDATE and CONTRADICTION, a cause said before its target answers KEEP,
so an older statement whose event date is later never ends a newer one; both stay live. It only
adds KEEP answers. "Said" is the episode time, read per edge (see ``_cause_said_earlier``). Each
such pair is recorded as an ``EdgeInvalidationConflict`` with ``kept_by = 'statement_order'``.

Known gap (fork): the OWN_END branch expires an edge whose own window is inverted or empty
without asking this hook.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from menhir.domain.namespace import tenant_scope_cypher, tenant_scope_params
from menhir.domain.self_identity import GATE_APPROVED_HUMAN_SOURCES

logger = logging.getLogger(__name__)

_SOURCE_LOOKUP_TIMEOUT_S = 10.0
_RECORD_TIMEOUT_S = 10.0

_STORED_EDGE_SOURCE_CYPHER = (
    "MATCH ()-[r:RELATES_TO {uuid: $uuid}]->() RETURN r.source AS source LIMIT 1"
)
_STORED_EDGE_SAID_CYPHER = (
    f"MATCH ()-[r:RELATES_TO {{uuid: $uuid}}]->() WHERE {tenant_scope_cypher('r')} "
    "RETURN r.reference_time AS said LIMIT 1"
)
#: Reasons the statement-order veto (2a) answers; OWN_END keeps today's answer.
_STATEMENT_ORDER_REASONS = frozenset({"newer_candidate", "contradiction"})

#: Standalone record per (kept edge, cause edge). Not an :Entity, so recall, merge eligibility
#: and the entity conflict groups are untouched. A repeat sighting never reopens a record an
#: operator has resolved; it only bumps ``last_seen_at``/``seen_count``.
KEPT_CONFLICT_CYPHER = """
UNWIND $rows AS row
MERGE (c:EdgeInvalidationConflict {uuid: row.uuid})
ON CREATE SET c.kept_edge_uuid = row.kept_edge_uuid, c.cause_edge_uuid = row.cause_edge_uuid,
    c.group_id = row.group_id, c.reason = row.reason, c.cause_source = row.cause_source,
    c.kept_fact = row.kept_fact, c.cause_fact = row.cause_fact, c.kept_by = row.kept_by,
    c.status = 'unresolved', c.created_at = datetime()
SET c.last_seen_at = datetime(), c.seen_count = coalesce(c.seen_count, 0) + 1
RETURN count(c) AS recorded
"""


def is_human_source(source: object) -> bool:
    return str(source or "").strip().lower() in GATE_APPROVED_HUMAN_SOURCES


@dataclass(frozen=True)
class KeptEdge:
    """An edge the hook kept against a cause.

    ``kept_by``: ``authority`` (#377, a stored human edge against a non-human contradiction) or
    ``statement_order`` (2a, a cause said before the edge). A record without ``kept_by`` is an
    authority keep, as are records written before 2a.
    """

    reason: str
    kept_edge_uuid: str
    kept_group_id: str
    kept_fact: str
    cause_edge_uuid: str
    cause_fact: str
    cause_source: str
    kept_by: str = "authority"


@dataclass
class InvalidationGuardState:
    """Per-call state for one ``GraphitiClient.add_episode`` invocation."""

    owner: str
    source: str
    kept: list[KeptEdge] = field(default_factory=list)
    stored_sources: dict[str, str | None] = field(default_factory=dict)
    stored_said: dict[tuple[str, str], datetime | None] = field(default_factory=dict)


invalidation_guard_state: ContextVar[InvalidationGuardState | None] = ContextVar(
    "menhir_invalidation_guard_state", default=None
)


class MenhirEdgeInvalidationHook:
    """Menhir adapter on the fork's ``EdgeInvalidationHook`` seam."""

    def __init__(self, driver: Any, *, statement_order: bool = False) -> None:
        self._driver = driver
        self._statement_order = statement_order

    async def decide_edge_invalidation(self, context: Any) -> Any:
        from graphiti_core.edge_invalidation import EdgeInvalidationDecision

        try:
            state = invalidation_guard_state.get()
            if await self._authority_keeps(state, context):
                return EdgeInvalidationDecision.KEEP
            if self._statement_order and await self._cause_said_earlier(state, context):
                return EdgeInvalidationDecision.KEEP
            return EdgeInvalidationDecision.INVALIDATE
        except Exception:
            logger.exception("Edge-invalidation hook failed; keeping the edge (fail closed)")
            return EdgeInvalidationDecision.KEEP

    async def _authority_keeps(self, state: InvalidationGuardState | None, context: Any) -> bool:
        """#377: a non-human cause never ends a stored human edge."""
        if context.target_is_new:
            # Created by this very call, so it carries this call's source.
            return False
        cause_source = await self._cause_source(state, context.cause_edge)
        if is_human_source(cause_source):
            return False
        target_source = await self._stored_source(state, str(context.target_edge.uuid))
        if not is_human_source(target_source):
            return False
        reason = _reason(context)
        logger.info(
            "Edge-invalidation guard kept human edge=%s reason=%s cause_source=%s",
            context.target_edge.uuid, reason, cause_source,
        )
        if state is not None and reason == "contradiction":
            state.kept.append(_kept_edge(context, reason, cause_source, "authority"))
        return True

    async def _cause_said_earlier(
        self, state: InvalidationGuardState | None, context: Any
    ) -> bool:
        """2a: a statement said before the target's never ends it (veto only).

        "Said" is the episode time: this call's episode for its own mention (the target when
        ``target_is_new``, the cause on CONTRADICTION), the stored ``r.reference_time`` for any
        other edge. A missing or unparseable time keeps today's answer.
        """
        reason = _reason(context)
        if reason not in _STATEMENT_ORDER_REASONS:
            return False
        episode_said = _as_utc(getattr(context.episode, "valid_at", None))
        if context.target_is_new:
            target_said = episode_said
        else:
            target_said = await self._stored_said(state, context.target_edge)
        if target_said is None:
            return False
        if reason == "contradiction":
            # The fork's CONTRADICTION cause is always this call's extracted mention.
            cause_said = episode_said
        else:
            cause_said = await self._stored_said(state, context.cause_edge)
        if cause_said is None or cause_said >= target_said:
            return False
        logger.info(
            "Edge-invalidation statement order kept edge=%s reason=%s target_said=%s "
            "cause=%s cause_said=%s",
            context.target_edge.uuid, reason, target_said.isoformat(),
            context.cause_edge.uuid, cause_said.isoformat(),
        )
        if state is not None:
            # Both reasons are recorded: the inversion this veto exists for is NEWER_CANDIDATE.
            cause_source = await self._cause_source(state, context.cause_edge)
            state.kept.append(_kept_edge(context, reason, cause_source, "statement_order"))
        return True

    async def _stored_said(
        self, state: InvalidationGuardState | None, edge: Any
    ) -> datetime | None:
        edge_uuid = str(edge.uuid)
        group_id = str(getattr(edge, "group_id", "") or "")
        key = (group_id, edge_uuid)
        if state is not None and key in state.stored_said:
            return state.stored_said[key]
        # A str group id never yields None params, so the tenancy fragment always filters.
        records = await self._lookup(
            _STORED_EDGE_SAID_CYPHER, uuid=edge_uuid, **tenant_scope_params(group_id)
        )
        said = _as_utc(records[0]["said"]) if records else None
        if state is not None:
            state.stored_said[key] = said
        return said

    async def _lookup(self, cypher: str, **params: Any) -> Any:
        """One stored-edge read (Neo4j only, no OpenAI), bounded by the lookup timeout."""
        result = await asyncio.wait_for(
            self._driver.execute_query(cypher, **params), timeout=_SOURCE_LOOKUP_TIMEOUT_S
        )
        return _records(result)

    async def _cause_source(self, state: InvalidationGuardState | None, cause: Any) -> str | None:
        """Stored source of a cause edge already in the graph, else this call's source.

        A cause the graph does not know yet is an edge extracted by this call.
        """
        stored = await self._stored_source(state, str(cause.uuid))
        if stored is not None:
            return stored
        return state.source if state is not None else None

    async def _stored_source(
        self, state: InvalidationGuardState | None, edge_uuid: str
    ) -> str | None:
        if state is not None and edge_uuid in state.stored_sources:
            return state.stored_sources[edge_uuid]
        records = await self._lookup(_STORED_EDGE_SOURCE_CYPHER, uuid=edge_uuid)
        value = records[0]["source"] if records else None
        source = str(value) if value else None
        if state is not None:
            state.stored_sources[edge_uuid] = source
        return source


def _kept_edge(context: Any, reason: str, cause_source: str | None, kept_by: str) -> KeptEdge:
    target, cause = context.target_edge, context.cause_edge
    return KeptEdge(
        reason=reason,
        kept_edge_uuid=str(target.uuid),
        kept_group_id=str(getattr(target, "group_id", "") or ""),
        kept_fact=str(getattr(target, "fact", "") or ""),
        cause_edge_uuid=str(cause.uuid),
        cause_fact=str(getattr(cause, "fact", "") or ""),
        cause_source=str(cause_source or ""),
        kept_by=kept_by,
    )


def _reason(context: Any) -> str:
    return str(getattr(context.reason, "value", context.reason))


def _records(result: Any) -> Any:
    records = getattr(result, "records", None)
    if records is None and isinstance(result, tuple):
        records = result[0]
    return records


def _as_utc(value: object) -> datetime | None:
    """A timezone-aware time, or None when missing or unparseable (naive is read as UTC)."""
    try:
        if value is None:
            return None
        to_native = getattr(value, "to_native", None)
        if callable(to_native):
            value = to_native()
        if isinstance(value, str):
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if not isinstance(value, datetime):
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, AttributeError):
        return None


def kept_conflict_rows(kept: list[KeptEdge]) -> list[dict[str, str]]:
    """One row per distinct (kept edge, cause edge); the uuid is derived, so writes are idempotent."""
    rows: dict[str, dict[str, str]] = {}
    for item in kept:
        uid = str(uuid5(NAMESPACE_URL, f"menhir:edge-conflict:{item.kept_edge_uuid}:{item.cause_edge_uuid}"))
        row = {
            "uuid": uid,
            "kept_edge_uuid": item.kept_edge_uuid,
            "cause_edge_uuid": item.cause_edge_uuid,
            "group_id": item.kept_group_id,
            "reason": item.reason,
            "cause_source": item.cause_source,
            "kept_fact": item.kept_fact,
            "cause_fact": item.cause_fact,
        }
        if item.kept_by != "authority":
            # Absent means authority, so pre-2a records and rows read the same way.
            row["kept_by"] = item.kept_by
        rows.setdefault(uid, row)
    return list(rows.values())


async def record_kept_conflicts(driver: Any, kept: list[KeptEdge]) -> int:
    """Write unresolved conflict records for ``kept``; returns the number of rows sent."""
    rows = kept_conflict_rows(kept)
    if not rows:
        return 0
    await asyncio.wait_for(
        driver.execute_query(KEPT_CONFLICT_CYPHER, rows=rows), timeout=_RECORD_TIMEOUT_S
    )
    return len(rows)


#: Edge properties the guard reads that must survive duplicate resolution. A paraphrased
#: restatement clears a stored edge's attributes and the save is a full replace, so without this
#: ``r.source`` is dropped and re-stamped with the restating call's source (first writer lost).
GUARD_PRESERVED_EDGE_ATTRIBUTES = ("source",)


def guarded_preserved_edge_attributes_kwargs(base: dict[str, Any]) -> dict[str, Any]:
    """``base`` preserved-attributes kwargs plus the guard's names (always non-empty)."""
    names = tuple(base.get("preserved_edge_attributes", ()))
    extra = tuple(n for n in GUARD_PRESERVED_EDGE_ATTRIBUTES if n not in names)
    return {"preserved_edge_attributes": names + extra}


def invalidation_hook_kwargs(
    graphiti_cls: Any, driver: Any, *, statement_order: bool = True
) -> dict[str, Any]:
    """``Graphiti(...)`` kwargs for the invalidation guard; refuses a fork without the seam."""
    try:
        params = inspect.signature(graphiti_cls.__init__).parameters
    except (TypeError, ValueError):
        params = {}
    # A **kwargs signature is a test double; the installed fork's explicit parameter is pinned by
    # tests/test_edge_invalidation_guard.py::test_installed_graphiti_has_the_explicit_seam.
    accepts_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    if "edge_invalidation_hook" not in params and not accepts_kwargs:
        raise RuntimeError(
            "Menhir requires archolith-graphiti-core==0.31.2.post1 or later "
            "(Graphiti(edge_invalidation_hook=...)); the installed graphiti_core has no "
            "edge_invalidation_hook, so an agent episode could expire a user-stated edge. "
            "Reinstall Menhir's pinned dependencies."
        )
    return {
        "edge_invalidation_hook": MenhirEdgeInvalidationHook(
            driver, statement_order=statement_order
        )
    }
