"""Episode lifecycle state machine helpers.

Owns the :class:`EpisodeLifecycleRepository` facade: the tenancy-ratchet and CF-158/CF-200
pinned write/admission/query methods stay defined here, while the intake, claim/queue,
transition, and read method families live in the ``episode_lifecycle_repo_*`` sibling
modules and the retry classifiers live in ``episode_lifecycle_retry``. Everything this
module exported before the split is still importable from here.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from menhir.domain.namespace import namespace_to_group_id
from menhir.domain.self_identity import self_uuid_for_namespace
from menhir.domain.utils import source_confidence_for
from menhir.infrastructure.cypher import (
    Cypher,
    EPISODE_PROCESSING_FIELDS,
    non_derived_view_cypher,
)
from menhir.infrastructure.episode_lifecycle_repo_claim import _EpisodeClaimMixin
from menhir.infrastructure.episode_lifecycle_repo_intake import _EpisodeIntakeMixin
from menhir.infrastructure.episode_lifecycle_repo_reads import _EpisodeReadsMixin
from menhir.infrastructure.episode_lifecycle_repo_transitions import _EpisodeTransitionsMixin
from menhir.infrastructure.episode_lifecycle_retry import (
    TRANSIENT_RETRY_CAP,
    is_context_window_error_text,
    is_recoverable_context_window_error,
)

logger = logging.getLogger(__name__)

#: Canonical name for the per-namespace self :Entity (matches the extraction prompt's "user" subject
#: convention, so a materialized first-person View reads naturally).
_SELF_ENTITY_NAME = "user"

_PENDING_EPISODE_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]{3,}")


class EpisodeLifecycleRepository(
    _EpisodeIntakeMixin,
    _EpisodeClaimMixin,
    _EpisodeTransitionsMixin,
    _EpisodeReadsMixin,
):
    """Episode creation, claiming, and state transitions."""

    neo4j: Any

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

    def ensure_self_entity(self, namespace: str) -> str:
        """Idempotently MERGE the ONE canonical self :Entity for `namespace` and return its uuid.

        The uuid is DETERMINISTIC per namespace (uuid5), so replay never forks a second self node and
        distinct namespaces never collide (one stable self identity per user silo). It is a PLAIN
        :Entity (no is_view/is_quantstate/view_kind), so the typed-assertion write's
        `OPTIONAL MATCH (n:Entity {uuid})` binding check resolves it and clears `binding_pending` for a
        first-person scalar assertion. It is intentionally NOT episode-linked: first-person binding goes
        through the perception self-seam, not lexical MENTIONS matching, so no per-episode `user` node is
        ever minted. Marked `is_self` + `entity_role='self'` for provenance/observability.

NON-DESTRUCTIVE. It creates or updates the canonical target and nothing else. If same-named
        forks exist it REPORTS them (`SELF_FORKS_REQUIRE_MIGRATION`) and leaves them untouched;
        consolidating them is an operator-only, journaled migration, never a side effect of a write."""
        if not namespace:
            raise ValueError("ensure_self_entity requires a non-empty namespace")
        self_uuid = self_uuid_for_namespace(namespace)
        self.neo4j.execute(
            """
            MERGE (n:Entity {uuid: $self_uuid})
            ON CREATE SET n.created_at = datetime(), n.summary = $summary
            SET n.name = $name,
                n.namespace = $namespace,
                n.group_id = $group_id,
                n.is_self = true,
                n.entity_role = 'self'
            """,
            params={
                "self_uuid": self_uuid,
                "name": _SELF_ENTITY_NAME,
                "namespace": namespace,
                # Logical `default` maps to physical "". Writing the logical name here is the
                # activation hazard the RCA recorded: it would target a partition holding none of
                # the production data.
                "group_id": namespace_to_group_id(namespace),
                "summary": "Canonical first-person self identity for this namespace.",
            },
        )
        forks = self.detect_self_forks(namespace=namespace, self_uuid=self_uuid)
        if forks:
            # DO NOT absorb here. The old path bulk-rewired and DETACH DELETEd every same-named
            # node, which is lossy (it drops fork-to-canonical edges as "split artifacts") and
            # cannot be reviewed or undone. Consolidation is now an operator-only, journaled
            # migration driven by an approved UUID manifest -- never a side effect of a write.
            logger.warning(
                "SELF_FORKS_REQUIRE_MIGRATION namespace=%s canonical=%s forks=%d",
                namespace, self_uuid, len(forks),
            )
        return self_uuid

    def detect_self_forks(self, *, namespace: str, self_uuid: str) -> list[str]:
        """Return the uuids of same-named self forks in `namespace`. READ ONLY.

        Replaces `_absorb_self_entity_forks`, which rewired every incident relationship and ended
        in `DETACH DELETE` as a side effect of an ordinary write. That was lossy and unreviewable:
        its `m.uuid <> $self_uuid` predicates deliberately DROPPED fork-to-canonical relationships
        as "split artifacts". Consolidation is now an operator-only, journaled migration driven by
        an approved UUID manifest, and its relationship contract preserves exactly those edges --
        so that Cypher must not be revived, adapted, or used as a migration template.

        Transitional read: accepts BOTH physical spellings. The old writer stamped
        `group_id = $namespace`, so a `default` namespace wrote group "default", while the
        namespace SSOT maps `default` -> "". Detection must see forks under either.

        Matches only the exact canonical name, and never `is_view`/`is_quantstate` nodes: a View is
        a projection, and folding one into the subject it describes would destroy a derived answer.
        """
        group_ids = [namespace_to_group_id(namespace)]
        if namespace not in group_ids:
            group_ids.append(namespace)
        return [
            str(row["uuid"])
            for row in self.neo4j.execute(
                f"""
                MATCH (f:Entity)
                WHERE f.group_id IN $group_ids AND f.uuid <> $self_uuid
                      AND toLower(f.name) = $name
                      AND {non_derived_view_cypher("f")}
                RETURN f.uuid AS uuid
                """,
                params={
                    "group_ids": group_ids,
                    "self_uuid": self_uuid,
                    "name": _SELF_ENTITY_NAME,
                },
            )
            if row.get("uuid")
        ]

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
