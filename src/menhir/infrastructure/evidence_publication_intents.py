"""Durable publication intent protocol for local Graphiti episode writes.

Graphiti cannot accept Menhir's queued episode UUID as the remote episode UUID.  A timeout can
therefore hide a committed remote episode from the caller.  This repository records the intent
before dispatch, fences publication by namespace generation, and only makes returned evidence
eligible for View admission after its exact tenant-bound identity is unique and untombstoned.

The reconciler API in this module is deliberately passive.  Nothing registers it with a scheduler;
an operator or a later, separately activated bootstrap hook must call it.
"""

from __future__ import annotations

from datetime import datetime
import uuid as uuidlib

from menhir.domain.namespace import (
    normalize_namespace,
    namespace_to_group_id,
    tenant_scope_params,
)
from menhir.infrastructure.evidence_publication_intents_cypher import (
    BEGIN_INTENT_CYPHER,
    CLAIM_PENDING_CYPHER,
    DISCOVER_EXACT_REMOTE_UUIDS_CYPHER,
    FETCH_EPISODE_ARTIFACT_CYPHER,
    FINALIZE_REMOTE_OUTCOME_CYPHER,
    GET_INTENT_CYPHER,
    RELEASE_PENDING_CYPHER,
)
from menhir.infrastructure.evidence_publication_intents_models import (
    CLAIMED,
    FINALIZED,
    PENDING,
    QUARANTINED,
    EpisodeArtifact,
    EvidenceTombstoneDigestService,
    GraphitiArtifactManifest,
    GraphitiArtifactManifestService,
    PublicationActivationBlocked,
    PublicationActivationStatus,
    PublicationDispatchSuppressed,
    PublicationIntent,
    PublicationIntentError,
    PublicationTransition,
    TombstoneProbe,
    _KEY_ID_PATTERN,
    _OPAQUE_DIGEST_PATTERN,
    _intent_from_row,
    _now,
    publication_intent_key,
    publication_operation_id,
)
from menhir.infrastructure.neo4j import Neo4jRepository

__all__ = [
    "CLAIMED",
    "FINALIZED",
    "PENDING",
    "QUARANTINED",
    "EpisodeArtifact",
    "EvidencePublicationIntentRepository",
    "EvidenceTombstoneDigestService",
    "GraphitiArtifactManifest",
    "GraphitiArtifactManifestService",
    "PublicationActivationBlocked",
    "PublicationActivationStatus",
    "PublicationDispatchSuppressed",
    "PublicationIntent",
    "PublicationIntentError",
    "PublicationTransition",
    "TombstoneProbe",
    "publication_intent_key",
    "publication_operation_id",
]


class EvidencePublicationIntentRepository:
    """Neo4j persistence boundary for Graphiti publication intent transitions."""

    neo4j: Neo4jRepository
    tombstone_digests: EvidenceTombstoneDigestService | None = None
    artifact_manifests: GraphitiArtifactManifestService | None = None

    def activation_status(self) -> PublicationActivationStatus:
        """Report missing safety prerequisites without mutating graph state."""

        blockers: list[str] = []
        if self.tombstone_digests is None:
            blockers.append("managed evidence-tombstone HMAC key ring is unavailable")
        elif not self.tombstone_digests.active_key_ids():
            blockers.append("evidence-tombstone HMAC key ring has no active keys")
        if self.artifact_manifests is None:
            blockers.append("created-only Graphiti artifact manifest is unavailable")
        return PublicationActivationStatus(enabled=not blockers, blockers=tuple(blockers))

    def require_activation(self) -> None:
        """Fail closed before dispatch if opaque tombstones or full quarantine are unavailable."""

        status = self.activation_status()
        if not status.enabled:
            raise PublicationActivationBlocked(
                "evidence publication intent activation blocked: " + "; ".join(status.blockers)
            )

    def begin(
        self,
        *,
        episode_uuid: str,
        namespace: str | None,
        expected_name: str,
        source_description: str,
        reference_time: datetime,
    ) -> PublicationIntent:
        """Create/merge an intent under the namespace fence before remote dispatch.

        A fresh random dispatch token is not an operation identity.  It only distinguishes this
        caller from a later process reopening the same stable intent after a crash.  Re-executing
        this one statement after an ambiguous Neo4j acknowledgement remains idempotent because the
        same call retains the same token.
        """

        self.require_activation()

        intent_key = publication_intent_key(episode_uuid)
        operation_id = publication_operation_id(episode_uuid)
        namespace_key = normalize_namespace(namespace)
        group_id = namespace_to_group_id(namespace_key)
        now = _now()
        dispatch_token = str(uuidlib.uuid4())
        rows = self.neo4j.execute(
            BEGIN_INTENT_CYPHER,
            params={
                "intent_key": intent_key,
                "operation_id": operation_id,
                "episode_uuid": episode_uuid,
                "namespace_key": namespace_key,
                "group_id": group_id,
                "expected_name": expected_name,
                "source_description": source_description,
                "reference_time": reference_time.isoformat(),
                "dispatch_token": dispatch_token,
                "now": now,
            },
        )
        if len(rows) != 1:
            raise PublicationIntentError(
                f"publication intent {intent_key!r} was not uniquely persisted"
            )
        row = rows[0]
        intent = _intent_from_row(
            row,
            dispatch_allowed=bool(row.get("dispatch_allowed", False)),
        )
        expected_identity = (
            operation_id,
            episode_uuid,
            namespace_key,
            group_id,
            expected_name,
            source_description,
        )
        actual_identity = (
            intent.operation_id,
            intent.episode_uuid,
            intent.namespace_key,
            intent.group_id,
            intent.expected_name,
            intent.source_description,
        )
        if actual_identity != expected_identity:
            raise PublicationIntentError(
                f"publication intent {intent_key!r} is bound to a different identity"
            )
        return intent

    def get(self, episode_uuid: str) -> PublicationIntent | None:
        """Read the stable intent for a queued episode, if one exists."""

        rows = self.neo4j.execute(
            GET_INTENT_CYPHER,
            params={"intent_key": publication_intent_key(episode_uuid)},
        )
        return _intent_from_row(rows[0]) if len(rows) == 1 else None

    def finalize_remote_outcome(
        self,
        intent: PublicationIntent,
        *,
        remote_episode_uuid: str | None,
    ) -> PublicationTransition:
        """Finalize one exact remote outcome or quarantine every ambiguous candidate.

        The complete eligibility decision and mutation are one Cypher statement under the same
        namespace fence.  ``remote_episode_uuid=None`` is reserved for reconciliation after an
        exact identity lookup found multiple candidates; it can never finalize.
        """

        self.require_activation()
        assert self.tombstone_digests is not None
        assert self.artifact_manifests is not None
        active_key_ids = self.tombstone_digests.active_key_ids()
        probes = self.tombstone_digests.probes_for_publication(
            namespace_key=intent.namespace_key,
            queued_episode_uuid=intent.episode_uuid,
            remote_episode_uuid=remote_episode_uuid,
            operation_id=intent.operation_id,
        )
        probe_key_ids = {probe.key_id for probe in probes}
        raw_identities = {
            intent.episode_uuid,
            intent.operation_id,
            intent.intent_key,
            remote_episode_uuid or "",
        }
        probes_are_opaque = all(
            _OPAQUE_DIGEST_PATTERN.fullmatch(probe.digest) is not None
            and _KEY_ID_PATTERN.fullmatch(probe.key_id) is not None
            and probe.digest not in raw_identities
            for probe in probes
        )
        if (
            not probes
            or probe_key_ids != set(active_key_ids)
            or not probes_are_opaque
        ):
            raise PublicationActivationBlocked(
                "evidence publication intent activation blocked: tombstone probes do not "
                "cover every active HMAC key"
            )
        manifest = self.artifact_manifests.created_artifacts(
            intent=intent,
            remote_episode_uuid=remote_episode_uuid,
        )
        if not manifest.complete or not manifest.quarantine_safe:
            raise PublicationActivationBlocked(
                "evidence publication intent activation blocked: Graphiti artifact manifest "
                "does not prove complete created-only quarantine coverage"
            )
        if remote_episode_uuid is not None and remote_episode_uuid not in manifest.node_uuids:
            raise PublicationActivationBlocked(
                "evidence publication intent activation blocked: remote episode is absent "
                "from the created-artifact manifest"
            )

        rows = self.neo4j.execute(
            FINALIZE_REMOTE_OUTCOME_CYPHER,
            params={
                "intent_key": intent.intent_key,
                "operation_id": intent.operation_id,
                "episode_uuid": intent.episode_uuid,
                "namespace_key": intent.namespace_key,
                "group_id": intent.group_id,
                "expected_name": intent.expected_name,
                "source_description": intent.source_description,
                "reference_time": intent.reference_time,
                "generation": intent.generation,
                "remote_episode_uuid": remote_episode_uuid,
                "tombstone_probes": [
                    {"digest": probe.digest, "key_id": probe.key_id}
                    for probe in probes
                ],
                "artifact_node_uuids": list(manifest.node_uuids),
                "artifact_edge_uuids": list(manifest.edge_uuids),
                "lease_owner": intent.lease_owner,
                "lease_token": intent.lease_token,
                "lease_generation": intent.lease_generation,
                "now": _now(),
                **tenant_scope_params(intent.namespace_key),
            },
        )
        if len(rows) != 1:
            raise PublicationIntentError(
                f"publication intent {intent.intent_key!r} could not transition under its fence"
            )
        row = rows[0]
        return PublicationTransition(
            intent_key=str(row.get("intent_key") or intent.intent_key),
            status=str(row.get("status") or QUARANTINED),
            resolved_episode_uuid=str(row.get("resolved_episode_uuid") or "") or None,
            candidate_count=int(row.get("candidate_count") or 0),
            tombstone_count=int(row.get("tombstone_count") or 0),
            reason=str(row.get("reason") or "") or None,
        )

    def claim_pending(
        self,
        *,
        owner_id: str,
        limit: int = 25,
        lease_seconds: int = 60,
    ) -> list[PublicationIntent]:
        """Claim a bounded pending batch with idempotent lease tokens."""

        self.require_activation()

        safe_limit = max(1, min(int(limit), 200))
        safe_lease_seconds = max(5, min(int(lease_seconds), 3600))
        lease_token = str(uuidlib.uuid4())
        now = _now()
        rows = self.neo4j.execute(
            CLAIM_PENDING_CYPHER,
            params={
                "owner_id": owner_id,
                "lease_token": lease_token,
                "lease_seconds": safe_lease_seconds,
                "limit": safe_limit,
                "now": now,
            },
        )
        return [_intent_from_row(row) for row in rows]

    def discover_exact_remote_uuids(self, intent: PublicationIntent) -> tuple[str, ...]:
        """Discover remote UUIDs only by the full persisted identity and tenant tuple."""

        rows = self.neo4j.execute(
            DISCOVER_EXACT_REMOTE_UUIDS_CYPHER,
            params={
                "episode_uuid": intent.episode_uuid,
                "expected_name": intent.expected_name,
                "group_id": intent.group_id,
                "source_description": intent.source_description,
                "reference_time": intent.reference_time,
                **tenant_scope_params(intent.namespace_key),
            },
        )
        values = rows[0].get("episode_uuids", []) if rows else []
        return tuple(sorted(str(value) for value in values if str(value)))

    def release_pending(self, intent: PublicationIntent) -> bool:
        """Release an inconclusive reconciliation claim back to pending."""

        rows = self.neo4j.execute(
            RELEASE_PENDING_CYPHER,
            params={
                "intent_key": intent.intent_key,
                "lease_owner": intent.lease_owner,
                "lease_token": intent.lease_token,
                "lease_generation": intent.lease_generation,
                "now": _now(),
            },
        )
        return bool(rows and int(rows[0].get("released") or 0) == 1)

    def reconcile_claim(self, intent: PublicationIntent) -> PublicationTransition | None:
        """Idempotently finalize/quarantine a claimed intent when its outcome is discoverable.

        No exact candidates means the remote outcome is still unknown, so the lease is released
        and the intent remains pending.  Multiple exact candidates are affirmative ambiguity and
        are quarantined; one exact candidate is submitted to the same fenced finalizer used by the
        synchronous return path.
        """

        if intent.status != CLAIMED or not intent.lease_token:
            raise PublicationIntentError("reconcile_claim requires an active claimed lease")
        remote_uuids = self.discover_exact_remote_uuids(intent)
        if not remote_uuids:
            self.release_pending(intent)
            return None
        return self.finalize_remote_outcome(
            intent,
            remote_episode_uuid=remote_uuids[0] if len(remote_uuids) == 1 else None,
        )

    def fetch_episode_artifact(
        self,
        intent: PublicationIntent,
        remote_episode_uuid: str,
    ) -> EpisodeArtifact | None:
        """Load one exact, finalized remote artifact without anchor-name reconciliation."""

        rows = self.neo4j.execute(
            FETCH_EPISODE_ARTIFACT_CYPHER,
            params={
                "remote_episode_uuid": remote_episode_uuid,
                "group_id": intent.group_id,
                "intent_key": intent.intent_key,
                **tenant_scope_params(intent.namespace_key),
            },
        )
        if len(rows) != 1:
            return None
        row = rows[0]
        resolved = str(row.get("resolved_episode_uuid") or "")
        if not resolved:
            return None
        return EpisodeArtifact(
            resolved_episode_uuid=resolved,
            entity_uuids=tuple(
                dict.fromkeys(str(value) for value in row.get("entity_uuids", []) if str(value))
            ),
            edge_uuids=tuple(
                dict.fromkeys(str(value) for value in row.get("edge_uuids", []) if str(value))
            ),
        )
