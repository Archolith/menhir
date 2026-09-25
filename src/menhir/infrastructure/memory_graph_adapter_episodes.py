"""Episode lifecycle delegates for the memory graph adapter.

Methods moved verbatim from ``memory_graph_adapter.py`` (facade split); this
mixin is composed into :class:`~menhir.infrastructure.memory_graph_adapter.MemoryGraphAdapter`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from menhir.domain.self_identity import self_uuid_for_namespace
from menhir.infrastructure.episode_lifecycle import TRANSIENT_RETRY_CAP
from menhir.infrastructure.episode_repository import PolicyStampResult


class MemoryGraphEpisodesMixin:
    """Episode lifecycle delegates (mixin for MemoryGraphAdapter)."""

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

    def find_pending_evidence_projection_uuid(
        self, *, turn_evidence_uuid: str, namespace: str | None = None
    ) -> str | None:
        """Find a durable pending projection so an admission retry can re-enqueue it."""
        return self._episodes.find_pending_evidence_projection_uuid(
            turn_evidence_uuid=turn_evidence_uuid,
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
        self,
        episode_uuid: str,
        error: str,
        *,
        worker_id: str | None = None,
        transient_requeue: bool = False,
        claim_started_at: object | None = None,
    ) -> bool:
        return self._episodes.mark_episode_failed(
            episode_uuid,
            error,
            worker_id=worker_id,
            transient_requeue=transient_requeue,
            claim_started_at=claim_started_at,
        )

    def mark_episode_pending(
        self,
        episode_uuid: str,
        *,
        retry_after_s: float = 0.0,
        worker_id: str | None = None,
        transient_requeue: bool = False,
        claim_started_at: object | None = None,
    ) -> bool:
        return self._episodes.mark_episode_pending(
            episode_uuid,
            retry_after_s=retry_after_s,
            worker_id=worker_id,
            transient_requeue=transient_requeue,
            claim_started_at=claim_started_at,
        )

    def fail_transient_exhausted_pending_episodes(
        self, *, transient_max: int = TRANSIENT_RETRY_CAP
    ) -> int:
        return self._episodes.fail_transient_exhausted_pending_episodes(
            transient_max=transient_max
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
        # Derive the target uuid, never MERGE it. `ensure_self_entity` writes -- calling it here
        # would make a census or pre-migration inventory mutate the graph it is inspecting, and
        # the plan requires discovery to be read-only and separable from consolidation.
        return self._episodes.detect_self_forks(
            namespace=namespace,
            self_uuid=self_uuid_for_namespace(namespace),
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

    def record_retention_sources(
        self,
        *,
        source_episode_uuid: str,
        entity_uuids: list[str],
        namespace: str | None = None,
    ) -> int:
        return self._episodes.record_retention_sources(
            source_episode_uuid=source_episode_uuid,
            entity_uuids=entity_uuids,
            namespace=namespace,
        )
