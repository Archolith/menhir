"""Adapter layer that hides graph-storage internals from policy services.

This module is a thin façade: all operations are delegated to focused
sub-repositories defined in the neighbouring modules.

Sub-repositories:
- ``EpisodeRepository``       — episode_repository.py
- ``ConsolidationRepository`` — consolidation_queries.py
- ``MemoryQueryRepository``   — memory_queries.py
"""

from __future__ import annotations

import logging
import uuid as uuidlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from menhir.infrastructure.episode_repository import (
    EpisodeRepository,
    PolicyStampResult,
    is_context_window_error_text,
    is_recoverable_context_window_error,
)
from menhir.infrastructure.consolidation_queries import ConsolidationRepository
from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.neo4j import Neo4jRepository
from menhir.infrastructure.schema import (
    PHASE_ONE_REQUIRED_CONSTRAINTS,
    PHASE_ONE_REQUIRED_INDEXES,
    SCALAR_STATE_REQUIRED_INDEXES,
    get_phase1_bootstrap_queries,
)

logger = logging.getLogger(__name__)

# Re-export so existing callers importing from this module still work.
__all__ = [
    "MemoryGraphAdapter",
    "PhaseOneSchemaResult",
    "PolicyStampResult",
    "is_context_window_error_text",
    "is_recoverable_context_window_error",
]


@dataclass
class PhaseOneSchemaResult:
    """Result shape for phase-1 schema bootstrap."""

    success: bool
    queries_executed: int
    failures: list[str]

    @property
    def query_failures(self) -> list[str]:
        """Compatibility alias for callers that expect a descriptive failure list."""
        return self.failures


@dataclass
class MemoryGraphAdapter:
    """Thin façade over graph storage; delegates to focused sub-repositories."""

    neo4j: Neo4jRepository

    def __post_init__(self) -> None:
        from menhir.infrastructure.memory_queries import MemoryQueryRepository
        from menhir.infrastructure.structure_queries import StructureGraphWriter
        from menhir.infrastructure.todo_repository import TodoRepository
        from menhir.infrastructure.temporal_repository import TemporalRepository
        from menhir.infrastructure.candidate_repository import CandidateRepository
        from menhir.infrastructure.artifact_repository import ArtifactRepository
        from menhir.infrastructure.view_repository import ViewRepository
        from menhir.infrastructure.work_artifact_repository import WorkArtifactRepository
        from menhir.infrastructure.personal_memory_queries import PersonalMemoryRepository
        from menhir.infrastructure.turn_evidence_repository import TurnEvidenceRepository

        self._memory_queries = MemoryQueryRepository(self.neo4j)
        self._episodes = EpisodeRepository(self.neo4j)
        self._consolidation = ConsolidationRepository(self.neo4j)
        self._correlation = CorrelationRepository(self.neo4j)
        self._structure = StructureGraphWriter(self.neo4j)
        self._todos = TodoRepository(self.neo4j)
        self._temporal = TemporalRepository(self.neo4j)
        self._candidates = CandidateRepository(self.neo4j)
        # _artifacts is the L4 institutional loop (Decision/Failure/Incident);
        # _work_artifacts is the engineering-document model. Different classes
        # answering different questions -- see domain/work_artifact.py.
        self._artifacts = ArtifactRepository(self.neo4j)
        self._work_artifacts = WorkArtifactRepository(self.neo4j)
        self._views = ViewRepository(self.neo4j)
        self._personal_memory = PersonalMemoryRepository(self.neo4j)
        self._turn_evidence = TurnEvidenceRepository(self.neo4j)
        from menhir.infrastructure.tool_event_repository import ToolEventRepository
        self._tool_events = ToolEventRepository(self.neo4j)
        from menhir.infrastructure.typed_assertion_repository import TypedAssertionRepository
        self._typed_assertions = TypedAssertionRepository(self.neo4j)
        from menhir.infrastructure.typed_event_repository import TypedEventAssertionRepository
        self._typed_event_assertions = TypedEventAssertionRepository(self.neo4j)

    # -------------------------------------------------------------------------
    # Schema bootstrap (stays here — uses SHOW INDEXES admin query)
    # -------------------------------------------------------------------------

    def phase_one_schema_ready(self) -> bool:
        """Return True when the core phase-one indexes already exist and are online."""

        rows = self.neo4j.execute(
            """
            SHOW INDEXES YIELD name, state
            WHERE name IN $names AND state = 'ONLINE'
            RETURN collect(name) AS names
            """,
            params={"names": list(PHASE_ONE_REQUIRED_INDEXES)},
        )
        online = {str(name) for name in (rows[0].get("names", []) if rows else [])}
        if not all(name in online for name in PHASE_ONE_REQUIRED_INDEXES):
            return False

        required_constraints = {
            name: (constraint_type, entity_type, labels, properties)
            for name, constraint_type, entity_type, labels, properties
            in PHASE_ONE_REQUIRED_CONSTRAINTS
        }
        constraint_rows = self.neo4j.execute(
            """
            SHOW CONSTRAINTS
            YIELD name, type, entityType, labelsOrTypes, properties
            WHERE name IN $names
            RETURN name, type, entityType, labelsOrTypes, properties
            """,
            params={"names": list(required_constraints)},
        )
        actual_constraints = {
            str(row.get("name") or ""): (
                str(row.get("type") or ""),
                str(row.get("entityType") or ""),
                tuple(str(value) for value in (row.get("labelsOrTypes") or [])),
                tuple(str(value) for value in (row.get("properties") or [])),
            )
            for row in constraint_rows
        }
        return all(
            actual_constraints.get(name) == expected
            for name, expected in required_constraints.items()
        )

    def scalar_state_schema_ready(self) -> bool:
        """Return True when the ScalarStateView typed-assertion DDL is online. Feature-scoped: a
        deploy with `enable_scalar_state` should gate on this in addition to phase_one. These
        constraints/indexes are ACTIVATION-created (via `activate_scalar_state`, behind the
        identity-version gate) — NOT created by `bootstrap_phase_one` — and are excluded from
        PHASE_ONE_REQUIRED_INDEXES. So this returns False until activation has run successfully."""
        rows = self.neo4j.execute(
            """
            SHOW INDEXES YIELD name, state
            WHERE name IN $names AND state = 'ONLINE'
            RETURN collect(name) AS names
            """,
            params={"names": list(SCALAR_STATE_REQUIRED_INDEXES)},
        )
        online = {str(name) for name in (rows[0].get("names", []) if rows else [])}
        return all(name in online for name in SCALAR_STATE_REQUIRED_INDEXES)

    def activate_scalar_state(self) -> dict[str, Any]:
        """Gated scalar-state activation (ScalarStateView Piece C.3). Refuses over a legacy store
        (raising ScalarStateActivationError from the typed-assertion repo) and otherwise brings the
        source_key-anchored typed-assertion DDL online. This is the ONLY path that creates the
        SCALAR_STATE_REQUIRED_INDEXES — they are deliberately excluded from bootstrap_phase_one so
        the identity space is never established silently over mixed identities. C.4 calls this when
        `enable_scalar_state` is set, in addition to (not instead of) bootstrap_phase_one."""
        return self._typed_assertions.activate_scalar_state()

    def record_typed_assertion(self, assertion: Any) -> dict[str, Any]:
        """Persist one durable :TypedAssertion (ScalarStateView Piece C.1 store). Delegates to the
        typed-assertion repository; used by the C.4.3 typed-scalar perception path AFTER activation."""
        return self._typed_assertions.record_assertion(assertion)

    def pending_advisory_assertions(
        self, *, namespaces: list[str] | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Current :TypedAssertion rows needing binding repair — binding_pending advisories OR
        projection_pending (bound, View not yet rebuilt) — in fairness order (unattempted-first, then
        least-recently attempted), optionally restricted to a namespace allowlist under one global
        limit (ScalarStateView C.4.4). See TypedAssertionRepository."""
        return self._typed_assertions.pending_advisory_assertions(namespaces=namespaces, limit=limit)

    def mark_binding_repair_attempted(self, assertion_ids: list[str], *, at: str) -> int:
        """Stamp `binding_repair_attempted_at` on the rows the repair pass examined, advancing the
        fairness frontier so a bounded repair is eventually complete (ScalarStateView C.4.4)."""
        return self._typed_assertions.mark_binding_repair_attempted(assertion_ids, at=at)

    def mark_projection_complete(self, assertion_ids: list[str]) -> int:
        """Clear the projection_pending crash-recovery marker after a successful View rebuild
        (ScalarStateView C.4.4)."""
        return self._typed_assertions.mark_projection_complete(assertion_ids)

    def fetch_assertion_contributors(self, **kwargs: Any) -> dict[str, Any]:
        """Hydrate bounded assertion provenance for synthetic scalar authority verdicts."""
        return self._typed_assertions.fetch_assertion_contributors(**kwargs)

    def entity_exists(self, uuid: str) -> bool:
        """True if an :Entity with this uuid exists (UUID-global, matching binding semantics) — the
        fail-closed survivor check for the C.4.4 orphan pass."""
        return self._typed_assertions.entity_exists(uuid)

    def namespaces_for_operation(
        self, *, merge_op_id: str, absorbed_uuid: str | None = None
    ) -> list[str]:
        """Namespaces a lifecycle op AFFECTS (its rebind records + assertions still on the absorbed
        uuid) — derives the op's scope for namespace-keyed reconciliation (ScalarStateView C.4.4)."""
        return self._typed_assertions.namespaces_for_operation(
            merge_op_id=merge_op_id, absorbed_uuid=absorbed_uuid)

    def namespaces_for_unmerge(self, *, unmerge_op_id: str, merge_op_id: str) -> list[str]:
        """Namespaces an unmerge affects: the forward merge's surviving rebind records UNION this
        unmerge's own pending/complete reconcile markers. The marker source keeps the namespace
        discoverable after restore deleted the rebind records but rebuild failed (ScalarStateView
        C.4.4)."""
        return self._typed_assertions.namespaces_for_unmerge(
            unmerge_op_id=unmerge_op_id, merge_op_id=merge_op_id)

    def orphaned_assertions(
        self, *, namespaces: list[str] | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Distinct (subject_uuid, namespace) orphan work items in fairness order, optionally
        restricted to a namespace allowlist (ScalarStateView C.4.4). See TypedAssertionRepository."""
        return self._typed_assertions.orphaned_assertions(namespaces=namespaces, limit=limit)

    def mark_orphan_repair_attempted(self, work_items: list[dict[str, Any]], *, at: str) -> int:
        """Stamp orphan_repair_attempted_at on examined (subject_uuid, namespace) orphans, advancing
        the fairness frontier (ScalarStateView C.4.4)."""
        return self._typed_assertions.mark_orphan_repair_attempted(work_items, at=at)

    def scalar_state_service(self, *, scalar_history_enabled: bool = False) -> Any:
        """Build a `ScalarStateService` bound to this adapter's typed-assertion store (fold input) and
        its scalar-state View sink. Used by the C.4.3 perception path to rebuild Views after persist."""
        from menhir.services.scalar_state_service import ScalarStateService
        return ScalarStateService(
            self._typed_assertions, self,
            scalar_history_enabled=scalar_history_enabled)

    # -------------------------------------------------------------------------
    # Typed-event assertion delegates → TypedEventAssertionRepository
    # -------------------------------------------------------------------------

    def activate_event_history(self) -> dict[str, Any]:
        """Bring the event-assertion DDL online (idempotent, IF NOT EXISTS)."""
        return self._typed_event_assertions.activate()

    def record_typed_event_assertion(self, assertion: Any) -> dict[str, Any]:
        """Persist one :TypedEventAssertion to the durable event log."""
        return self._typed_event_assertions.record_event_assertion(assertion)

    def event_assertions_for_lane(
        self,
        lane: Any,
        *,
        include_superseded: bool = False,
        materializable_only: bool = False,
    ) -> list[Any]:
        """Reconstructed assertions in one event lane (exact namespace/subject/predicate/domain)."""
        return self._typed_event_assertions.assertions_for_lane(
            lane,
            include_superseded=include_superseded,
            materializable_only=materializable_only,
        )

    def event_assertions_for_subject_predicate(
        self,
        subject_uuid: str,
        predicate: str,
        *,
        namespace: str | None = None,
        include_superseded: bool = False,
        materializable_only: bool = True,
    ) -> list[Any]:
        """Reconstructed assertions for an exact normalized subject_uuid + predicate across ALL actual
        domain lanes (optionally namespace-scoped); no domain filter/override. Default excludes
        superseded and binding-pending (safe for recall)."""
        return self._typed_event_assertions.assertions_for_subject_predicate(
            subject_uuid,
            predicate,
            namespace=namespace,
            include_superseded=include_superseded,
            materializable_only=materializable_only,
        )

    def typed_event_assertion_by_key(self, assertion_key: str) -> Any:
        """Reconstruct the event assertion with `assertion_key`, or None."""
        return self._typed_event_assertions.assertion_by_key(assertion_key)

    def current_typed_event_assertion_for_source(self, source_key: str) -> Any:
        """Reconstruct the CURRENT event assertion for a binding-stable source_key, or None."""
        return self._typed_event_assertions.current_for_source(source_key)

    def event_history_service(self) -> Any:
        """Build an `EventHistoryService` bound to this adapter's event-assertion log (source) and
        to the adapter itself as the event-lane timeline View sink."""
        from menhir.services.event_history_service import EventHistoryService
        return EventHistoryService(self._typed_event_assertions, self)

    def bootstrap_phase_one(self) -> PhaseOneSchemaResult:
        """Apply phase-1 schema fields and indexes in an idempotent manner."""
        queries = get_phase1_bootstrap_queries()
        failures: list[str] = []
        for query in queries:
            try:
                self.neo4j.execute(query)
            except Exception as exc:  # pragma: no cover - external dependency behavior
                failures.append(str(exc))
        return PhaseOneSchemaResult(
            success=len(failures) == 0,
            queries_executed=len(queries),
            failures=failures,
        )

    # -------------------------------------------------------------------------
    # Episode lifecycle delegates → EpisodeRepository
    # -------------------------------------------------------------------------

    def sync_edge_counts(self) -> int:
        return self._consolidation.sync_edge_counts()

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
        return self._episodes.create_pending_episode(
            episode_uuid=episode_uuid,
            name=name,
            content=content,
            session_id=session_id,
            user_id=user_id,
            source=source,
            source_confidence=source_confidence,
            diff=diff,
            user_flagged=user_flagged,
            bootstrap_scope=bootstrap_scope,
            namespace=namespace,
            reference_time=reference_time,
        )

    def link_episode_admission(
        self, *, episode_uuid: str, turn_evidence_uuid: str, namespace: str | None = None
    ) -> bool:
        return self._episodes.link_episode_admission(
            episode_uuid=episode_uuid,
            turn_evidence_uuid=turn_evidence_uuid,
            namespace=namespace,
        )

    def create_evidence_projection(
        self, *, turn_evidence_uuid: str, projection_uuid: str, name: str,
        session_id: str, user_id: str, namespace: str,
    ) -> str | None:
        """See EpisodeLifecycleRepository.create_evidence_projection: a non-recallable episode
        carrying a captured turn's verbatim text, so entities exist in the user's own vocabulary."""
        return self._episodes.create_evidence_projection(
            turn_evidence_uuid=turn_evidence_uuid,
            projection_uuid=projection_uuid,
            name=name,
            session_id=session_id,
            user_id=user_id,
            namespace=namespace,
        )

    def list_pending_episode_uuids(
        self, *, max_attempts: int, limit: int = 100
    ) -> list[str]:
        return self._episodes.list_pending_episode_uuids(
            max_attempts=max_attempts, limit=limit
        )

    def fetch_failed_episode_retry_candidates(
        self, limit: int = 100
    ) -> list[dict[str, Any]]:
        return self._episodes.fetch_failed_episode_retry_candidates(limit)

    def fetch_failed_error_signatures(self, limit: int = 25) -> list[dict[str, Any]]:
        """Return FAILED episodes grouped by error text, most common first."""
        return self._episodes.fetch_failed_error_signatures(limit)

    def find_completed_episode_artifact(
        self,
        *,
        anchor_uuid: str,
        anchor_name: str,
    ) -> dict[str, Any] | None:
        return self._episodes.find_completed_episode_artifact(
            anchor_uuid=anchor_uuid, anchor_name=anchor_name
        )

    def claim_pending_episode(
        self,
        episode_uuid: str,
        *,
        max_attempts: int,
        context_retry_attempts: int | None = None,
        worker_id: str,
        lease_seconds: int,
    ) -> dict[str, Any] | None:
        return self._episodes.claim_pending_episode(
            episode_uuid,
            max_attempts=max_attempts,
            context_retry_attempts=context_retry_attempts,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )

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
        return self._episodes.mark_episode_ready(
            episode_uuid,
            worker_id=worker_id,
            required_state=required_state,
            resolved_episode_uuid=resolved_episode_uuid,
            nodes_touched=nodes_touched,
            edges_touched=edges_touched,
        )

    def mark_episode_failed(
        self, episode_uuid: str, error: str, *, worker_id: str | None = None
    ) -> bool:
        return self._episodes.mark_episode_failed(
            episode_uuid, error, worker_id=worker_id
        )

    def mark_episode_pending(
        self,
        episode_uuid: str,
        *,
        retry_after_s: float = 0.0,
        worker_id: str | None = None,
    ) -> bool:
        return self._episodes.mark_episode_pending(
            episode_uuid,
            retry_after_s=retry_after_s,
            worker_id=worker_id,
        )

    def create_raw_capture_entity(
        self,
        episode_uuid: str,
        name: str,
        content: str,
        namespace: str,
        session_id: str,
        user_id: str,
        source: str,
    ) -> str | None:
        """Create a raw-capture entity for a failed episode with memorable content.

        The entity is created with minimal metadata, then stamped via the standard
        stamp_ingest_metadata choke point to ensure trust metadata flows correctly.
        """
        from menhir.domain.utils import source_confidence_for
        from menhir.domain.namespace import stamped_namespace

        entity_uuid = self._episodes.create_raw_capture_entity(
            episode_uuid=episode_uuid,
            name=name,
            content=content,
            namespace=namespace,
            session_id=session_id,
            user_id=user_id,
            source=source,
        )
        if entity_uuid:
            # Stamp through the standard choke point to ensure trust metadata flows correctly
            try:
                self.stamp_ingest_metadata(
                    node_uuids=[entity_uuid],
                    edge_uuids=[],
                    session_id=session_id,
                    user_id=user_id,
                    source=source,
                    source_confidence=source_confidence_for(source),
                    namespace=stamped_namespace(namespace),
                )
            except (OSError, RuntimeError) as e:
                import logging
                logging.getLogger(__name__).warning(
                    "Failed to stamp raw-capture entity %s: %s",
                    entity_uuid,
                    e,
                )
                # Entity was created but stamping failed — this is a partial failure
                # but we return the UUID so the episode knows a capture was attempted
        return entity_uuid

    def mark_raw_capture_superseded(self, episode_uuid: str) -> bool:
        """Mark raw-capture entities as GONE when repair succeeds."""
        return self._episodes.mark_raw_capture_superseded(episode_uuid)

    def fail_exhausted_pending_episodes(self, *, max_attempts: int) -> int:
        """Mark exhausted PENDING episodes as FAILED, with raw-capture creation for each.

        PART 2: Creates raw-capture entities for exhausted episodes with content before
        marking them as failed, so terminal breakage preserves the episode text for recall.

        This body was dead until 2026-08-19: a second, bare-delegation definition of the same
        method later in the class shadowed it, so an exhausted episode was marked FAILED with
        no raw capture and its text never reached recall (recall searches ``:Entity``; the
        surviving ``:Episodic`` node is not one). Restoring it also switches its cost back on,
        which is why ``fetch_exhausted_pending_episodes`` is now bounded by a LIMIT and
        ``raw_capture_for`` is indexed -- the per-episode MERGE below is a label scan without it.
        """
        import logging

        logger = logging.getLogger(__name__)

        # Fetch episodes that will be marked as exhausted
        exhausted_episodes = self._episodes.fetch_exhausted_pending_episodes(
            max_attempts=max_attempts
        )

        # Create raw-captures for episodes with content (best-effort, don't block failure)
        for row in exhausted_episodes:
            content = str(row.get("content") or "").strip()
            if content:  # Only create capture if there's content
                try:
                    episode_uuid = row.get("episode_uuid")
                    capture_name = content[:60].replace("\n", " ").strip()
                    self.create_raw_capture_entity(
                        episode_uuid=episode_uuid,
                        name=capture_name,
                        content=content,
                        namespace=row.get("namespace") or "default",
                        session_id=row.get("session_id") or "",
                        user_id=row.get("user_id") or "",
                        source=row.get("source") or "claude-code",
                    )
                except Exception as e:
                    logger.debug(
                        "Failed to create raw-capture for exhausted episode %s: %s",
                        row.get("episode_uuid"),
                        e,
                    )

        # Now mark all exhausted episodes as failed
        return self._episodes.fail_exhausted_pending_episodes(max_attempts=max_attempts)

    def fetch_episode_processing(self, episode_uuid: str) -> dict[str, Any] | None:
        return self._episodes.fetch_episode_processing(episode_uuid)

    def fetch_relevant_pending_episodes(
        self, query: str, limit: int = 3, *, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        return self._episodes.fetch_relevant_pending_episodes(
            query, limit, namespace=namespace
        )

    def fetch_linked_entity_uuids_for_episode(self, episode_uuid: str) -> list[str]:
        return self._episodes.fetch_linked_entity_uuids_for_episode(episode_uuid)

    def fetch_linked_entity_uuids_for_episodes(
        self, episode_uuids: list[str]
    ) -> dict[str, list[str]]:
        return self._episodes.fetch_linked_entity_uuids_for_episodes(episode_uuids)

    def fetch_linked_entities_for_episode(self, episode_uuid: str) -> list[dict[str, str]]:
        """Surviving entities linked to an episode as {uuid, name} rows — the post-finalization
        binding candidates for ScalarStateView typed-scalar perception (C.4.3)."""
        return self._episodes.fetch_linked_entities_for_episode(episode_uuid)

    def lookup_entities_by_normalized_names(
        self, namespace: str, spellings: list[str],
    ) -> list[dict[str, str]]:
        """Exact same-namespace :Entity lookup by normalized name spellings — the OPTIONAL repository
        fallback for typed-scalar binding after exact local episode matching fails (C.4.3). Fail-closed
        on nonblank namespace, group_id equality, View exclusion, and blank uuids (see
        EpisodeLifecycleRepository.lookup_entities_by_normalized_names)."""
        return self._episodes.lookup_entities_by_normalized_names(namespace, spellings)

    def ensure_self_entity(self, namespace: str) -> str:
        """Idempotently MERGE the canonical per-namespace self :Entity and return its (deterministic)
        uuid — the binding target for first-person typed-scalar assertions (C.4.3 canonical self).

        NON-DESTRUCTIVE. Creates or updates only the canonical target. Pre-existing forks are
        reported, never absorbed; see `EpisodeLifecycleRepository.ensure_self_entity`."""
        return self._episodes.ensure_self_entity(namespace)

    def detect_self_forks(self, namespace: str) -> list[str]:
        """Read-only inventory of same-named self forks for `namespace`.

        Discovery is deliberately separate from consolidation: this reports what an operator-only,
        journaled migration would have to consider, and mutates nothing."""
        return self._episodes.detect_self_forks(
            namespace=namespace,
            self_uuid=self._episodes.ensure_self_entity(namespace),
        )

    def stamp_ingest_metadata(
        self,
        *,
        node_uuids: list[str],
        edge_uuids: list[str],
        session_id: str,
        user_id: str,
        source: str,
        source_confidence: float,
        namespace: str = "default",
        bootstrap_scope: str | None = None,
        belief_commit: str | None = None,
        belief_branch: str | None = None,
    ) -> PolicyStampResult:
        return self._episodes.stamp_ingest_metadata(
            node_uuids=node_uuids,
            edge_uuids=edge_uuids,
            session_id=session_id,
            user_id=user_id,
            source=source,
            source_confidence=source_confidence,
            namespace=namespace,
            bootstrap_scope=bootstrap_scope,
            belief_commit=belief_commit,
            belief_branch=belief_branch,
        )

    def count_namespace(self, group_id: str, *, namespace: str | None = None) -> int:
        """Count nodes in the given graphiti group partition, without deleting anything.

        :TurnEvidence is included here for the same reason it is included in the delete and in
        the pre-erasure capture: these three predicates must name the same set. A dry_run that
        under-reported the blast radius, a capture that missed a subject, and a delete that left
        the node behind were all the same omission.

        Used by the delete_namespace safety gate to report the blast radius before an
        irreversible DETACH DELETE.

        In addition to the ``group_id`` partition (which already covers the namespace-keyed
        :EventConsolidationWatermark cursor) and the scalar/episode namespace rows, counts the
        durable event log: every :TypedEventAssertion in the namespace and every
        :TypedEventAssertionHead that HAS_VERSION to an event assertion in the namespace.
        """
        namespace_clause = (
            " OR (n:Episodic AND n.namespace = $namespace)"
            " OR ((n:TypedAssertion OR n:TypedAssertionHead OR "
            "n:ScalarConsolidationWatermark OR n:TurnEvidence) AND n.namespace = $namespace)"
            " OR (n:TypedEventAssertion AND n.namespace = $namespace)"
            " OR (n:TypedEventAssertionHead AND EXISTS {"
            " MATCH (n)-[:HAS_VERSION]->(ev:TypedEventAssertion) WHERE ev.namespace = $namespace})"
            if namespace is not None else ""
        )
        rows = self.neo4j.execute(
            f"MATCH (n) WHERE n.group_id = $group_id{namespace_clause} "
            "RETURN count(DISTINCT n) AS total",
            params={"group_id": group_id, "namespace": namespace},
        )
        return int(rows[0].get("total", 0)) if rows else 0

    def fetch_node_namespaces(self, uuids: list[str]) -> dict[str, str]:
        """Map uuid -> namespace for nodes that still exist. Missing uuids are absent.

        Used by the CF-165 lineage backfill. Absence is the load-bearing part: a uuid with no
        row here has no provable namespace, and the backfill must leave it NULL rather than
        guess one.
        """
        wanted = [str(u) for u in uuids if u]
        if not wanted:
            return {}
        rows = self.neo4j.execute(
            "MATCH (n) WHERE n.uuid IN $uuids "
            "RETURN n.uuid AS uuid, "
            "coalesce(n.namespace, n.group_id) AS namespace",
            params={"uuids": wanted},
        )
        return {
            str(r["uuid"]): str(r["namespace"])
            for r in (rows or [])
            if r.get("uuid") and r.get("namespace")
        }

    def capture_namespace_uuids(
        self, group_id: str, *, namespace: str | None = None
    ) -> list[str]:
        """Return the uuids in a namespace partition, for capture BEFORE erasure (CF-165).

        Deliberately mirrors ``count_namespace``'s predicate rather than inventing its own: the
        captured set is what a sidecar purge will be keyed on, so it must cover exactly what
        ``delete_namespace`` is about to destroy. If the two predicates drifted, an erasure
        would delete graph nodes whose sidecar content it never recorded a subject for.
        """
        namespace_clause = (
            " OR (n:Episodic AND n.namespace = $namespace)"
            " OR ((n:TypedAssertion OR n:TypedAssertionHead OR "
            "n:ScalarConsolidationWatermark OR n:TurnEvidence) AND n.namespace = $namespace)"
            " OR (n:TypedEventAssertion AND n.namespace = $namespace)"
            " OR (n:TypedEventAssertionHead AND EXISTS {"
            " MATCH (n)-[:HAS_VERSION]->(ev:TypedEventAssertion) WHERE ev.namespace = $namespace})"
            if namespace is not None else ""
        )
        rows = self.neo4j.execute(
            f"MATCH (n) WHERE n.group_id = $group_id{namespace_clause} "
            "AND n.uuid IS NOT NULL RETURN DISTINCT n.uuid AS uuid",
            params={"group_id": group_id, "namespace": namespace},
        )
        return [str(r.get("uuid")) for r in (rows or []) if r.get("uuid")]

    def delete_namespace(self, group_id: str, *, namespace: str | None = None) -> int:
        """Delete every node in the given graphiti group partition; returns the count.

        The caller is responsible for refusing the default/shared group and for any
        node-count safety gate. Intended for tearing down throwaway/eval namespaces.
        """
        operation_id = uuidlib.uuid4().hex
        logical_namespace = str(namespace or group_id).strip()
        result = self._memory_queries.delete_namespace_with_scalar_cascade(
            group_id, logical_namespace, operation_id=operation_id)
        repair = self.scalar_state_service().repair_pending_deletions(
            operation_id=operation_id, as_of=datetime.now(timezone.utc))
        if repair["failed"]:
            logger.warning(
                "namespace %s deleted with %d scalar projection repair(s) pending retry",
                namespace, len(repair["failed"]),
            )
        return int(result["deleted"])

    def flag_memory(
        self, node_uuid: str, bootstrap_scope: str | None = None
    ) -> bool:
        """Persist the explicit v1 retention override on a node."""
        return self._memory_queries.flag_memory(
            node_uuid, bootstrap_scope=bootstrap_scope
        )

    def unflag_memory(self, node_uuid: str) -> bool:
        """Remove the explicit user retention override from a node."""
        return self._memory_queries.unflag_memory(node_uuid)

    def promote_memory(self, node_uuid: str) -> bool:
        """Promote a PERSISTENT memory to PROMOTED (operator-curated ground truth, SSOT-08)."""
        return self._memory_queries.promote_memory(node_uuid)

    def delete_memory(self, node_uuid: str) -> bool:
        """Delete a memory/observation, cascade its assertions, and repair scalar projections."""
        operation_id = uuidlib.uuid4().hex
        result = self._memory_queries.delete_memory_with_scalar_cascade(
            node_uuid, operation_id=operation_id)
        repair = self.scalar_state_service().repair_pending_deletions(
            operation_id=operation_id, as_of=datetime.now(timezone.utc))
        if repair["failed"]:
            logger.warning(
                "memory %s deleted with %d scalar projection repair(s) pending retry",
                node_uuid, len(repair["failed"]),
            )
        return bool(result["touched"])

    def fetch_candidate_metadata(
        self, node_uuids: list[str]
    ) -> list[dict[str, object]]:
        """Fetch scoring-relevant fields for candidate nodes."""
        return self._memory_queries.fetch_candidate_metadata(node_uuids)

    def search_content_embeddings(
        self,
        query_vector: list[float],
        *,
        limit: int = 50,
        group_ids: list[str] | None = None,
    ) -> list[dict[str, object]]:
        return self._memory_queries.search_content_embeddings(
            query_vector, limit=limit, group_ids=group_ids
        )

    def search_assertion_embeddings(
        self,
        query_vector: list[float],
        *,
        limit: int = 50,
        namespaces: list[str] | None = None,
    ) -> list[dict[str, object]]:
        """Observation-lane candidate search over :TypedAssertion embeddings (Phase 4a.2)."""
        return self._memory_queries.search_assertion_embeddings(
            query_vector, limit=limit, namespaces=namespaces
        )

    def fetch_assertion_candidate_metadata(
        self, assertion_ids: list[str]
    ) -> list[dict[str, object]]:
        """Hydrate surfaced :TypedAssertion observations by id (Phase 4a.2 observation lane)."""
        return self._memory_queries.fetch_assertion_candidate_metadata(assertion_ids)

    def fetch_temporal_facts(
        self, node_uuids: list[str]
    ) -> list[dict[str, object]]:
        """Fetch bi-temporal fact-edge state for candidate nodes."""
        return self._memory_queries.fetch_temporal_facts(node_uuids)

    def fetch_candidate_fact_edges(
        self, node_uuids: list[str]
    ) -> list[dict[str, object]]:
        """Fetch fact-edge identity (edge uuid + both endpoints) for candidate nodes."""
        return self._memory_queries.fetch_candidate_fact_edges(node_uuids)

    def fetch_candidate_provenance(
        self, node_uuids: list[str]
    ) -> list[dict[str, object]]:
        """Raw per-candidate provenance (SUPPORTED_BY / ANCHORED_TO project / MENTIONS source)."""
        return self._memory_queries.fetch_candidate_provenance(node_uuids)

    def fetch_node_receipts(self, node_uuid: str) -> dict[str, object] | None:
        """Receipts for one node: its MENTIONS source episodes + SUPPORTED_BY evidence + ANCHORED_TO paths."""
        return self._memory_queries.fetch_node_receipts(node_uuid)

    def fetch_adjacency_pairs(
        self,
        candidate_uuids: list[str],
        context_uuids: list[str] | None = None,
        namespace: str | None = None,
    ) -> list[dict[str, object]]:
        """Find edges connecting candidates to each other or to context nodes."""
        return self._memory_queries.fetch_adjacency_pairs(
            candidate_uuids, context_uuids, namespace=namespace,
        )

    def touch_retrieved_nodes(self, node_uuids: list[str]) -> int:
        """Update last_accessed for retrieved nodes, return count touched."""
        return self._memory_queries.touch_retrieved_nodes(node_uuids)

    # -------------------------------------------------------------------------
    # Consolidation delegates → ConsolidationRepository
    # -------------------------------------------------------------------------

    def fetch_decay_candidates(
        self,
        freshness: str,
        *,
        min_days_since_accessed: float,
        max_edge_count: int,
        max_sharpness: float | None = None,
    ) -> list[dict[str, object]]:
        return self._consolidation.fetch_decay_candidates(
            freshness,
            min_days_since_accessed=min_days_since_accessed,
            max_edge_count=max_edge_count,
            max_sharpness=max_sharpness,
        )

    def compress_node(self, node_uuid: str, compressed_summary: str) -> bool:
        return self._consolidation.compress_node(node_uuid, compressed_summary)

    def fetch_node_freshness(self, node_uuids: list[str]) -> dict[str, str]:
        return self._consolidation.fetch_node_freshness(node_uuids)

    def complete_rehydration(
        self, node_uuid: str, updated_content: str | None = None
    ) -> bool:
        return self._consolidation.complete_rehydration(node_uuid, updated_content)

    def bridge_and_delete(self, node_uuid: str) -> dict[str, int]:
        return self._consolidation.bridge_and_delete(node_uuid)

    def fetch_memory_overview(self, namespace: str | None = None) -> dict[str, Any]:
        """Return high-level graph counts, optionally scoped to one silo (CF-33)."""
        return self._memory_queries.fetch_memory_overview(namespace)

    def fetch_recent_memories(
        self, limit: int = 10, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """Return the most recently accessed or created memory nodes."""
        return self._memory_queries.fetch_recent_memories(limit, namespace=namespace)

    def fetch_flagged_memories(
        self,
        limit: int = 10,
        workspace: str | None = None,
        *,
        namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return explicitly flagged memory nodes for bootstrap context reads."""
        return self._memory_queries.fetch_flagged_memories(
            limit, workspace=workspace, namespace=namespace
        )

    def fetch_flagged_memory_bootstrap_version(
        self,
        workspace: str | None = None,
        *,
        namespace: str | None = None,
    ) -> str:
        """Return a deterministic version fingerprint for the flagged-memory set."""
        return self._memory_queries.fetch_flagged_memory_bootstrap_version(
            workspace=workspace, namespace=namespace
        )

    def unflag_structural_nodes(self) -> int:
        """One-time cleanup: remove user_flagged from structural graph nodes."""
        return self._memory_queries.unflag_structural_nodes()

    def fetch_memory_by_uuid(
        self, node_uuid: str, *, namespace: str | None = None
    ) -> dict[str, Any] | None:
        """Return a single memory node by UUID, optionally restricted to one namespace."""
        return self._memory_queries.fetch_memory_by_uuid(
            node_uuid, namespace=namespace
        )

    def fetch_memories_by_scope(
        self, scope: str, limit: int = 10, *, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """Return memory nodes filtered by scope, optionally restricted to one namespace."""
        return self._memory_queries.fetch_memories_by_scope(
            scope, limit, namespace=namespace
        )

    def fetch_memories_by_type(
        self, memory_type: str, limit: int = 10, *, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """Return entity memories filtered by type, optionally restricted to one namespace."""
        return self._memory_queries.fetch_memories_by_type(
            memory_type, limit, namespace=namespace
        )

    def fetch_session_entities(
        self,
        session_id: str | None = None,
        max_age_hours: float = 0,
    ) -> list[dict[str, Any]]:
        return self._consolidation.fetch_session_entities(session_id, max_age_hours)

    def count_persistent_edges(self, node_uuid: str) -> int:
        return self._consolidation.count_persistent_edges(node_uuid)

    def promote_to_persistent(self, node_uuids: list[str]) -> int:
        return self._consolidation.promote_to_persistent(node_uuids)

    def delete_session_nodes(self, node_uuids: list[str]) -> int:
        return self._consolidation.delete_session_nodes(node_uuids)

    def set_demote_ttl(self, node_uuids: list[str], ttl_days: int) -> int:
        return self._consolidation.set_demote_ttl(node_uuids, ttl_days)

    def fetch_ttl_expired_session_uuids(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return self._consolidation.fetch_ttl_expired_session_uuids(session_id)

    def update_sharpness(self, node_uuid: str, sharpness: float) -> bool:
        return self._consolidation.update_sharpness(node_uuid, sharpness)

    def set_conflict(
        self,
        node_uuid_a: str,
        node_uuid_b: str,
        new_group_id: str,
        *,
        initial_status: str = "pending_llm_review",
    ) -> tuple[str, int]:
        return self._consolidation.set_conflict(
            node_uuid_a, node_uuid_b, new_group_id, initial_status=initial_status
        )

    def set_conflict_group_status(self, group_id: str, status: str) -> int:
        return self._consolidation.set_conflict_group_status(group_id, status)

    def requeue_conflicts_for_llm_review(
        self, *, from_status: str = "unresolved", limit: int = 200,
        namespace: str | None = None,
    ) -> int:
        return self._consolidation.requeue_conflicts_for_llm_review(
            from_status=from_status, limit=limit, namespace=namespace
        )

    # -------------------------------------------------------------------------
    # Episode reset / recovery delegates → EpisodeRepository
    # -------------------------------------------------------------------------

    def reset_stale_enriching_episodes(self, *, max_attempts: int) -> int:
        return self._episodes.reset_stale_enriching_episodes(max_attempts=max_attempts)

    def reset_orphaned_enriching_episodes(self, *, max_attempts: int) -> int:
        return self._episodes.reset_orphaned_enriching_episodes(
            max_attempts=max_attempts
        )

    def force_reset_failed_episode(self, episode_uuid: str) -> bool:
        return self._episodes.force_reset_failed_episode(episode_uuid)

    def force_release_episode_lease(
        self, episode_uuid: str, *, max_attempts: int
    ) -> bool:
        return self._episodes.force_release_episode_lease(
            episode_uuid, max_attempts=max_attempts
        )

    def release_worker_episode_leases(
        self, worker_id: str, *, max_attempts: int
    ) -> int:
        return self._episodes.release_worker_episode_leases(
            worker_id, max_attempts=max_attempts
        )

    def list_episode_processing(
        self,
        *,
        processing_states: list[str] | None = None,
        limit: int = 25,
        namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._episodes.list_episode_processing(
            processing_states=processing_states, limit=limit, namespace=namespace
        )

    def fetch_stale_enriching_episodes(
        self,
        *,
        include_missing_lease: bool = True,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self._episodes.fetch_stale_enriching_episodes(
            include_missing_lease=include_missing_lease, limit=limit
        )

    def update_episode_processing(
        self,
        episode_uuid: str,
        *,
        worker_id: str | None = None,
        stage: str | None = None,
        substage: str | None = None,
        progress: float | None = None,
        steps_completed: int | None = None,
        steps_total: int | None = None,
        llm_active_task: str | None = None,
        llm_active_kind: str | None = None,
        llm_active_model: str | None = None,
        llm_active_endpoint: str | None = None,
        clear_llm_active: bool = False,
        heartbeat: bool = True,
    ) -> bool:
        return self._episodes.update_episode_processing(
            episode_uuid,
            worker_id=worker_id,
            stage=stage,
            substage=substage,
            progress=progress,
            steps_completed=steps_completed,
            steps_total=steps_total,
            llm_active_task=llm_active_task,
            llm_active_kind=llm_active_kind,
            llm_active_model=llm_active_model,
            llm_active_endpoint=llm_active_endpoint,
            clear_llm_active=clear_llm_active,
            heartbeat=heartbeat,
        )

    def touch_episode_processing_heartbeat(
        self, episode_uuid: str, *, worker_id: str | None = None
    ) -> bool:
        return self._episodes.touch_episode_processing_heartbeat(
            episode_uuid, worker_id=worker_id
        )

    def increment_episode_llm_usage(
        self,
        episode_uuid: str,
        *,
        task_delta: int = 1,
        phase: str = "started",
        kind: str | None = None,
        model: str | None = None,
        endpoint: str | None = None,
        task: str | None = None,
        error: str | None = None,
    ) -> bool:
        return self._episodes.increment_episode_llm_usage(
            episode_uuid,
            task_delta=task_delta,
            phase=phase,
            kind=kind,
            model=model,
            endpoint=endpoint,
            task=task,
            error=error,
        )

    def reset_zero_extraction_episodes(self) -> int:
        return self._episodes.reset_zero_extraction_episodes()

    def count_pending_episodes(self, session_id: str | None = None) -> int:
        return self._episodes.count_pending_episodes(session_id)

    def cleanup_orphan_episodes(self, session_id: str | None = None) -> int:
        return self._episodes.cleanup_orphan_episodes(session_id)

    def increment_edge_weight(self, edge_uuid: str) -> bool:
        return self._consolidation.increment_edge_weight(edge_uuid)

    def increment_edge_weights(self, edge_uuids: list[str]) -> int:
        return self._consolidation.increment_edge_weights(edge_uuids)

    def update_edge_facts(self, updates: list[dict[str, str]]) -> int:
        return self._consolidation.update_edge_facts(updates)

    # -------------------------------------------------------------------------
    # Conflict governance delegates → ConsolidationRepository
    # -------------------------------------------------------------------------

    def list_conflict_groups(
        self,
        *,
        status: str | None = "unresolved",
        limit: int = 25,
        namespace: str | None = None,
        created_before: datetime | None = None,
        oldest_first: bool = False,
    ) -> list[dict[str, Any]]:
        return self._consolidation.list_conflict_groups(
            status=status,
            limit=limit,
            namespace=namespace,
            created_before=created_before,
            oldest_first=oldest_first,
        )

    def list_conflict_pairs(
        self,
        *,
        status: str | None = "unresolved",
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        return self._consolidation.list_conflict_pairs(status=status, limit=limit)

    def bridge_edges_for_node(self, node_uuid: str) -> int:
        return self._consolidation.bridge_edges_for_node(node_uuid)

    def bridge_edges_for_nodes(self, node_uuids: list[str]) -> int:
        return self._consolidation.bridge_edges_for_nodes(node_uuids)

    def resolve_conflict_group(
        self,
        conflict_group_id: str,
        action: str,
        *,
        keep_uuid: str | None = None,
        remove_uuid: str | None = None,
        resolution_status: str = "resolved",
        allow_promoted_removal: bool = False,
    ) -> dict[str, Any]:
        return self._consolidation.resolve_conflict_group(
            conflict_group_id,
            action,
            keep_uuid=keep_uuid,
            remove_uuid=remove_uuid,
            resolution_status=resolution_status,
            allow_promoted_removal=allow_promoted_removal,
        )

    # -------------------------------------------------------------------------
    # Structure graph delegates → StructureGraphWriter
    # -------------------------------------------------------------------------

    def write_project_structure(
        self,
        scan: Any,
        session_id: str,
        user_id: str,
    ) -> dict[str, int]:
        """Write a project's structure graph, subject to the migration fence.

        THE choke point, and that is why the fence sits here rather than at the four call sites:
        the REST scan path, the deprecated raw-payload path, the background symbol rescan and the
        unattended watcher all arrive through this one method. Guarding the callers instead would
        mean four places to keep in step, and the next writer added would silently miss it.
        """
        from menhir.infrastructure.project_identity_binding import binding_host, root_key_for
        from menhir.infrastructure.structure_write_fence import (
            IdentityClaim, admit_structure_writer, release_structure_writer,
        )

        # CF-257. The identity invariant belongs HERE, beside the fence, for the same reason the
        # fence does: this is the one method every structure writer funnels through. Settling
        # identity only in `scan_and_write_project` left the watcher and the deprecated raw path
        # writing id-less nodes -- and a NULL key does not violate a uniqueness constraint, so the
        # invariant eroded silently from zero to 1,816 nodes with both constraints live and no
        # error anywhere. Refusing at the choke point is what makes "every structure node carries
        # an id" a property of the system rather than of one call path.
        #
        # **A populated id is not an authorisation.** This check used to be exactly
        # `if not scan.project_id`, and that admits the stale-transfer race: X settles, Y
        # supersedes X, X's scan finishes minutes later and writes under an identity that no
        # longer owns the directory -- carrying the per-project stale prune into another
        # project's silo. So the id travels as a CLAIM (identity, directory, generation) and is
        # re-validated inside `admit_structure_writer`, in the same statement that registers the
        # writer and under a lock a concurrent transfer must wait for.
        if not getattr(scan, "project_id", None):
            raise ValueError(
                f"Refusing to write structure for {getattr(scan, 'name', '<unknown>')!r} with no "
                "structure_project_id. Callers must settle identity first "
                "(services.project_identity_service.settle_project_identity). An id-less node is "
                "invisible to the composite uniqueness constraint, so it erodes the invariant "
                "without failing."
            )

        # The directory comes from the SCAN, not from the claim. The claim must authorise the
        # directory this payload actually describes, so a settlement that bound some other root
        # fails here rather than being taken on trust.
        claim = IdentityClaim(
            project_id=str(scan.project_id),
            root_key=root_key_for(str(getattr(scan, "root_path", "") or "")),
            generation=int(getattr(scan, "identity_generation", 0) or 0),
            host=binding_host(),
        )
        handle = admit_structure_writer(
            self.neo4j, label=str(getattr(scan, "name", "") or ""), claim=claim
        )
        try:
            return self._structure.write_project(scan, session_id, user_id)
        finally:
            release_structure_writer(self.neo4j, handle)

    def write_document(
        self,
        file_path: str,
        content: str,
        *,
        project: str,
        structure_path: str,
        project_id: str,
        identity_generation: int,
        identity_root: str,
        session_id: str,
        user_id: str,
        document_type: str = "generic",
    ) -> None:
        """Write one document under the same durable identity fence as a project scan."""
        from menhir.infrastructure.project_identity_binding import binding_host, root_key_for
        from menhir.infrastructure.structure_write_fence import (
            IdentityClaim, admit_structure_writer, release_structure_writer,
        )

        if not project_id:
            raise ValueError(
                f"Refusing to write document structure for {project!r} with no "
                "structure_project_id. Callers must settle identity first."
            )
        if not identity_root:
            raise ValueError(
                f"Refusing to write document structure for {project!r} with no identity root."
            )
        if identity_generation is None:
            raise ValueError(
                f"Refusing to write document structure for {project!r} with no identity "
                "generation."
            )

        claim = IdentityClaim(
            project_id=str(project_id),
            root_key=root_key_for(str(identity_root)),
            generation=int(identity_generation),
            host=binding_host(),
        )
        handle = admit_structure_writer(self.neo4j, label=project, claim=claim)
        try:
            self._structure.write_document(
                file_path,
                content,
                project=project,
                structure_path=structure_path,
                structure_project_id=str(project_id),
                session_id=session_id,
                user_id=user_id,
                document_type=document_type,
            )
        finally:
            release_structure_writer(self.neo4j, handle)

    def get_scan_fingerprint(self, project_name: str) -> str | None:
        return self._structure.get_scan_fingerprint(project_name)

    def get_project_root_path(self, project_name: str) -> str | None:
        return self._structure.get_project_root_path(project_name)

    #: The structure query types this adapter will dispatch (CF-164).
    #:
    #: An ALLOWLIST rather than `getattr(self._structure, f"query_{query_type}")` on the caller's
    #: string. That form exposed every `query_*` method the repository happens to define -- 14 of
    #: them against 13 advertised -- so `contained_repos` and `linked_memories` were reachable and
    #: undocumented. `query_linked_memories` is CF-126's unscoped recall, which made that finding
    #: reachable through a type the boundary does not describe.
    #:
    #: The exposure is not the MCP tool, which dispatches literals through its own `if` chain. It
    #: is `POST /api/internal/backend/query_structure`: `query_structure` is in `_BACKEND_METHODS`
    #: (`api/routes_support.py:601`) and falls to the readonly remainder, and
    #: `backend_runtime_data_ops.query_structure` passes `query_type` straight through.
    #:
    #: Three advertised types are absent because they never reach here -- `projects`,
    #: `orphan_structure_projects` and `documents` are answered upstream in
    #: `backend_runtime_data_ops.query_structure` before this fallthrough.
    STRUCTURE_QUERY_TYPES: frozenset[str] = frozenset({
        "overview",
        "files",
        "imports",
        "tests",
        "endpoints",
        "dependencies",
        "cross_refs",
        "blast_radius",
        "affected_tests",
        "symbols",
        "context",
    })

    def query_structure(self, project: str, query_type: str, **kwargs: Any) -> Any:
        if query_type not in self.STRUCTURE_QUERY_TYPES:
            # Same message and type as before: an unknown type was already a ValueError, and a
            # now-refused-but-existing method must be indistinguishable from a typo, or the error
            # itself enumerates the private surface.
            raise ValueError(f"Unknown structure query type: {query_type}")
        method = getattr(self._structure, f"query_{query_type}", None)
        if method is None:
            raise ValueError(f"Unknown structure query type: {query_type}")
        return method(project, **kwargs)

    def query_documents(
        self, project: str, path_filter: str = "", document_type: str | None = None
    ) -> list[dict[str, str]]:
        """List document entities for a project."""
        return self._structure.query_documents(project, path_filter, document_type)

    def link_episode_to_documents(
        self,
        episode_uuid: str,
        entity_names: list[str],
        project: str,
        max_links: int = 5,
    ) -> int:
        """Link an episode to wiki/reference documents by name match."""
        return self._structure.link_episode_to_documents(
            episode_uuid, entity_names, project, max_links
        )

    def get_linked_documents(self, episode_uuids: list[str]) -> list[dict[str, str]]:
        """Get documents linked to episodes via RELATES_TO."""
        return self._structure.get_linked_documents(episode_uuids)

    def list_structure_projects(self) -> list[dict[str, str]]:
        return self._structure.list_projects()

    def list_orphan_structure_projects(self) -> list[dict[str, Any]]:
        return self._structure.list_orphan_structure_projects()

    # --- personal-memory consolidation (perception) ---
    # Prefer raw :Turn evidence (ADR 0001) when any user-authored Turn exists; otherwise fall back to
    # the legacy `user:`-prefixed Episodic path (benchmark fixtures). The switch is per-call so a box
    # that starts capturing Turns transitions with no restart.
    def list_dirty_namespaces(self, *, limit: int = 200) -> list[str]:
        if self._turn_evidence.evidence_exists():
            return self._turn_evidence.list_dirty_evidence_namespaces(limit=limit)
        return self._personal_memory.list_dirty_namespaces(limit=limit)

    def load_user_episodes(self, namespace: str, *, limit: int = 500) -> list[dict[str, Any]]:
        if self._turn_evidence.evidence_exists():
            return self._turn_evidence.load_user_evidence(namespace, limit=limit)
        return self._personal_memory.load_user_episodes(namespace, limit=limit)

    def mark_consolidated(self, namespace: str, *, at: str) -> None:
        self._personal_memory.mark_consolidated(namespace, at=at)

    def list_scalar_dirty_namespaces(
        self, *, perceiver_version: str, limit: int = 200
    ) -> list[str]:
        """Namespaces due for typed-scalar consolidation (ScalarStateView C.4.3) per the independent,
        version-stamped :ScalarConsolidationWatermark cursor — NOT the counter watermark. Dirty when
        never scalar-consolidated, consolidated by a different perceiver_version, or carrying episodes
        beyond the stored cursor.

        G14 bridge: prefers raw :TurnEvidence when any user-authored Turn exists (mirrors the counter
        path's per-call switch above), so the typed-scalar path discovers user input -- and grounds its
        assertions to the declarant foundation -- in a Turn-capturing production box; otherwise falls
        back to the legacy `user:`-prefixed Episodic path (benchmark fixtures)."""
        if self._turn_evidence.evidence_exists():
            return self._turn_evidence.list_scalar_dirty_evidence_namespaces(
                perceiver_version=perceiver_version, limit=limit)
        return self._personal_memory.list_scalar_dirty_namespaces(
            perceiver_version=perceiver_version, limit=limit)

    def load_next_scalar_batch(
        self, namespace: str, *, perceiver_version: str, limit: int = 500
    ) -> list[dict[str, Any]]:
        """The next bounded page of USER episodes AFTER the namespace's scalar cursor (C.4.3
        truncation-safe backfill). See PersonalMemoryRepository.load_next_scalar_batch. G14: reads
        :TurnEvidence (turn_id as the grounding anchor) when Turn evidence exists, else Episodic."""
        if self._turn_evidence.evidence_exists():
            return self._turn_evidence.load_next_scalar_evidence_batch(
                namespace, perceiver_version=perceiver_version, limit=limit)
        return self._personal_memory.load_next_scalar_batch(
            namespace, perceiver_version=perceiver_version, limit=limit)

    def advance_scalar_cursor(
        self, namespace: str, *, cursor_at: str, cursor_uuid: str,
        perceiver_version: str, at: str
    ) -> None:
        """Advance the namespace's scalar cursor to the last processed episode's monotonic
        work-discovery key `cursor_at` (C.4.3, NOT world-time). Called only after a batch actually
        ran, so a partial backfill resumes without stranding the tail."""
        self._personal_memory.advance_scalar_cursor(
            namespace, cursor_at=cursor_at, cursor_uuid=cursor_uuid,
            perceiver_version=perceiver_version, at=at)

    # --- event-consolidation cursor (Event History consolidation source) ---
    # These delegates read canonical :TurnEvidence ONLY — no Episodic fallback and no global
    # `evidence_exists` switch. They advance an independent :EventConsolidationWatermark keyed by the
    # namespace string in `group_id`, so event consolidation never disturbs the scalar/counter cursors.
    def list_event_dirty_evidence_namespaces(
        self, *, perceiver_version: str, limit: int = 200,
    ) -> list[str]:
        return self._turn_evidence.list_event_dirty_evidence_namespaces(
            perceiver_version=perceiver_version, limit=limit)

    def load_next_event_evidence_batch(
        self, namespace: str, *, perceiver_version: str, limit: int = 500,
    ) -> list[dict[str, Any]]:
        return self._turn_evidence.load_next_event_evidence_batch(
            namespace, perceiver_version=perceiver_version, limit=limit)

    def advance_event_cursor(
        self, namespace: str, *, cursor_at: str, cursor_uuid: str,
        perceiver_version: str, at: str,
    ) -> None:
        self._turn_evidence.advance_event_cursor(
            namespace, cursor_at=cursor_at, cursor_uuid=cursor_uuid,
            perceiver_version=perceiver_version, at=at)

    # --- selective :TurnEvidence capture (ADR 0001) ---
    def record_turn_evidence(self, **kwargs: Any) -> dict[str, Any]:
        return self._turn_evidence.record_turn_evidence(**kwargs)

    def fetch_turn_evidence(self, turn_id: str) -> dict[str, Any] | None:
        """Fetch one :TurnEvidence node by its turn_id for admission gating."""
        return self._turn_evidence.fetch_by_uuid(turn_id)

    def load_preceding_turn_evidence_context(
        self,
        turn_id: str,
        *,
        namespace: str,
        limit: int = 2,
    ) -> list[dict[str, Any]]:
        """Load adjacent dialogue turns for a bounded relationless-extraction repair.

        ``namespace`` is the CALLER's namespace and is required: `turn_id` is caller-supplied,
        so without it a foreign turn's text reaches this namespace's extraction (CF-236).
        """
        return self._turn_evidence.load_preceding_context(
            turn_id, namespace=namespace, limit=limit
        )

    def turn_evidence_stats(self) -> dict[str, Any]:
        return self._turn_evidence.evidence_stats()

    def count_turn_evidence(self, namespace: str) -> int:
        return self._turn_evidence.count_namespace(namespace)

    def purge_turn_evidence(self, namespace: str) -> int:
        """Delete `:TurnEvidence` for a namespace (not covered by group_id-keyed delete_namespace)."""
        return self._turn_evidence.purge_namespace(namespace)

    # --- Hook Center tool/file events (v0): deterministic dirty/stale marking ---
    def record_file_event(self, **kwargs: Any) -> dict[str, Any]:
        return self._tool_events.record_file_event(**kwargs)

    def list_dirty_files(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self._tool_events.list_dirty_files(**kwargs)

    def stale_anchored_memories(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self._tool_events.stale_anchored_memories(**kwargs)

    def clear_file_dirty(self, **kwargs: Any) -> int:
        return self._tool_events.clear_file_dirty(**kwargs)

    def tool_event_dirty_stats(self, **kwargs: Any) -> dict[str, Any]:
        return self._tool_events.dirty_stats(**kwargs)

    def record_stale_anchor_verification(self, **kwargs: Any) -> dict[str, Any]:
        return self._tool_events.record_stale_anchor_verification(**kwargs)

    def list_stale_anchor_verifications(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self._tool_events.list_stale_anchor_verifications(**kwargs)

    def latest_stale_anchor_verifications(self, **kwargs: Any) -> dict[str, dict[str, Any]]:
        return self._tool_events.latest_stale_anchor_verifications(**kwargs)

    # -------------------------------------------------------------------------
    # Structural anchoring delegates
    # -------------------------------------------------------------------------

    def anchor_semantic_to_structural(
        self,
        semantic_uuids: list[str],
        candidate_paths: list[str],
        *,
        anchor_source: str = "narrative_path",
        weight: float = 1.0,
        project_filter: str | None = None,
    ) -> int:
        """Resolve file paths and create ANCHORED_TO edges from semantic entities."""
        from menhir.infrastructure.structural_anchoring import (
            resolve_structural_entities,
            create_anchor_edges,
        )

        resolved = resolve_structural_entities(
            self.neo4j, candidate_paths, project_filter
        )
        if not resolved:
            return 0
        structural_uuids = [r["uuid"] for r in resolved]
        return create_anchor_edges(
            self.neo4j,
            semantic_uuids,
            structural_uuids,
            anchor_source=anchor_source,
            weight=weight,
        )

    def find_cross_linked_semantic_entities(
        self, structural_uuids: list[str]
    ) -> list[str]:
        """Find semantic entity UUIDs anchored to the given structural entities."""
        from menhir.infrastructure.structural_anchoring import (
            find_cross_linked_semantic_entities,
        )

        return find_cross_linked_semantic_entities(self.neo4j, structural_uuids)

    # -------------------------------------------------------------------------
    # TODO delegates → TodoRepository
    # -------------------------------------------------------------------------

    def create_todo(
        self,
        *,
        content: str,
        code_ref: str | None = None,
        priority: str = "normal",
        source: str = "claude-code",
        episode_uuid: str | None = None,
        structure_project: str | None = None,
        due_date: str | None = None,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        return self._todos.create_todo(
            content=content,
            code_ref=code_ref,
            priority=priority,
            source=source,
            episode_uuid=episode_uuid,
            structure_project=structure_project,
            due_date=due_date,
            namespace=namespace,
        )

    def list_todos(
        self, *, status: str = "open", limit: int = 50, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        return self._todos.list_todos(status=status, limit=limit, namespace=namespace)

    def get_todo(self, uuid: str, *, namespace: str | None = None) -> dict[str, Any] | None:
        return self._todos.get_todo(uuid, namespace=namespace)

    # -------------------------------------------------------------------------
    # WorkArtifact delegates → WorkArtifactRepository
    # -------------------------------------------------------------------------

    def get_artifact(
        self, artifact_uuid: str, *, namespace: str | None = None
    ) -> dict[str, Any] | None:
        return self._work_artifacts.get_artifact(artifact_uuid, namespace=namespace)

    def list_artifacts(
        self,
        *,
        artifact_type: str | None = None,
        status: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return self._work_artifacts.list_artifacts(
            artifact_type=artifact_type, status=status, namespace=namespace, limit=limit
        )

    def list_artifact_questions(
        self,
        *,
        artifact_uuid: str | None = None,
        status: str | None = None,
        namespace: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return self._work_artifacts.open_questions(
            artifact_uuid=artifact_uuid, status=status, namespace=namespace, limit=limit
        )

    def get_artifact_relationships(self, artifact_uuid: str) -> dict[str, list[dict[str, Any]]]:
        return self._work_artifacts.artifact_relationships(artifact_uuid)

    def link_artifacts(
        self, source_uuid: str, target_uuid: str, relation: str
    ) -> dict[str, Any]:
        return self._work_artifacts.link_artifacts(source_uuid, target_uuid, relation)

    def supersede_artifact(self, new_uuid: str, old_uuid: str) -> dict[str, Any]:
        return self._work_artifacts.supersede_artifact(new_uuid, old_uuid)

    def transition_artifact_status(
        self, artifact_uuid: str, to_status: str, *, namespace: str | None = None
    ) -> dict[str, Any]:
        return self._work_artifacts.transition_status(
            artifact_uuid, to_status, namespace=namespace
        )

    def fetch_artifact_corpus_audit(
        self,
        *,
        repo_path: str,
        repository: str,
        from_commit: str | None = None,
        conflict_limit: int = 25,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        """Read-only corpus parity summary, with a bounded conflict list.

        The full ledger stays in CLI JSON output. A caller over MCP wants to know
        whether the corpus is in sync and what is wrong, not to receive several
        hundred action records through a chat transport.
        """
        from menhir.services.artifact_reconciliation_service import (
            ArtifactReconciliationService,
        )

        service = ArtifactReconciliationService(self._work_artifacts)
        report = service.audit(
            repo_path, repository=repository, from_commit=from_commit,
            namespace=namespace,
        )
        limit = max(1, min(int(conflict_limit), 100))
        conflicts = report.conflicts
        return {
            "repository": report.repository,
            "observed_commit": report.observed_commit,
            "cursor_commit": report.cursor_commit,
            "evidence_from_commit": report.evidence_from_commit,
            "evidence_base_valid": report.evidence_base_valid,
            "plan_digest": report.plan_digest,
            "counts": dict(report.counts),
            "conflicts": [c.as_dict() for c in conflicts[:limit]],
            "conflicts_truncated": max(0, len(conflicts) - limit),
            "contradictions": [c.as_dict() for c in report.contradictions[:limit]],
        }

    def relocate_artifact_source(
        self,
        *,
        artifact_uuid: str,
        old_path: str,
        new_path: str,
        repository: str | None = None,
        medium: str = "markdown",
        expected_old_integrity: str = "",
        observed_integrity: str = "",
    ) -> dict[str, Any]:
        """Move one source's locator, checked against the artifact that owns it.

        The artifact UUID is not decoration: it is the caller stating which
        record they believe is at the old path. If the path belongs to a
        different artifact, that disagreement is the whole finding, and applying
        the move anyway would silently transplant one document's history onto
        another.
        """
        from datetime import datetime, timezone

        from menhir.domain.artifact_reconciliation import (
            MatchBasis,
            SourceObservation,
        )

        if not (repository or "").strip():
            # The locator key is repository-scoped, so an empty repository can
            # only ever miss. Saying "not found" would send the caller hunting
            # for the path instead of supplying the argument they omitted.
            return {"applied": False, "reason": "repository_required"}

        source_uuid, reason = self._work_artifacts._source_uuid_at_locator(  # noqa: SLF001
            repository, medium, old_path
        )
        if source_uuid is None:
            return {"applied": False, "reason": reason}

        owner = self._work_artifacts.neo4j.execute(
            """
            MATCH (a:WorkArtifact)-[:EMBODIED_IN]->(s:ArtifactSource {source_uuid: $source_uuid})
            RETURN a.artifact_uuid AS artifact_uuid
            """,
            {"source_uuid": source_uuid},
        )
        owner_uuid = owner[0].get("artifact_uuid") if owner else None
        if owner_uuid != artifact_uuid:
            return {
                "applied": False,
                "reason": "uuid_locator_disagreement",
                "locator_owner": owner_uuid,
            }

        observation = SourceObservation(
            integrity=observed_integrity or None,
            observed_at=datetime.now(timezone.utc).isoformat(),
            basis=MatchBasis.DECLARED_UUID,
        )
        return self._work_artifacts.relocate_artifact_source(
            source_uuid=source_uuid,
            old_locator={"repository": repository, "path": old_path, "medium": medium},
            new_locator={"repository": repository, "path": new_path, "medium": medium},
            observation=observation,
            expected_integrity=expected_old_integrity or None,
        )

    def reconcile_file_event_source(
        self,
        *,
        path: str,
        operation: str,
        old_path: str | None = None,
        repository: str | None = None,
        after_hash: str | None = None,
        git_commit: str | None = None,
    ) -> dict[str, Any]:
        """Best-effort source reconciliation for one observed file event.

        Returns an outcome rather than raising. Structural dirty marking already
        happened by the time this runs, and a reconciliation refusal must never
        undo it or block the coding tool that reported the change.
        """
        from datetime import datetime, timezone

        from menhir.domain.artifact_reconciliation import (
            MatchBasis,
            SourceObservation,
            medium_for_path,
            route_for_path,
        )

        op = (operation or "").strip().lower()
        route = route_for_path(path)
        medium = medium_for_path(path)
        if route is None or medium is None:
            return {"attempted": False, "reason": "path_is_not_corpus_material"}

        observation = SourceObservation(
            integrity=after_hash or None,
            lane=route.lane,
            observed_commit=git_commit,
            observed_at=datetime.now(timezone.utc).isoformat(),
            basis=MatchBasis.GIT_RENAME if op == "rename" else MatchBasis.EXACT_LOCATOR,
        )
        try:
            if op == "rename" and old_path:
                result = self._work_artifacts.relocate_artifact_source_by_locator(
                    repository=repository or "",
                    medium=medium,
                    old_path=old_path,
                    new_path=path,
                    observation=observation,
                )
            elif op in ("edit", "write"):
                result = self._work_artifacts.refresh_artifact_source_by_locator(
                    repository=repository or "",
                    medium=medium,
                    path=path,
                    observation=observation,
                )
            else:
                # `create` carries no document metadata, so registering from the
                # event alone would mint an identity from a filename. The next
                # audit registers it with the record actually read.
                return {"attempted": False, "reason": f"operation_not_reconciled:{op}"}
        except Exception as exc:  # noqa: BLE001 - fail open, never block the hook
            return {"attempted": True, "applied": False, "reason": f"error:{type(exc).__name__}"}
        return {"attempted": True, **result}

    def supersede_todo(self, old_uuid: str, new_uuid: str) -> dict[str, Any]:
        return self._todos.supersede_todo(old_uuid, new_uuid)

    def resolve_todo(self, todo_uuid: str, memory_uuid: str) -> dict[str, Any]:
        return self._todos.resolve_todo(todo_uuid, memory_uuid)

    def reopen_todo(self, todo_uuid: str, memory_uuid: str) -> dict[str, Any]:
        return self._todos.reopen_todo(todo_uuid, memory_uuid)

    def link_memory_to_todo(
        self, memory_uuid: str, todo_uuid: str, relation: str
    ) -> dict[str, Any]:
        return self._todos.link_memory_to_todo(memory_uuid, todo_uuid, relation)

    def close_todo(self, uuid: str) -> bool:
        return self._todos.close_todo(uuid)

    def delete_todo(self, uuid: str) -> bool:
        return self._todos.delete_todo(uuid)

    def close_stale_todos(
        self, *, older_than_days: int = 60, dry_run: bool = True, namespace: str | None = None
    ) -> dict[str, Any]:
        return self._todos.close_stale_todos(
            older_than_days=older_than_days, dry_run=dry_run, namespace=namespace
        )

    def list_todos_matching_query(
        self, query: str, *, limit: int = 3
    ) -> list[dict[str, Any]]:
        return self._todos.search_by_query(query, limit=limit)

    # -------------------------------------------------------------------------
    # TEMPORAL delegates → TemporalRepository
    # -------------------------------------------------------------------------

    def create_temporal(
        self,
        *,
        content: str,
        target_date: str,
        source: str = "claude-code",
        name: str | None = None,
        flagged: bool = False,
        bootstrap_scope: str | None = None,
        namespace: str | None = None,
        turn_evidence_uuid: str | None = None,
    ) -> dict[str, Any]:
        return self._temporal.create_temporal(
            content=content,
            target_date=target_date,
            source=source,
            name=name,
            user_flagged=flagged,
            bootstrap_scope=bootstrap_scope,
            namespace=namespace,
            turn_evidence_uuid=turn_evidence_uuid,
            turn_evidence_repo=self._turn_evidence,
            audit_recorder=self.record_admission_audit,
        )

    def list_temporal_in_window(
        self, *, window_days: int = 30, namespace: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return self._temporal.list_in_window(
            window_days=window_days, namespace=namespace, limit=limit
        )

    def complete_temporal(self, uuid: str) -> bool:
        return self._temporal.complete_temporal(uuid)

    # -------------------------------------------------------------------------
    # CANDIDATE delegates -> CandidateRepository
    # -------------------------------------------------------------------------

    def create_candidate(
        self,
        *,
        content: str,
        source: str,
        cluster_id: str,
        label: str,
        kind: str = "memory",
        candidate_type: str = "other",
        type: str = "SEMANTIC",
        evidence_strength: str = "REPEATED",
        distinct_sessions: int = 0,
        first_seen: str | None = None,
        last_seen: str | None = None,
        notes: list[str] | None = None,
        source_confidence: float = 0.5,
        namespace: str | None = None,
    ) -> dict[str, Any]:
        return self._candidates.create_candidate(
            content=content,
            source=source,
            cluster_id=cluster_id,
            label=label,
            kind=kind,
            candidate_type=candidate_type,
            type=type,
            evidence_strength=evidence_strength,
            distinct_sessions=distinct_sessions,
            first_seen=first_seen,
            last_seen=last_seen,
            notes=notes,
            source_confidence=source_confidence,
            namespace=namespace,
        )

    def list_candidates(self, *, source: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self._candidates.list_candidates(source=source, limit=limit)

    def fetch_candidate(self, uuid: str) -> dict[str, Any] | None:
        return self._candidates.fetch_candidate(uuid)

    def promote_candidate(self, uuid: str) -> bool:
        return self._candidates.promote_candidate(uuid)

    def reject_candidate(self, uuid: str) -> bool:
        return self._candidates.reject_candidate(uuid)

    # -------------------------------------------------------------------------
    # L4 ARTIFACT delegates → ArtifactRepository
    # -------------------------------------------------------------------------

    def create_artifact(
        self,
        *,
        artifact_id: str,
        artifact_type: str,
        summary: str,
        source: str,
        status: str,
        body: str = "",
        evidence: list[dict[str, Any]] | None = None,
        anchors: list[str] | None = None,
        source_confidence: float = 0.5,
    ) -> dict[str, Any]:
        return self._artifacts.create_artifact(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            summary=summary,
            source=source,
            status=status,
            body=body,
            evidence=evidence,
            anchors=anchors,
            source_confidence=source_confidence,
        )

    def promote_artifact(self, artifact_id: str, *, trusted_confidence: float = 0.9) -> bool:
        return self._artifacts.promote_artifact(artifact_id, trusted_confidence=trusted_confidence)

    def supersede_l4_artifact(self, old_id: str, new_id: str) -> bool:
        return self._artifacts.supersede_artifact(old_id, new_id)

    def find_artifacts(
        self, *, tokens: list[str] | None = None, anchors: list[str] | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        return self._artifacts.find_artifacts(tokens=tokens, anchors=anchors, limit=limit)

    def fetch_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        return self._artifacts.fetch_artifact(artifact_id)

    # -------------------------------------------------------------------------
    # CORRELATION delegates → CorrelationRepository
    # -------------------------------------------------------------------------

    def create_related_to_edge(
        self,
        source_uuid: str,
        target_uuid: str,
        *,
        similarity: float,
        source: str = "correlation-detected",
    ) -> bool:
        return self._correlation.create_related_to_edge(
            source_uuid, target_uuid, similarity=similarity, source=source,
        )

    # -------------------------------------------------------------------------
    # VIEW delegates → ViewRepository (one supersedable View shape, many kinds)
    #   kind='counter' (QuantState): record/fetch/list/history_counter
    #   kind='timeline'            : record/fetch_timeline
    # -------------------------------------------------------------------------

    def record_counter(self, **kwargs: Any) -> dict[str, Any]:
        return self._views.record_counter(**kwargs)

    def fetch_counter(self, *, subject: str, counter: str, namespace: str | None = None
                      ) -> dict[str, Any] | None:
        return self._views.fetch_counter(subject=subject, counter=counter, namespace=namespace)

    def list_counters(self, *, namespace: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self._views.list_counters(namespace=namespace, limit=limit)

    def counter_history(self, *, subject: str, counter: str, namespace: str | None = None
                        ) -> list[dict[str, Any]]:
        return self._views.history(subject=subject, counter=counter, namespace=namespace)

    def record_scalar_state(self, **kwargs: Any) -> dict[str, Any]:
        return self._views.record_scalar_state(**kwargs)

    def fetch_scalar_state(self, **kwargs: Any) -> dict[str, Any] | None:
        return self._views.fetch_scalar_state(**kwargs)

    def fetch_current_scalar_view_for_slot(self, **kwargs: Any) -> dict[str, Any] | None:
        """Slot-keyed current scalar_state View for deterministic recall injection (Phase 4a.4)."""
        return self._views.fetch_current_scalar_view_for_slot(**kwargs)

    def fetch_scalar_authority_contributors(self, **kwargs: Any) -> dict[str, Any]:
        """Bounded labeled provenance for the structured authority layer (Phase 4c/7.J)."""
        return self._views.fetch_scalar_authority_contributors(**kwargs)

    def scalar_view_has_user_foundation(self, **kwargs: Any) -> bool:
        """True if the View's head traces to a declarant='user' :TurnEvidence foundation (G14/10.G)."""
        return self._views.scalar_view_has_user_foundation(**kwargs)

    def draw_scalar_state_provenance_edges(self, **kwargs: Any) -> dict[str, int]:
        """Draw a scalar_state View's CURRENT_ANCHOR/CONTRIBUTED_TO/SUPERSEDED_ANCHOR edges (Phase 3).

        REQUIRED: `scalar_state_service()` injects THIS adapter as the View sink, and
        ScalarStateService.rebuild_scalar_state only draws edges when
        `hasattr(self._views, "draw_scalar_state_provenance_edges")`. Without this passthrough the guard
        is False and the edges are SILENTLY skipped in the production consolidation path (they were only
        ever drawn by tests that injected a ViewRepository directly), so a View can never trace to its
        anchor and the G14/10.G foundation gate can never let it lead."""
        return self._views.draw_scalar_state_provenance_edges(**kwargs)

    def assertions_have_user_foundation(self, **kwargs: Any) -> bool:
        """True if any given assertion has a declarant='user' :TurnEvidence FOUNDS (G13 expiry basis)."""
        return self._views.assertions_have_user_foundation(**kwargs)

    def list_scalar_state_views(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self._views.list_scalar_state_views(**kwargs)

    def retire_scalar_state(self, **kwargs: Any) -> bool:
        return self._views.retire_scalar_state(**kwargs)

    def retire_counters_superseded_by_scalar(self, *, namespace: str) -> int:
        return self._views.retire_counters_superseded_by_scalar(namespace=namespace)

    # --- scalar_history (advisory ordered assertion history per slot) ---

    def record_scalar_history(self, **kwargs: Any) -> dict[str, Any]:
        return self._views.record_scalar_history(**kwargs)

    def fetch_scalar_history(self, **kwargs: Any) -> dict[str, Any] | None:
        return self._views.fetch_scalar_history(**kwargs)

    def list_scalar_history_views(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self._views.list_scalar_history_views(**kwargs)

    def list_scalar_history_views_for_namespace(
        self, **kwargs: Any
    ) -> list[dict[str, Any]]:
        return self._views.list_scalar_history_views_for_namespace(**kwargs)

    def retire_scalar_history(self, **kwargs: Any) -> bool:
        return self._views.retire_scalar_history(**kwargs)

    def draw_scalar_history_entries(self, **kwargs: Any) -> dict[str, int]:
        return self._views.draw_scalar_history_entries(**kwargs)

    def list_scalar_history_entries(self, **kwargs: Any) -> dict[str, Any]:
        return self._views.list_scalar_history_entries(**kwargs)

    def record_timeline(self, **kwargs: Any) -> dict[str, Any]:
        return self._views.record_timeline(**kwargs)

    def fetch_timeline(self, *, subject: str, namespace: str | None = None
                       ) -> dict[str, Any] | None:
        return self._views.fetch_timeline(subject=subject, namespace=namespace)

    def list_views(self, *, kind: str | None = None, namespace: str | None = None,
                   limit: int = 100) -> list[dict[str, Any]]:
        return self._views.list_views(kind=kind, namespace=namespace, limit=limit)

    # --- event-lane timeline (Event History Phase 2B.2) ---

    def record_event_timeline(self, **kwargs: Any) -> dict[str, Any]:
        """Upsert the entity-anchored, predicate/domain event-lane timeline View."""
        return self._views.record_event_timeline(**kwargs)

    def fetch_event_timeline(self, **kwargs: Any) -> dict[str, Any] | None:
        """Current event-lane timeline View for (subject_uuid, predicate[, domain])."""
        return self._views.fetch_event_timeline(**kwargs)

    def list_event_timeline_views(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Current event-lane timeline Views for one resolved entity."""
        return self._views.list_event_timeline_views(**kwargs)

    def retire_event_timeline(self, **kwargs: Any) -> bool:
        """Expire a current event-lane timeline View with no replacement."""
        return self._views.retire_event_timeline(**kwargs)

    def draw_event_timeline_entries(self, **kwargs: Any) -> dict[str, int]:
        """Atomically rewrite a View's EVENT_HISTORY_ENTRY edges to an exact ordered assertion set."""
        return self._views.draw_event_timeline_entries(**kwargs)

    def list_event_timeline_entries(self, **kwargs: Any) -> dict[str, Any]:
        """Paginated EVENT_HISTORY_ENTRY contributors for one event-lane timeline View."""
        return self._views.list_event_timeline_entries(**kwargs)

    # --- Metric class (operator-only instrumentation; :Metric label, Metric plan A3) ---

    def record_metric(self, **kwargs: Any) -> dict[str, Any]:
        return self._views.record_metric(**kwargs)

    def fetch_metric(self, *, subject: str, counter: str, namespace: str | None = None
                     ) -> dict[str, Any] | None:
        return self._views.fetch_metric(subject=subject, counter=counter, namespace=namespace)

    def fetch_metric_state(self, *, view_key: str) -> dict[str, Any] | None:
        return self._views.fetch_metric_state(view_key=view_key)

    def metric_history(self, *, subject: str, counter: str, namespace: str | None = None
                       ) -> list[dict[str, Any]]:
        return self._views.metric_history(subject=subject, counter=counter, namespace=namespace)

    def list_metrics(self, *, namespace: str | None = None, source: str | None = None,
                     current_only: bool = True, limit: int = 100) -> list[dict[str, Any]]:
        return self._views.list_metrics(
            namespace=namespace, source=source, current_only=current_only, limit=limit
        )

    def record_admission_audit(
        self, *, subject: str, namespace: str | None = None,
        requested_source: str = "", effective_source: str = "",
        granted: bool = False, turn_evidence_uuid: str | None = None,
        reason: str = "", source: str = "admission_gate", source_confidence: float = 0.6,
    ) -> dict[str, Any]:
        """Record an admission verdict audit row via View storage."""
        return self._views.record(
            "admission_audit",
            subject=subject,
            namespace=namespace,
            source=source,
            source_confidence=source_confidence,
            requested_source=requested_source,
            effective_source=effective_source,
            granted=granted,
            turn_evidence_uuid=turn_evidence_uuid,
            reason=reason,
        )

    def merge_entity(
        self,
        survivor_uuid: str,
        absorbed_uuid: str,
        *,
        similarity: float,
        operation_id: str | None = None,
    ) -> dict[str, int]:
        return self._correlation.merge_entity(
            survivor_uuid, absorbed_uuid, similarity=similarity, operation_id=operation_id,
        )

    def correlation_exists(self, uuid_a: str, uuid_b: str) -> bool:
        return self._correlation.correlation_exists(uuid_a, uuid_b)

    def fetch_entity_merge_metadata(self, uuids: list[str]) -> list[dict[str, Any]]:
        return self._correlation.fetch_entity_merge_metadata(uuids)

    def check_ineligible_node_veto(self, survivor_uuid: str, absorbed_uuid: str) -> bool:
        return self._correlation.check_ineligible_node_veto(survivor_uuid, absorbed_uuid)

    def evaluate_merge_eligibility(self, survivor_uuid: str, absorbed_uuid: str) -> Any:
        return self._correlation.evaluate_merge_eligibility(survivor_uuid, absorbed_uuid)

    def capture_merge_snapshot(
        self, survivor_uuid: str, absorbed_uuid: str, *, similarity: float | None = None
    ) -> dict[str, Any]:
        return self._correlation.capture_merge_snapshot(
            survivor_uuid, absorbed_uuid, similarity=similarity
        )

    def fetch_merge_state(self, survivor_uuid: str, absorbed_uuid: str) -> dict[str, Any]:
        return self._correlation.fetch_merge_state(survivor_uuid, absorbed_uuid)

    def fetch_survivor_properties(self, survivor_uuid: str) -> dict[str, Any] | None:
        return self._correlation.fetch_survivor_properties(survivor_uuid)

    def peers_exist(self, uuids: list[str]) -> set[str]:
        return self._correlation.peers_exist(uuids)

    def restore_merge_snapshot(self, **kwargs: Any) -> dict[str, Any]:
        return self._correlation.restore_merge_snapshot(**kwargs)

    def capture_node_state(self, uuid: str) -> dict[str, Any] | None:
        return self._correlation.capture_node_state(uuid)

    def delete_entities_returning_uuids(
        self, node_uuids: list[str], *, require_scope: str | None = None
    ) -> list[str]:
        return self._consolidation.delete_entities_returning_uuids(
            node_uuids, require_scope=require_scope
        )

    def newly_unreferenced_evidence(self, node_uuids: list[str]) -> list[str]:
        return self._consolidation.newly_unreferenced_evidence(node_uuids)

    def check_co_mention_veto(self, uuid_a: str, uuid_b: str) -> bool:
        return self._correlation.check_co_mention_veto(uuid_a, uuid_b)

    def check_anchor_project_veto(self, uuid_a: str, uuid_b: str) -> bool:
        return self._correlation.check_anchor_project_veto(uuid_a, uuid_b)
