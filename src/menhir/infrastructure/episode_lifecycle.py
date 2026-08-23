"""Episode lifecycle state machine helpers."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from typing import Any

from menhir.domain.utils import source_confidence_for
from menhir.infrastructure.cypher import (
    Cypher,
    EPISODE_PROCESSING_FIELDS,
    EPISODE_RETRY_FIELDS,
    MEMORY_RETURN_FIELDS,
    non_derived_view_cypher,
)

logger = logging.getLogger(__name__)

#: Canonical name for the per-namespace self :Entity (matches the extraction prompt's "user" subject
#: convention, so a materialized first-person View reads naturally).
_SELF_ENTITY_NAME = "user"

_PENDING_EPISODE_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]{3,}")

#: Context-window errors an OPERATOR can clear without the payload changing -- local
#: llama.cpp/server phrasing, where the server was started with too small an n_ctx. Reloading
#: the model with a larger window makes the SAME episode succeed, which is why these alone earn
#: the extended retry budget (`get_context_window_retry_attempts`).
_RECOVERABLE_CONTEXT_WINDOW_MARKERS = (
    "cannot truncate prompt",
    "n_keep",
    "n_ctx",
    "available context window",
    "exceeds the available context window",
    "exceeds the available context",
)

#: A hosted provider refusing the request against a fixed model limit. Also a context-window
#: error, but NOT operator-clearable: the ceiling is not ours to raise, and re-sending the same
#: payload fails identically. These must classify as terminal WITHOUT earning extra retries.
_PROVIDER_CONTEXT_LIMIT_MARKERS = (
    "maximum context length",
    "context_length_exceeded",
    "reduce the length of the messages",
)


def is_recoverable_context_window_error(error: object | None) -> bool:
    """Return True for a context-window error a config change could clear.

    Gates the EXTENDED retry budget. Deliberately narrower than
    `is_context_window_error_text`: retrying an unchanged payload against a hosted
    provider's fixed limit burns attempts and cannot ever succeed.
    """
    if error is None:
        return False
    error_text = str(error).lower()
    return any(marker in error_text for marker in _RECOVERABLE_CONTEXT_WINDOW_MARKERS)


def is_context_window_error_text(error: object | None) -> bool:
    """Return True when the error text indicates a context-window mismatch, from any source.

    Covers both local server misconfiguration and a hosted provider's hard limit. Used for
    CLASSIFICATION (these are terminal). For retry-budget decisions use
    `is_recoverable_context_window_error` instead -- only the local variant is retryable.

    The provider markers were missing until 2026-07-28, so an OpenAI 400
    `context_length_exceeded` fell through every list to the `manual_review` default. That
    still parked the episode correctly, so no retry behaviour depended on the gap; the
    classification was simply less accurate than it looked.
    """
    if error is None:
        return False
    error_text = str(error).lower()
    return is_recoverable_context_window_error(error) or any(
        marker in error_text for marker in _PROVIDER_CONTEXT_LIMIT_MARKERS
    )


class EpisodeLifecycleRepository:
    """Episode creation, claiming, and state transitions."""

    neo4j: Any

    def create_pending_episode(
        self,
        *,
        episode_uuid: str,
        name: str,
        content: str,
        session_id: str,
        user_id: str,
        source: str,
        source_confidence: float,
        diff: str | None = None,
        user_flagged: bool = False,
        bootstrap_scope: str | None = None,
        namespace: str = "default",
        reference_time: datetime | None = None,
    ) -> str:
        self.neo4j.execute(
            """
            // CF-158 criterion 1, sibling label. `:Episodic.uuid` has no uniqueness constraint
            // either -- the duplicate census in that finding counted BOTH labels -- so this
            // carries the identical hazard as the TEMPORAL `:Entity` writes. Fixing one and
            // leaving this one is trap T26 exactly: the instance gets fixed, the class does not.
            MERGE (n:Episodic {uuid: $episode_uuid})
            ON CREATE SET
                n.name = $name,
                n.type = 'EPISODIC',
                n.scope = 'SESSION',
                n.content = $content,
                n.diff = $diff,
                n.source = $source,
                n.source_confidence = $source_confidence,
                n.user_flagged = $user_flagged,
                n.bootstrap_scope = $bootstrap_scope,
                n.session_id = $session_id,
                n.user_id = $user_id,
                n.namespace = $namespace,
                n.created_at = datetime(),
                n.last_accessed = datetime(),
                n.sharpness = 0.0,
                n.edge_count = 0,
                n.processing_state = 'PENDING',
                n.processing_stage = 'queued',
                n.processing_progress = 0.0,
                n.processing_steps_total = 5,
                n.processing_steps_completed = 0,
                n.processing_llm_tasks_attempt = 0,
                n.processing_llm_tasks_total = 0,
                n.processing_llm_last_task_at = null,
                n.processing_attempts = 0,
                n.queued_at = datetime(),
                n.reference_time = $reference_time,
                n.processing_owner = null,
                n.processing_lease_expires_at = null,
                n.processing_heartbeat_at = datetime(),
                n.processing_error = null,
                n.resolved_episode_uuid = null,
                n.enrichment_priority = 'P1',
                n.enriched_nodes_touched = 0,
                n.enriched_edges_touched = 0
            """,
            params={
                "episode_uuid": episode_uuid,
                "name": name,
                "content": content,
                "diff": diff,
                "session_id": session_id,
                "user_id": user_id,
                "source": source,
                "source_confidence": source_confidence,
                "user_flagged": user_flagged,
                "bootstrap_scope": bootstrap_scope,
                "namespace": namespace,
                "reference_time": reference_time,
            },
        )
        return episode_uuid

    def create_evidence_projection(
        self, *, turn_evidence_uuid: str, projection_uuid: str, name: str,
        session_id: str, user_id: str, namespace: str,
    ) -> str | None:
        """Mint a NON-RECALLABLE `:Episodic` carrying a captured turn's VERBATIM text, so entities
        exist in the user's own vocabulary. Returns the projection uuid, or None when nothing was
        created (turn absent, not user-declared, or already projected).

        WHY THIS EXISTS. A typed scalar assertion is extracted from the USER's words while the
        entities it must bind to come from the AGENT's memory text -- a paraphrase. The two
        vocabularies need not agree, so the bind fails. `:TurnEvidence` holds the user's actual words
        but is deliberately never enriched (ADR 0001), so it yields no entities to bind to. This
        projection is the missing step: the turn's text, enriched like any episode, kept out of recall.

        NOT A PROMOTION TO MEMORY. ADR 0001 rejected mixing raw turns into `:Episodic` memory
        (Option A) because raw chat would pollute recall and decay. This carries
        `is_evidence_projection: true` so lifecycle and listing paths can exclude it; semantic recall
        needs no change, because recall candidates are `:Entity`, not `:Episodic`.

        FAIL-CLOSED ON DECLARANT. Projects ONLY a turn that is BOTH `role='user'` and
        `declarant='user'`. The declarant boundary is load-bearing (ADR 0001): an assistant restating
        a user's fact must never become evidence for it. Anything else projects nothing.

        THE TEXT is not caller-chosen: `content` is copied from `t.text` inside this query, so a
        caller cannot choose the words that land in a node stamped `source='user'`. That is the
        specific guarantee, and it is worth having.

        IT IS NOT PROOF A HUMAN SPOKE. `/api/turn-evidence` accepts `role` and `declarant` FROM THE
        CALLER (`api/routes_support.py:256`), so anyone holding the agent key can post a turn
        claiming `declarant='user'` and have it projected here. The chain is producer-trust end to
        end; the governance ledger's "convention-sound, not adversarially-sound" verdict still
        applies. An earlier version of this docstring claimed the link was unforgeable -- retracted,
        see the CORRECTION in `.agent/plans/menhir-evidence-projection-episodes.md`.

        IDEMPOTENT on the turn: `MERGE` keys on `evidence_projection_of`, so N memories citing one
        turn yield ONE projection, not N.

        THE PROJECTION INHERITS THE TURN'S NAMESPACE, and `namespace` is a FILTER on which turns
        this caller may reach -- two separate things that were previously one.

        It used to stamp the CALLER's namespace on a turn matched globally. That is a content
        EXFILTRATION, not merely a mis-scoped link: it copies another tenant's verbatim user text
        into a node in the caller's own silo, where their recall then enriches and surfaces it.
        Worse than the admission link it accompanies, because it moves content across the
        boundary rather than drawing an edge across it.

        Stamping from `t.namespace` is what makes the fix correct rather than merely restrictive.
        A projection is a COPY OF THE TURN, so its home is wherever the turn lives; deriving it
        from the caller meant an unpinned caller projecting a turn in namespace X produced a
        projection in the default silo -- the content separated from its own tenant, a latent
        mis-filing that predates the tenancy work and that a caller-derived value could never fix.

        `namespace=None` does not filter, so an unpinned deployment reaches every turn exactly as
        before and each projection now lands in the right silo.
        """
        rows = self.neo4j.execute(
            """
            MATCH (t:TurnEvidence {turn_id: $turn_evidence_uuid})
            WHERE t.role = 'user' AND t.declarant = 'user'
              AND t.text IS NOT NULL AND trim(t.text) <> ''
              AND ($namespace IS NULL OR coalesce(t.namespace, 'default') = $namespace)
            MERGE (p:Episodic {evidence_projection_of: t.turn_id})
            ON CREATE SET
                p.uuid = $projection_uuid,
                p.name = $name,
                p.type = 'EPISODIC',
                p.scope = 'SESSION',
                p.content = t.text,
                p.is_evidence_projection = true,
                p.source = 'user',
                p.source_confidence = $source_confidence,
                p.diff = null,
                p.user_flagged = false,
                p.bootstrap_scope = null,
                p.session_id = $session_id,
                p.user_id = $user_id,
                p.namespace = coalesce(t.namespace, 'default'),
                p.created_at = datetime(),
                p.last_accessed = datetime(),
                p.sharpness = 0.0,
                p.edge_count = 0,
                p.processing_state = 'PENDING',
                p.processing_stage = 'queued',
                p.processing_progress = 0.0,
                p.processing_steps_total = 5,
                p.processing_steps_completed = 0,
                p.processing_llm_tasks_attempt = 0,
                p.processing_llm_tasks_total = 0,
                p.processing_llm_last_task_at = null,
                p.processing_attempts = 0,
                p.queued_at = datetime(),
                p.reference_time = coalesce(t.occurred_at, t.recorded_at),
                p.processing_owner = null,
                p.processing_lease_expires_at = null,
                p.processing_heartbeat_at = datetime(),
                p.processing_error = null,
                p.resolved_episode_uuid = null,
                p.enrichment_priority = 'P1',
                p.enriched_nodes_touched = 0,
                p.enriched_edges_touched = 0
            MERGE (p)-[:ADMITTED_ON]->(t)
            // `created` WITHOUT a flag property: our uuid is written only ON CREATE, so it comes back
            // only when this call is the one that made the node. A stored `created = true` would
            // survive on the node and report true forever, defeating the idempotency check.
            RETURN p.uuid AS uuid, (p.uuid = $projection_uuid) AS created
            """,
            params={
                "turn_evidence_uuid": turn_evidence_uuid,
                "projection_uuid": projection_uuid,
                "name": name,
                "session_id": session_id,
                "user_id": user_id,
                # A FILTER on which turns this caller may reach -- NOT the value stamped on the
                # projection, which is inherited from the turn itself. None means "do not
                # filter" and keeps an unpinned caller's reach exactly as it was.
                "namespace": (str(namespace).strip() or None) if namespace else None,
                # From the SSOT, never a literal. `source_confidence_for`'s own docstring records
                # that hardcoding this value here was drift (SSOT-09), and the tier ladder is the
                # kind of thing that gets tuned -- a literal would keep passing tests while silently
                # disagreeing with every other writer.
                "source_confidence": source_confidence_for("user"),
            },
        )
        if not rows:
            return None
        row = rows[0]
        if not row.get("created"):
            return None  # already projected on an earlier memory citing the same turn
        return str(row["uuid"])

    def link_episode_admission(
        self, *, episode_uuid: str, turn_evidence_uuid: str, namespace: str | None = None
    ) -> bool:
        """Record WHICH `:TurnEvidence` an apex-tier memory was admitted on:
        `(:Episodic)-[:ADMITTED_ON]->(:TurnEvidence)`.

        The admission gate already resolves this pair to decide the claim's tier, then drops it, so the
        same user turn lived in the graph twice under two identities with nothing joining them: a
        scalar View could prove a human said something (`:TurnEvidence-[:FOUNDS]->(:TypedAssertion)`)
        but could not reach the memory built from that turn, and recall could return the memory without
        knowing a View existed about it. This edge closes that traversal.

        It does NOT merge the two stores (ADR 0001): `:TurnEvidence` stays raw evidence, out of normal
        recall, and stays independently writable — the hook captures triaged prompts whether or not
        anyone calls `add_memory`, so most evidence has no memory and the edge is legitimately absent.

        MATCH-only on both sides: never MERGE a node, so a bad uuid draws nothing rather than creating a
        stub that would later read as evidence that does not exist. Idempotent on the relationship, so a
        replayed ingest does not fan out duplicates. Returns True when the edge exists after the call.

        ``namespace`` scopes BOTH endpoints and is opt-in (``None`` does not filter). Both ids come
        from the caller, and matching them globally meant a caller could join ANY episode to ANY
        turn -- including two nodes neither of which was its own, drawing a permanent provenance
        edge inside another tenant's graph. The predicate is on both MATCHes rather than one,
        because scoping only the episode would still let a caller's own memory be joined to
        someone else's captured turn, which is the direction that leaks.
        """
        ns = str(namespace).strip() if namespace is not None else ""
        rows = self.neo4j.execute(
            """
            MATCH (e:Episodic {uuid: $episode_uuid})
            WHERE $namespace IS NULL OR coalesce(e.namespace, 'default') = $namespace
            MATCH (t:TurnEvidence {turn_id: $turn_evidence_uuid})
            WHERE $namespace IS NULL OR coalesce(t.namespace, 'default') = $namespace
            MERGE (e)-[:ADMITTED_ON]->(t)
            RETURN count(*) AS linked
            """,
            params={
                "episode_uuid": episode_uuid,
                "turn_evidence_uuid": turn_evidence_uuid,
                "namespace": ns or None,
            },
        )
        return bool(rows and int(rows[0].get("linked") or 0) > 0)

    def list_pending_episode_uuids(self, *, max_attempts: int, limit: int = 100) -> list[str]:
        safe_limit = max(1, min(limit, 500))
        query = (
            Cypher()
            .match("(n:Episodic)")
            .where(
                "n.processing_state = 'PENDING'",
                "coalesce(toInteger(n.processing_attempts), 0) < $max_attempts",
                "coalesce(properties(n)['retry_after'], datetime()) <= datetime()",
            )
            .return_raw("n.uuid AS uuid")
            .order_by("coalesce(n.queued_at, n.created_at) ASC, n.uuid")
            .limit()
            .build()
        )
        rows = self.neo4j.execute(query, params={"limit": safe_limit, "max_attempts": max(1, max_attempts)})
        return [str(row["uuid"]) for row in rows if row.get("uuid")]

    def fetch_failed_episode_retry_candidates(self, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 500))
        query = (
            Cypher()
            .match("(n:Episodic)")
            .where("n.processing_state = 'FAILED'")
            .return_fields(EPISODE_RETRY_FIELDS)
            .order_by("coalesce(n.processing_completed_at, n.processing_started_at, n.queued_at, n.created_at) ASC")
            .limit()
            .build()
        )
        return self.neo4j.execute(query, params={"limit": safe_limit})

    def fetch_failed_error_signatures(self, limit: int = 25) -> list[dict[str, Any]]:
        """Group FAILED episodes by their error text, most common first.

        Feeds the queue-health job's stuck-backlog warning. Grouping first keeps the
        classification cost proportional to the number of DISTINCT causes (small) rather
        than to the size of the backlog, which can be hundreds of episodes.

        Classification itself deliberately stays in Python (`classify_enrichment_failure`)
        rather than being reimplemented in Cypher, so the marker lists cannot drift apart.
        """
        safe_limit = max(1, min(limit, 200))
        return self.neo4j.execute(
            """
            MATCH (e:Episodic) WHERE e.processing_state = 'FAILED'
            WITH coalesce(e.processing_error, '(no error recorded)') AS error,
                 count(*) AS count,
                 min(coalesce(e.processing_completed_at, e.created_at)) AS oldest
            RETURN error, count, toString(oldest) AS oldest_at
            ORDER BY count DESC, error ASC
            LIMIT $limit
            """,
            params={"limit": safe_limit},
        )

    def find_completed_episode_artifact(
        self,
        *,
        anchor_uuid: str,
        anchor_name: str,
    ) -> dict[str, Any] | None:
        rows = self.neo4j.execute(
            (
                Cypher()
                .match("(e:Episodic)-[]-(n:Entity)")
                .where("e.uuid <> $anchor_uuid", "e.name = $anchor_name")
                .return_raw("e.uuid AS resolved_episode_uuid, collect(DISTINCT n.uuid) AS entity_uuids")
                .limit("2")
                .build()
            ),
            params={"anchor_uuid": anchor_uuid, "anchor_name": anchor_name},
        )
        if len(rows) > 1:
            logger.warning(
                "Multiple completed episode artifacts found for anchor_name=%s anchor_uuid=%s - skipping reconciliation",
                anchor_name,
                anchor_uuid,
            )
        if len(rows) != 1:
            return None

        resolved_episode_uuid = str(rows[0].get("resolved_episode_uuid") or "")
        entity_uuids = [str(uuid) for uuid in (rows[0].get("entity_uuids") or []) if str(uuid)]
        if not resolved_episode_uuid or not entity_uuids:
            return None

        edge_uuids: list[str] = []
        episode_edge_rows = self.neo4j.execute(
            (
                Cypher()
                .match("(:Episodic {uuid: $episode_uuid})-[r]-()")
                .where("r.uuid IS NOT NULL")
                .return_raw("collect(DISTINCT r.uuid) AS edge_uuids")
                .build()
            ),
            params={"episode_uuid": resolved_episode_uuid},
        )
        if episode_edge_rows:
            edge_uuids.extend(str(uuid) for uuid in (episode_edge_rows[0].get("edge_uuids") or []) if str(uuid))

        entity_edge_rows = self.neo4j.execute(
            (
                Cypher()
                .match("(a:Entity)-[r]-(b:Entity)")
                .where("a.uuid IN $entity_uuids", "b.uuid IN $entity_uuids", "r.uuid IS NOT NULL")
                .return_raw("collect(DISTINCT r.uuid) AS edge_uuids")
                .build()
            ),
            params={"entity_uuids": entity_uuids},
        )
        if entity_edge_rows:
            edge_uuids.extend(str(uuid) for uuid in (entity_edge_rows[0].get("edge_uuids") or []) if str(uuid))

        return {
            "resolved_episode_uuid": resolved_episode_uuid,
            "entity_uuids": entity_uuids,
            "edge_uuids": list(dict.fromkeys(edge_uuids)),
        }

    def claim_pending_episode(
        self,
        episode_uuid: str,
        *,
        max_attempts: int,
        context_retry_attempts: int | None = None,
        worker_id: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None:
        precheck_query = (
            Cypher()
            .match("(n:Episodic)")
            .where("n.uuid = $episode_uuid")
            .return_fields(
                (),
                "n.uuid AS uuid",
                "n.processing_state AS processing_state",
                "coalesce(toInteger(n.processing_attempts), 0) AS processing_attempts",
                "n.processing_error AS processing_error",
            )
            .build()
        )
        precheck_rows = self.neo4j.execute(precheck_query, params={"episode_uuid": episode_uuid})
        context_cap = max(max_attempts, int(context_retry_attempts if context_retry_attempts is not None else max_attempts))
        claim_query = (
            Cypher()
            .match("(n:Episodic)")
            .where(
                "n.uuid = $episode_uuid",
                "n.processing_state IN ['PENDING', 'FAILED']",
                "coalesce(properties(n)['retry_after'], datetime()) <= datetime()",
                "(coalesce(toInteger(n.processing_attempts), 0) < $max_attempts"
                " OR (n.processing_state = 'FAILED'"
                " AND coalesce(toInteger(n.processing_attempts), 0) < $context_retry_attempts"
                " AND n.processing_error IS NOT NULL"
                " AND ANY(marker IN $context_error_markers"
                " WHERE toLower(n.processing_error) CONTAINS marker)))",
            )
            .set(
                (
                    "n.processing_state = 'ENRICHING'",
                    "n.processing_stage = 'claiming'",
                    "n.processing_substage = 'lease_acquired'",
                    "n.processing_substage_started_at = datetime()",
                    "n.processing_progress = 5.0",
                    "n.processing_steps_total = coalesce(toInteger(n.processing_steps_total), 5)",
                    "n.processing_steps_completed = 0",
                    "n.processing_llm_tasks_attempt = 0",
                    "n.processing_llm_last_task_at = null",
                    "n.processing_llm_active_task = null",
                    "n.processing_llm_active_kind = null",
                    "n.processing_llm_active_model = null",
                    "n.processing_llm_active_endpoint = null",
                    "n.processing_started_at = datetime()",
                    "n.processing_completed_at = null",
                    "n.processing_attempts = coalesce(toInteger(n.processing_attempts), 0) + 1",
                    "n.processing_owner = $worker_id",
                    "n.processing_lease_expires_at = datetime() + duration({seconds: $lease_seconds})",
                    "n.processing_heartbeat_at = datetime()",
                    "n.processing_error = null",
                    "n.retry_after = null",
                )
            )
            .return_fields(
                (),
                "n.uuid AS uuid",
                "n.name AS name",
                "n.content AS content",
                "n.source AS source",
                "n.session_id AS session_id",
                "n.user_id AS user_id",
                "n.user_flagged AS user_flagged",
                "n.bootstrap_scope AS bootstrap_scope",
                # Carry the stored namespace so the enrichment worker writes the
                # extracted entity/edge nodes into the episode's graphiti group_id.
                # Without this the claim returns no namespace, enrichment defaults to
                # the "default" group (group_id ""), and namespace-scoped recall finds
                # nothing (candidates_evaluated=0) -- breaking namespace isolation on
                # the async ingest path.
                "n.namespace AS namespace",
                # Carry the caller-supplied world time (`occurred_at` -> `reference_time`).
                # Without it the claim returns no reference_time, and the enrichment handoff
                # `ctx.claimed.get("reference_time") or ctx.claimed.get("queued_at")` silently
                # falls back to the QUEUE time -- so every backdated episode is stamped with its
                # ingestion time in graphiti, and supersession then orders history by when it was
                # imported rather than when it happened. Same failure shape as the namespace bug
                # noted above: stored correctly, projected nowhere.
                # Carry the caller-supplied world time (`occurred_at` -> `reference_time`).
                # Without it the claim returns no reference_time, and the enrichment handoff
                # `ctx.claimed.get("reference_time") or ctx.claimed.get("queued_at")` silently
                # falls back to the QUEUE time -- so every backdated episode is stamped with its
                # ingestion time in graphiti, and supersession then orders history by when it was
                # imported rather than when it happened. Same failure shape as the namespace bug
                # noted above: stored correctly, projected nowhere.
                "n.reference_time AS reference_time",
                "n.queued_at AS queued_at",
                # The episode-to-evidence link identifies the current raw transcript turn. A
                # relationless corrective extraction can use it to retrieve bounded adjacent
                # dialogue without putting assistant context into the persisted episode body.
                "head([(n)-[:ADMITTED_ON]->(t:TurnEvidence) | t.turn_id]) AS turn_evidence_uuid",
                "n.processing_attempts AS processing_attempts",
                "n.processing_owner AS processing_owner",
                "n.processing_lease_expires_at AS processing_lease_expires_at",
            )
            .build()
        )
        rows = self.neo4j.execute(
            claim_query,
            params={
                "episode_uuid": episode_uuid,
                "max_attempts": max_attempts,
                "context_retry_attempts": context_cap,
                # Retry-budget gate inside the claim query: only operator-clearable errors.
                "context_error_markers": list(_RECOVERABLE_CONTEXT_WINDOW_MARKERS),
                "worker_id": worker_id,
                "lease_seconds": max(1, lease_seconds),
            },
        )
        if rows:
            return rows[0]

        if not precheck_rows:
            logger.debug("claim_pending_episode skipped uuid=%s reason=missing", episode_uuid)
            return None

        current = precheck_rows[0]
        state = str(current.get("processing_state") or "")
        attempts = int(current.get("processing_attempts") or 0)
        error = str(current.get("processing_error") or "")
        # Retry-budget decision: only the operator-clearable variant earns the extended cap.
        context_window_error = is_recoverable_context_window_error(error)
        if state not in {"PENDING", "FAILED"}:
            reason = f"state={state or 'unknown'}"
        elif attempts >= max_attempts and not (
            state == "FAILED" and attempts < context_cap and context_window_error
        ):
            reason = f"attempts_exhausted attempts={attempts} max={max_attempts}"
        else:
            reason = "predicate_miss"
        logger.debug("claim_pending_episode skipped uuid=%s reason=%s", episode_uuid, reason)
        return None

    def mark_episode_ready(
        self,
        episode_uuid: str,
        *,
        worker_id: str | None = None,
        required_state: str | None = None,
        resolved_episode_uuid: str,
        nodes_touched: int,
        edges_touched: int,
    ) -> bool:
        query = (
            Cypher()
            .match("(n:Episodic)")
            .where(
                "n.uuid = $episode_uuid",
                "($required_state IS NULL OR n.processing_state = $required_state)",
                "($worker_id IS NULL OR (n.processing_state = 'ENRICHING'"
                " AND n.processing_owner = $worker_id))",
            )
            .set(
                (
                    "n.processing_state = 'READY'",
                    "n.processing_stage = 'ready'",
                    "n.processing_substage = 'complete'",
                    "n.processing_substage_started_at = datetime()",
                    "n.processing_progress = 100.0",
                    "n.processing_steps_completed = coalesce(toInteger(n.processing_steps_total), 5)",
                    "n.processing_completed_at = datetime()",
                    "n.processing_owner = null",
                    "n.processing_lease_expires_at = null",
                    "n.processing_heartbeat_at = datetime()",
                    "n.processing_error = null",
                    "n.processing_llm_active_task = null",
                    "n.processing_llm_active_kind = null",
                    "n.processing_llm_active_model = null",
                    "n.processing_llm_active_endpoint = null",
                    "n.resolved_episode_uuid = $resolved_episode_uuid",
                    "n.enriched_nodes_touched = $nodes_touched",
                    "n.enriched_edges_touched = $edges_touched",
                )
            )
            .return_raw("count(n) AS updated")
            .build()
        )
        rows = self.neo4j.execute(
            query,
            params={
                "episode_uuid": episode_uuid,
                "worker_id": worker_id,
                "required_state": required_state,
                "resolved_episode_uuid": resolved_episode_uuid,
                "nodes_touched": nodes_touched,
                "edges_touched": edges_touched,
            },
        )
        return bool(rows and int(rows[0].get("updated", 0)) > 0)

    def mark_episode_failed(self, episode_uuid: str, error: str, *, worker_id: str | None = None) -> bool:
        query = (
            Cypher()
            .match("(n:Episodic)")
            .where(
                "n.uuid = $episode_uuid",
                "($worker_id IS NULL OR (n.processing_state = 'ENRICHING'"
                " AND n.processing_owner = $worker_id))",
            )
            .set(
                (
                    "n.processing_state = 'FAILED'",
                    "n.processing_stage = 'failed'",
                    "n.processing_substage = 'failed'",
                    "n.processing_substage_started_at = datetime()",
                    "n.processing_progress = CASE"
                    " WHEN n.processing_progress IS NULL THEN 100.0"
                    " ELSE n.processing_progress END",
                    "n.processing_completed_at = datetime()",
                    "n.processing_owner = null",
                    "n.processing_lease_expires_at = null",
                    "n.processing_heartbeat_at = datetime()",
                    "n.processing_error = $error",
                    "n.processing_llm_active_task = null",
                    "n.processing_llm_active_kind = null",
                    "n.processing_llm_active_model = null",
                    "n.processing_llm_active_endpoint = null",
                )
            )
            .return_raw("count(n) AS updated")
            .build()
        )
        rows = self.neo4j.execute(
            query,
            params={
                "episode_uuid": episode_uuid,
                "error": error,
                "worker_id": worker_id,
            },
        )
        return bool(rows and int(rows[0].get("updated", 0)) > 0)

    def mark_episode_pending(
        self,
        episode_uuid: str,
        *,
        retry_after_s: float = 0.0,
        worker_id: str | None = None,
    ) -> bool:
        retry_clause = (
            f"n.retry_after = datetime() + duration({{seconds: {int(max(0, retry_after_s))}}})"
            if retry_after_s > 0
            else "n.retry_after = null"
        )
        query = (
            Cypher()
            .match("(n:Episodic)")
            .where(
                "n.uuid = $episode_uuid",
                "($worker_id IS NULL OR (n.processing_state = 'ENRICHING'"
                " AND n.processing_owner = $worker_id))",
            )
            .set(
                (
                    "n.processing_state = 'PENDING'",
                    "n.processing_stage = 'queued'",
                    "n.processing_substage = 'circuit_breaker_requeue'",
                    "n.processing_substage_started_at = datetime()",
                    "n.processing_owner = null",
                    "n.processing_lease_expires_at = null",
                    "n.processing_heartbeat_at = datetime()",
                    "n.processing_error = null",
                    "n.processing_llm_active_task = null",
                    "n.processing_llm_active_kind = null",
                    "n.processing_llm_active_model = null",
                    "n.processing_llm_active_endpoint = null",
                    retry_clause,
                )
            )
            .return_raw("count(n) AS updated")
            .build()
        )
        rows = self.neo4j.execute(
            query,
            params={
                "episode_uuid": episode_uuid,
                "worker_id": worker_id,
            },
        )
        return bool(rows and int(rows[0].get("updated", 0)) > 0)

    def fetch_episode_processing(self, episode_uuid: str) -> dict[str, Any] | None:
        # MEMORY_RETURN_FIELDS already includes processing_substage/
        # processing_substage_started_at and the active LLM task/kind/model/endpoint
        # fields (SSOT-11, see cypher.py's _PROCESSING_DETAIL_FIELDS docstring). This
        # call site used to re-add them explicitly from before that fix and was never
        # cleaned up, producing a RETURN clause with duplicate column aliases --
        # tolerated silently until now, but a genuine syntax error
        # (Neo.ClientError.Statement.SyntaxError: "Multiple result columns with the
        # same name are not supported"), caught live during a 2026-07-12 graphiti-core
        # 0.29.2 upgrade canary (this path only runs during background enrichment
        # finalize, which the offline test suite doesn't exercise against a real
        # Neo4j server).
        query = (
            Cypher()
            .match("(n:Episodic)")
            .where("n.uuid = $episode_uuid")
            .return_fields(MEMORY_RETURN_FIELDS)
            .limit("1")
            .build()
        )
        rows = self.neo4j.execute(query, params={"episode_uuid": episode_uuid})
        return rows[0] if rows else None

    def fetch_relevant_pending_episodes(
        self, query: str, limit: int = 3, *, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        tokens = [token.lower() for token in _PENDING_EPISODE_TOKEN_PATTERN.findall(query or "")]
        if not tokens:
            normalized = (query or "").strip().lower()
            if normalized:
                tokens = [normalized]
        if not tokens:
            return []

        safe_limit = max(1, min(limit, 10))
        cypher = (
            Cypher()
            .match("(n:Episodic)")
            .where(
                "n.processing_state IN ['PENDING', 'ENRICHING']",
                "($namespace IS NULL OR n.namespace = $namespace)",
                "ANY(token IN $tokens"
                " WHERE toLower(coalesce(n.content, '')) CONTAINS token"
                " OR toLower(coalesce(n.name, '')) CONTAINS token)",
            )
            .return_fields(
                EPISODE_PROCESSING_FIELDS,
                "labels(n) AS labels",
                "n.type AS type",
                "n.scope AS scope",
                "n.content AS content",
                "n.summary AS summary",
                "n.resolved_episode_uuid AS resolved_episode_uuid",
            )
            .order_by("coalesce(n.processing_started_at, n.queued_at, n.created_at) ASC, n.uuid")
            .limit()
            .build()
        )
        return self.neo4j.execute(
            cypher,
            params={"tokens": tokens, "limit": safe_limit, "namespace": namespace},
        )

    def fetch_linked_entity_uuids_for_episode(self, episode_uuid: str) -> list[str]:
        query = (
            Cypher()
            .match("(e:Episodic)-[]-(n:Entity)")
            .where("e.uuid = $episode_uuid")
            .return_raw("DISTINCT n.uuid AS uuid")
            .build()
        )
        rows = self.neo4j.execute(query, params={"episode_uuid": episode_uuid})
        return [str(row["uuid"]) for row in rows if row.get("uuid")]

    def fetch_linked_entity_uuids_for_episodes(
        self, episode_uuids: list[str]
    ) -> dict[str, list[str]]:
        """Same as the singular form, for many episodes in ONE round trip.

        CF-75: the recall path ran the singular query once per resolved episode, serially, each
        awaiting a full round trip before starting the next. The saving here is round trips, NOT
        database work -- measured against Neo4j 5, the batched form costs slightly MORE dbHits
        (1,150 vs 900 for 50 episodes) because of the UNWIND and the aggregation. That is the
        opposite of the edge-weight loop, where batching removes N whole-store scans, and the
        difference is why the two were measured separately rather than assumed to share a profile.

        Returns a uuid -> linked-entity-uuids map. The caller re-orders by its own input list, so
        this deliberately does not promise UNWIND's row order survives the aggregation.
        """
        if not episode_uuids:
            return {}
        rows = self.neo4j.execute(
            """
            UNWIND $episode_uuids AS episode_uuid
            MATCH (e:Episodic)-[]-(n:Entity)
            WHERE e.uuid = episode_uuid
            RETURN episode_uuid AS episode_uuid, collect(DISTINCT n.uuid) AS uuids
            """,
            params={"episode_uuids": list(dict.fromkeys(episode_uuids))},
        )
        return {
            str(row["episode_uuid"]): [str(u) for u in (row.get("uuids") or []) if u]
            for row in rows
            if row.get("episode_uuid")
        }

    def ensure_self_entity(self, namespace: str) -> str:
        """Idempotently MERGE the ONE canonical self :Entity for `namespace` and return its uuid.

        The uuid is DETERMINISTIC per namespace (uuid5), so replay never forks a second self node and
        distinct namespaces never collide (one stable self identity per user silo). It is a PLAIN
        :Entity (no is_view/is_quantstate/view_kind), so the typed-assertion write's
        `OPTIONAL MATCH (n:Entity {uuid})` binding check resolves it and clears `binding_pending` for a
        first-person scalar assertion. It is intentionally NOT episode-linked: first-person binding goes
        through the perception self-seam, not lexical MENTIONS matching, so no per-episode `user` node is
        ever minted. Marked `is_self` + `entity_role='self'` for provenance/observability.

        ABSORBS an extraction-minted twin (`_absorb_self_entity_forks`). Extraction now names the self
        endpoint explicitly so a first-person edge survives, which mints a SECOND `user` :Entity — and
        the two creators are independent, so every namespace where both fire forks deterministically.
        Left alone, the user's facts accumulate on the extraction node while Views anchor to this one,
        splitting one identity in half. Absorbing here rather than in a repair pass means the two never
        coexist: whichever creator runs second folds into this uuid."""
        if not namespace:
            raise ValueError("ensure_self_entity requires a non-empty namespace")
        self_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"menhir-self:{namespace}"))
        self.neo4j.execute(
            """
            MERGE (n:Entity {uuid: $self_uuid})
            ON CREATE SET n.created_at = datetime(), n.summary = $summary
            SET n.name = $name,
                n.namespace = $namespace,
                n.group_id = $namespace,
                n.is_self = true,
                n.entity_role = 'self'
            """,
            params={
                "self_uuid": self_uuid,
                "name": _SELF_ENTITY_NAME,
                "namespace": namespace,
                "summary": "Canonical first-person self identity for this namespace.",
            },
        )
        self._absorb_self_entity_forks(namespace=namespace, self_uuid=self_uuid)
        return self_uuid

    def _absorb_self_entity_forks(self, *, namespace: str, self_uuid: str) -> int:
        """Fold any extraction-minted `user` :Entity in `namespace` into the canonical self node.

        Recall resolves a first-person query's subject by computing `uuid5("menhir-self:<ns>")` with NO
        database read (recall_pipeline G17), so the canonical uuid is fixed and the twin is what has to
        move -- not the other way round.

        Carries the twin's `name_embedding` and richer summary across BEFORE deleting it. This is not
        cosmetic: the canonical node is written by raw Cypher and has no embedding, so it is invisible
        to semantic search. Folding an embedded node into an unembedded one without carrying the vector
        would move every fact the user stated onto a node recall cannot find -- strictly worse than the
        fork. Returns the number of twins absorbed.

        Matches ONLY the exact canonical name. `:Entity` nodes carrying `is_view`/`is_quantstate` are
        never touched: a View is a projection, and absorbing one would fold a derived answer into the
        subject it describes.
        """
        forks = [
            str(row["uuid"])
            for row in self.neo4j.execute(
                f"""
                MATCH (f:Entity)
                WHERE f.group_id = $namespace AND f.uuid <> $self_uuid
                      AND toLower(f.name) = $name
                      AND {non_derived_view_cypher("f")}
                RETURN f.uuid AS uuid
                """,
                params={"namespace": namespace, "self_uuid": self_uuid, "name": _SELF_ENTITY_NAME},
            )
            if row.get("uuid")
        ]
        if not forks:
            return 0

        # Identity first: the embedding and the accumulated summary are what make the surviving node
        # findable. `properties(r)` copies graphiti's edge payload verbatim (uuid, fact, fact_embedding,
        # episodes, valid_at/invalid_at) -- rewiring an entity edge without it would keep the shape of
        # the graph and lose the fact it carried.
        self.neo4j.execute(
            """
            MATCH (c:Entity {uuid: $self_uuid})
            MATCH (f:Entity) WHERE f.uuid IN $forks
            WITH c, f ORDER BY size(coalesce(f.summary, '')) DESC LIMIT 1
            SET c.name_embedding = coalesce(c.name_embedding, f.name_embedding),
                c.labels          = coalesce(c.labels, f.labels),
                c.source          = coalesce(c.source, f.source),
                c.source_confidence = coalesce(c.source_confidence, f.source_confidence),
                c.summary = CASE
                    WHEN size(coalesce(f.summary, '')) > size(coalesce(c.summary, '')) THEN f.summary
                    ELSE c.summary END
            """,
            params={"self_uuid": self_uuid, "forks": forks},
        )
        # `:RELATES_TO` carries TWO unrelated edge families and they cannot be rewired the same way:
        #   * graphiti FACT edges -- uuid, fact, fact_embedding, episodes, valid_at. Keyed by uuid.
        #   * menhir CORRELATION edges -- weight, bridged_from, last_traversed. NO uuid at all.
        # A uuid-keyed MERGE crashes on the second family ("null property value for 'uuid'"), and a
        # bare `MERGE (c)-[:RELATES_TO]->(m)` for them would MATCH an existing FACT edge between the
        # same pair and overwrite it with correlation properties -- destroying the fact. So the
        # identified family MERGEs on its identity and the anonymous family is CREATEd outright.
        # CREATE cannot duplicate on replay: the twin is deleted in this same call, so there is never
        # a second pass with edges left to move.
        for statement in (
            # Outgoing entity edges. Self-loops are dropped: an edge from the twin BACK to the canonical
            # node describes the user relating to themselves, which is an artifact of the split.
            """
            MATCH (f:Entity)-[r:RELATES_TO]->(m)
            WHERE f.uuid IN $forks AND m.uuid <> $self_uuid AND r.uuid IS NOT NULL
            MATCH (c:Entity {uuid: $self_uuid})
            MERGE (c)-[r2:RELATES_TO {uuid: r.uuid}]->(m)
            SET r2 = properties(r)
            """,
            """
            MATCH (f:Entity)-[r:RELATES_TO]->(m)
            WHERE f.uuid IN $forks AND m.uuid <> $self_uuid AND r.uuid IS NULL
            MATCH (c:Entity {uuid: $self_uuid})
            CREATE (c)-[r2:RELATES_TO]->(m)
            SET r2 = properties(r)
            """,
            """
            MATCH (m)-[r:RELATES_TO]->(f:Entity)
            WHERE f.uuid IN $forks AND m.uuid <> $self_uuid AND r.uuid IS NOT NULL
            MATCH (c:Entity {uuid: $self_uuid})
            MERGE (m)-[r2:RELATES_TO {uuid: r.uuid}]->(c)
            SET r2 = properties(r)
            """,
            """
            MATCH (m)-[r:RELATES_TO]->(f:Entity)
            WHERE f.uuid IN $forks AND m.uuid <> $self_uuid AND r.uuid IS NULL
            MATCH (c:Entity {uuid: $self_uuid})
            CREATE (m)-[r2:RELATES_TO]->(c)
            SET r2 = properties(r)
            """,
            # Episode provenance: which episodes mentioned the self.
            """
            MATCH (e:Episodic)-[:MENTIONS]->(f:Entity) WHERE f.uuid IN $forks
            MATCH (c:Entity {uuid: $self_uuid})
            MERGE (e)-[:MENTIONS]->(c)
            """,
            """
            MATCH (f:Entity) WHERE f.uuid IN $forks
            DETACH DELETE f
            """,
        ):
            self.neo4j.execute(
                statement, params={"self_uuid": self_uuid, "forks": forks}
            )
        logger.info(
            "Absorbed %d extraction-minted self entit%s into the canonical self namespace=%s",
            len(forks), "y" if len(forks) == 1 else "ies", namespace,
        )
        return len(forks)

    def lookup_entities_by_normalized_names(
        self, namespace: str, spellings: list[str],
    ) -> list[dict[str, str]]:
        """Exact same-namespace :Entity lookup by a bounded list of NORMALIZED name spellings — the
        optional repository fallback for typed-scalar binding AFTER exact local episode matching fails
        (C.4.3). Returns `{uuid, name}` rows, or [] when nothing matches.

        Deliberately exact and bounded — this is a name-equality query over a caller-provided list of
        normalized spellings, NOT fuzzy/substring/stem/synonym matching; the caller still runs the
        unique fail-closed binder over the returned rows. Fail-closed on every axis:
          * requires a NONBLANK `namespace` — an empty/absent namespace returns [] (never a
            namespace-blind lookup that could leak across tenants);
          * enforces `group_id` EQUALITY (`n.group_id = $namespace`), so a subject can never bind to an
            entity from a different namespace silo;
          * matches only `toLower(trim(n.name)) IN $spellings` — the SAME normalize-then-lower form the
            unique binder uses (`str(name).strip().lower()`), so a name that carries surrounding
            whitespace is still found;
          * EXCLUDES derived View nodes (`is_view`/`is_quantstate`/`view_kind`), so a scalar assertion
            can never bind to a projection;
          * EXCLUDES blank-uuid rows.
        """
        safe_spellings = [s for s in (spellings or []) if isinstance(s, str) and s.strip()]
        if not (namespace and namespace.strip()) or not safe_spellings:
            return []
        rows = self.neo4j.execute(
            f"""
            MATCH (n:Entity)
            WHERE n.group_id = $namespace
              AND toLower(trim(n.name)) IN $spellings
              AND n.uuid IS NOT NULL AND trim(toString(n.uuid)) <> ''
              AND {non_derived_view_cypher("n")}
            RETURN DISTINCT n.uuid AS uuid, n.name AS name,
                   coalesce(n.is_view, false) AS is_view,
                   coalesce(n.is_quantstate, false) AS is_quantstate,
                   n.view_kind AS view_kind
            """,
            params={"namespace": namespace, "spellings": [s.strip() for s in safe_spellings]},
        )
        out: list[dict[str, str]] = []
        for row in rows:
            if not row.get("uuid"):
                continue
            if row.get("is_view") or row.get("is_quantstate") or row.get("view_kind"):
                continue  # defense-in-depth: never surface a derived View node
            out.append({"uuid": str(row["uuid"]), "name": str(row.get("name") or "")})
        return out

    def fetch_linked_entities_for_episode(self, episode_uuid: str) -> list[dict[str, str]]:
        """Surviving REAL entities linked to an episode as {uuid, name} rows — the post-finalization
        binding candidates for ScalarStateView typed-scalar perception (C.4.3). Name-carrying variant
        of `fetch_linked_entity_uuids_for_episode`: binding matches the extracted subject_text against
        these names, so the uuid list alone is insufficient.

        `episode_uuid` accepts EITHER identifier kind, because the scalar batch does not always key on
        an episode. When `:TurnEvidence` exists, `load_next_scalar_batch` takes the G14 branch and
        returns rows keyed by `turn_id` ("turn_id as the grounding anchor"), which then flows through
        `build_episodes` into the proposal's `episode_uuid`. Matching that id against `Episodic.uuid`
        alone can NEVER hit, so the candidate list came back empty on every call and every non-self
        subject abstained from authority — silently, because `_resolve_subject` tries the self seam
        first and that path bypasses this lookup entirely. So a turn_id is resolved to its episode
        through the `ADMITTED_ON` admission join. Accepting both kinds here (rather than at the call
        site) keeps the single-id contract the binder and the repair pass share.

        DERIVED View nodes are EXCLUDED. Recallable Views (counter and scalar_state) are themselves
        stored as `:Entity` (carrying `is_view`/`view_kind`, and counters `is_quantstate`) and are
        linked to their source episodes via `(:Episodic)-[:MENTIONS]->(view:Entity)` — and the
        scheduler writes counter Views BEFORE typed-scalar binding runs, so an unfiltered lookup would
        offer a View as a binding target and could bind a scalar assertion to a projection. Filtered
        BOTH in Cypher (at source) and in Python (defense-in-depth on the returned rows). DISTINCT by
        uuid."""
        # Entities attach to GRAPHITI's episode node, not to menhir's pending one. The two are
        # content-identical twins created by different writers; the pending node points at its twin
        # through `resolved_episode_uuid`. Anchoring on the pending node alone returns nothing, so
        # the anchor set is BOTH: whichever node the id names, plus its resolved twin.
        # f-string: the helper is interpolated, so every LITERAL Cypher brace below is doubled.
        query = f"""
            MATCH (a:Episodic)
            WHERE a.uuid = $episode_uuid
               OR EXISTS {{ (a)-[:ADMITTED_ON]->(:TurnEvidence {{turn_id: $episode_uuid}}) }}
            OPTIONAL MATCH (g:Episodic {{uuid: a.resolved_episode_uuid}})
            WITH collect(DISTINCT a) + collect(DISTINCT g) AS anchors
            UNWIND anchors AS e
            MATCH (e)-[]-(n:Entity)
            WHERE {non_derived_view_cypher("n")}
            RETURN DISTINCT n.uuid AS uuid, n.name AS name,
                   coalesce(n.is_view, false) AS is_view,
                   coalesce(n.is_quantstate, false) AS is_quantstate,
                   n.view_kind AS view_kind
        """
        rows = self.neo4j.execute(query, params={"episode_uuid": episode_uuid})
        out: list[dict[str, str]] = []
        for row in rows:
            if not row.get("uuid"):
                continue
            if row.get("is_view") or row.get("is_quantstate") or row.get("view_kind"):
                continue  # defense-in-depth: never bind a scalar assertion to a derived View node
            out.append({"uuid": str(row["uuid"]), "name": str(row.get("name") or "")})
        return out
