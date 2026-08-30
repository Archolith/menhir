"""Durable publication intent protocol for local Graphiti episode writes.

Graphiti cannot accept Menhir's queued episode UUID as the remote episode UUID.  A timeout can
therefore hide a committed remote episode from the caller.  This repository records the intent
before dispatch, fences publication by namespace generation, and only makes returned evidence
eligible for View admission after its exact tenant-bound identity is unique and untombstoned.

The reconciler API in this module is deliberately passive.  Nothing registers it with a scheduler;
an operator or a later, separately activated bootstrap hook must call it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Protocol
import uuid as uuidlib

from menhir.domain.namespace import (
    normalize_namespace,
    namespace_to_group_id,
    tenant_scope_cypher,
    tenant_scope_params,
)
from menhir.infrastructure.neo4j import Neo4jRepository


PENDING = "PENDING"
CLAIMED = "CLAIMED"
FINALIZED = "FINALIZED"
QUARANTINED = "QUARANTINED"

_OPAQUE_DIGEST_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class PublicationIntentError(RuntimeError):
    """Base failure for a publication intent transition."""


class PublicationDispatchSuppressed(PublicationIntentError):
    """A stable intent already exists, so another ambiguous dispatch is refused."""


class PublicationActivationBlocked(PublicationIntentError):
    """The optional protocol lacks a prerequisite needed to fail closed."""


@dataclass(frozen=True)
class PublicationActivationStatus:
    """Whether this local repository can safely dispatch publication intents."""

    enabled: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class TombstoneProbe:
    """Opaque HMAC lookup produced for one active tombstone key."""

    digest: str
    key_id: str


@dataclass(frozen=True)
class GraphitiArtifactManifest:
    """Created-only Graphiti rows that must transition together with the episode."""

    node_uuids: tuple[str, ...]
    edge_uuids: tuple[str, ...]
    complete: bool
    quarantine_safe: bool


class EvidenceTombstoneDigestService(Protocol):
    """Managed HMAC key-ring boundary; raw erased IDs never enter tombstone queries."""

    def active_key_ids(self) -> tuple[str, ...]: ...

    def probes_for_publication(
        self,
        *,
        namespace_key: str,
        queued_episode_uuid: str,
        remote_episode_uuid: str | None,
        operation_id: str,
    ) -> tuple[TombstoneProbe, ...]: ...


class GraphitiArtifactManifestService(Protocol):
    """Proves which Graphiti rows were created by one publication operation."""

    def created_artifacts(
        self,
        *,
        intent: "PublicationIntent",
        remote_episode_uuid: str | None,
    ) -> GraphitiArtifactManifest: ...


@dataclass(frozen=True)
class PublicationIntent:
    """Generation-fenced identity captured before one Graphiti dispatch."""

    intent_key: str
    operation_id: str
    episode_uuid: str
    namespace_key: str
    group_id: str
    expected_name: str
    source_description: str
    reference_time: str
    generation: int
    status: str
    resolved_episode_uuid: str | None = None
    dispatch_allowed: bool = False
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_generation: int | None = None


@dataclass(frozen=True)
class PublicationTransition:
    """Outcome of finalizing or quarantining one remote publication."""

    intent_key: str
    status: str
    resolved_episode_uuid: str | None
    candidate_count: int
    tombstone_count: int
    reason: str | None

    @property
    def finalized(self) -> bool:
        return self.status == FINALIZED


@dataclass(frozen=True)
class EpisodeArtifact:
    """Exact Graphiti artifact reconstructed for an intent reconciliation."""

    resolved_episode_uuid: str
    entity_uuids: tuple[str, ...]
    edge_uuids: tuple[str, ...]


def publication_intent_key(episode_uuid: str) -> str:
    """Return the stable publication-intent key for a queued episode UUID."""

    return f"evidence-publication:{episode_uuid}"


def publication_operation_id(episode_uuid: str) -> str:
    """Return the stable external-operation identity for a queued episode UUID."""

    return f"graphiti-add-episode:{episode_uuid}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _intent_from_row(row: dict[str, object], *, dispatch_allowed: bool = False) -> PublicationIntent:
    return PublicationIntent(
        intent_key=str(row.get("intent_key") or ""),
        operation_id=str(row.get("operation_id") or ""),
        episode_uuid=str(row.get("episode_uuid") or ""),
        namespace_key=str(row.get("namespace_key") or ""),
        group_id=str(row.get("group_id") or ""),
        expected_name=str(row.get("expected_name") or ""),
        source_description=str(row.get("source_description") or ""),
        reference_time=str(row.get("reference_time") or ""),
        generation=int(row.get("generation") or 0),
        status=str(row.get("status") or PENDING),
        resolved_episode_uuid=str(row.get("resolved_episode_uuid") or "") or None,
        dispatch_allowed=dispatch_allowed,
        lease_owner=str(row.get("lease_owner") or "") or None,
        lease_token=str(row.get("lease_token") or "") or None,
        lease_generation=(
            int(row["lease_generation"])
            if row.get("lease_generation") is not None
            else None
        ),
    )


@dataclass(frozen=True)
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
            """
            MERGE (f:EvidenceNamespaceFence {namespace_key: $namespace_key})
            ON CREATE SET f.generation = 0, f.created_at = datetime($now)
            SET f.lock_nonce = $operation_id, f.locked_at = datetime($now)
            WITH f
            MERGE (i:EvidencePublicationIntent {intent_key: $intent_key})
            ON CREATE SET
                i.operation_id = $operation_id,
                i.episode_uuid = $episode_uuid,
                i.namespace_key = $namespace_key,
                i.group_id = $group_id,
                i.expected_name = $expected_name,
                i.source_description = $source_description,
                i.reference_time = datetime($reference_time),
                i.generation = f.generation,
                i.status = 'PENDING',
                i.dispatch_token = $dispatch_token,
                i.created_at = datetime($now)
            SET i.updated_at = datetime($now)
            MERGE (f)-[:GOVERNS_PUBLICATION]->(i)
            RETURN i.intent_key AS intent_key,
                   i.operation_id AS operation_id,
                   i.episode_uuid AS episode_uuid,
                   i.namespace_key AS namespace_key,
                   i.group_id AS group_id,
                   i.expected_name AS expected_name,
                   i.source_description AS source_description,
                   toString(i.reference_time) AS reference_time,
                   i.generation AS generation,
                   i.status AS status,
                   i.resolved_episode_uuid AS resolved_episode_uuid,
                   i.lease_owner AS lease_owner,
                   i.lease_token AS lease_token,
                   i.lease_generation AS lease_generation,
                   i.dispatch_token = $dispatch_token AND i.status = 'PENDING'
                       AS dispatch_allowed
            """,
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
            """
            MATCH (i:EvidencePublicationIntent {intent_key: $intent_key})
            RETURN i.intent_key AS intent_key,
                   i.operation_id AS operation_id,
                   i.episode_uuid AS episode_uuid,
                   i.namespace_key AS namespace_key,
                   i.group_id AS group_id,
                   i.expected_name AS expected_name,
                   i.source_description AS source_description,
                   toString(i.reference_time) AS reference_time,
                   i.generation AS generation,
                   i.status AS status,
                   i.resolved_episode_uuid AS resolved_episode_uuid,
                   i.lease_owner AS lease_owner,
                   i.lease_token AS lease_token,
                   i.lease_generation AS lease_generation
            """,
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
            """
            MATCH (f:EvidenceNamespaceFence {namespace_key: $namespace_key})
            SET f.lock_nonce = $operation_id, f.locked_at = datetime($now)
            WITH f
            MATCH (i:EvidencePublicationIntent {intent_key: $intent_key})
            OPTIONAL MATCH (e:Episodic)
            WHERE e.uuid <> $episode_uuid
              AND ($remote_episode_uuid IS NULL OR e.uuid = $remote_episode_uuid)
              AND e.name = $expected_name
              AND """ + tenant_scope_cypher("e") + """
              AND coalesce(e.source_description, '') = $source_description
              AND e.valid_at = datetime($reference_time)
            WITH f, i, [candidate IN collect(DISTINCT e)
                        WHERE candidate IS NOT NULL] AS candidates
            OPTIONAL MATCH (artifact_node)
            WHERE artifact_node.uuid IN $artifact_node_uuids
            WITH f, i, candidates,
                 [node IN collect(DISTINCT artifact_node) WHERE node IS NOT NULL] AS artifact_nodes
            OPTIONAL MATCH (artifact_start)-[artifact_edge]-(artifact_end)
            WHERE artifact_edge.uuid IN $artifact_edge_uuids
            WITH f, i, candidates, artifact_nodes,
                 [edge IN collect(DISTINCT artifact_edge) WHERE edge IS NOT NULL] AS artifact_edges
            OPTIONAL MATCH (t:EvidenceTombstone)
            WHERE coalesce(t.status, 'ACTIVE') IN ['ACTIVE', 'PENDING']
              AND coalesce(t.active, true)
              AND t.cleared_at IS NULL
              AND any(probe IN $tombstone_probes
                      WHERE t.digest = probe.digest AND t.key_id = probe.key_id)
            WITH f, i, candidates, artifact_nodes, artifact_edges,
                 [tombstone IN collect(DISTINCT t)
                  WHERE tombstone IS NOT NULL] AS tombstones
            WITH f, i, candidates, artifact_nodes, artifact_edges, tombstones,
                 i.status IN ['PENDING', 'CLAIMED']
                 AND ($lease_token IS NULL OR (
                    i.lease_owner = $lease_owner
                    AND i.lease_token = $lease_token
                    AND i.lease_generation = $lease_generation
                 )) AS transition_allowed,
                 i.status = 'FINALIZED'
                 AND i.resolved_episode_uuid = $remote_episode_uuid
                 AND i.generation = $generation
                 AND f.generation = $generation AS already_finalized,
                 i.status = 'QUARANTINED' AS already_quarantined
            WITH f, i, candidates, artifact_nodes, artifact_edges, tombstones,
                 already_finalized, already_quarantined,
                 transition_allowed
                 AND i.generation = $generation
                 AND f.generation = $generation
                 AND $remote_episode_uuid IS NOT NULL
                 AND size(candidates) = 1
                 AND size(artifact_nodes) = size($artifact_node_uuids)
                 AND size(artifact_edges) = size($artifact_edge_uuids)
                 AND size(tombstones) = 0 AS eligible
            FOREACH (artifact_node IN artifact_nodes |
                SET artifact_node.evidence_finalized = false,
                    artifact_node.evidence_quarantined = true,
                    artifact_node.publication_intent_key = $intent_key,
                    artifact_node.publication_operation_id = $operation_id,
                    artifact_node.publication_generation = $generation,
                    artifact_node.evidence_generation = $generation)
            FOREACH (artifact_edge IN artifact_edges |
                SET artifact_edge.evidence_finalized = false,
                    artifact_edge.evidence_quarantined = true,
                    artifact_edge.publication_intent_key = $intent_key,
                    artifact_edge.publication_operation_id = $operation_id,
                    artifact_edge.publication_generation = $generation,
                    artifact_edge.evidence_generation = $generation)
            FOREACH (artifact_node IN CASE WHEN eligible OR already_finalized
                                           THEN artifact_nodes ELSE [] END |
                SET artifact_node.evidence_finalized = true,
                    artifact_node.evidence_quarantined = false,
                    artifact_node.evidence_finalized_at = datetime($now)
                REMOVE artifact_node.evidence_quarantine_reason)
            FOREACH (artifact_edge IN CASE WHEN eligible OR already_finalized
                                           THEN artifact_edges ELSE [] END |
                SET artifact_edge.evidence_finalized = true,
                    artifact_edge.evidence_quarantined = false,
                    artifact_edge.evidence_finalized_at = datetime($now)
                REMOVE artifact_edge.evidence_quarantine_reason)
            FOREACH (artifact_node IN CASE WHEN eligible OR already_finalized
                                           THEN [] ELSE artifact_nodes END |
                SET artifact_node.evidence_quarantine_reason = CASE
                    WHEN f.generation <> $generation OR i.generation <> $generation
                        THEN 'generation_mismatch'
                    WHEN size(candidates) <> 1 THEN 'remote_identity_not_unique'
                    WHEN size(tombstones) > 0 THEN 'active_tombstone'
                    WHEN $lease_token IS NOT NULL AND (
                        i.lease_owner <> $lease_owner OR
                        i.lease_token <> $lease_token OR
                        i.lease_generation <> $lease_generation
                    ) THEN 'lease_lost'
                    ELSE 'intent_not_finalizable'
                END)
            FOREACH (artifact_edge IN CASE WHEN eligible OR already_finalized
                                           THEN [] ELSE artifact_edges END |
                SET artifact_edge.evidence_quarantine_reason = CASE
                    WHEN f.generation <> $generation OR i.generation <> $generation
                        THEN 'generation_mismatch'
                    WHEN size(candidates) <> 1 THEN 'remote_identity_not_unique'
                    WHEN size(artifact_nodes) <> size($artifact_node_uuids)
                      OR size(artifact_edges) <> size($artifact_edge_uuids)
                        THEN 'artifact_manifest_mismatch'
                    WHEN size(tombstones) > 0 THEN 'active_tombstone'
                    WHEN $lease_token IS NOT NULL AND (
                        i.lease_owner <> $lease_owner OR
                        i.lease_token <> $lease_token OR
                        i.lease_generation <> $lease_generation
                    ) THEN 'lease_lost'
                    ELSE 'intent_not_finalizable'
                END)
            SET i.status = CASE
                    WHEN already_finalized THEN 'FINALIZED'
                    WHEN already_quarantined THEN 'QUARANTINED'
                    WHEN eligible THEN 'FINALIZED'
                    ELSE 'QUARANTINED'
                END,
                i.resolved_episode_uuid = CASE
                    WHEN already_finalized THEN i.resolved_episode_uuid
                    WHEN size(candidates) = 1 THEN head(candidates).uuid ELSE null END,
                i.candidate_count = size(candidates),
                i.tombstone_count = size(tombstones),
                i.completed_at = coalesce(i.completed_at, datetime($now)),
                i.updated_at = datetime($now),
                i.quarantine_reason = CASE
                    WHEN eligible OR already_finalized THEN null
                    WHEN already_quarantined THEN i.quarantine_reason
                    WHEN f.generation <> $generation OR i.generation <> $generation
                        THEN 'generation_mismatch'
                    WHEN size(candidates) <> 1 THEN 'remote_identity_not_unique'
                    WHEN size(artifact_nodes) <> size($artifact_node_uuids)
                      OR size(artifact_edges) <> size($artifact_edge_uuids)
                        THEN 'artifact_manifest_mismatch'
                    WHEN size(tombstones) > 0 THEN 'active_tombstone'
                    WHEN $lease_token IS NOT NULL AND (
                        i.lease_owner <> $lease_owner OR
                        i.lease_token <> $lease_token OR
                        i.lease_generation <> $lease_generation
                    ) THEN 'lease_lost'
                    ELSE 'intent_not_finalizable'
                END
            REMOVE i.lease_owner, i.lease_token, i.lease_expires_at
            RETURN i.intent_key AS intent_key,
                   i.status AS status,
                   i.resolved_episode_uuid AS resolved_episode_uuid,
                   i.candidate_count AS candidate_count,
                   i.tombstone_count AS tombstone_count,
                   i.quarantine_reason AS reason
            """,
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
            """
            MATCH (i:EvidencePublicationIntent)
            WHERE i.status = 'PENDING'
               OR (i.status = 'CLAIMED' AND i.lease_expires_at <= datetime($now))
            WITH i ORDER BY i.created_at, i.intent_key LIMIT $limit
            SET i.lease_generation = CASE
                    WHEN i.lease_token = $lease_token THEN i.lease_generation
                    ELSE coalesce(i.lease_generation, 0) + 1
                END,
                i.status = 'CLAIMED',
                i.lease_owner = $owner_id,
                i.lease_token = $lease_token,
                i.lease_expires_at = datetime($now) + duration({seconds: $lease_seconds}),
                i.updated_at = datetime($now)
            RETURN i.intent_key AS intent_key,
                   i.operation_id AS operation_id,
                   i.episode_uuid AS episode_uuid,
                   i.namespace_key AS namespace_key,
                   i.group_id AS group_id,
                   i.expected_name AS expected_name,
                   i.source_description AS source_description,
                   toString(i.reference_time) AS reference_time,
                   i.generation AS generation,
                   i.status AS status,
                   i.resolved_episode_uuid AS resolved_episode_uuid,
                   i.lease_owner AS lease_owner,
                   i.lease_token AS lease_token,
                   i.lease_generation AS lease_generation
            ORDER BY i.intent_key
            """,
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
            """
            MATCH (e:Episodic)
            WHERE e.uuid <> $episode_uuid
              AND e.name = $expected_name
              AND """ + tenant_scope_cypher("e") + """
              AND coalesce(e.source_description, '') = $source_description
              AND e.valid_at = datetime($reference_time)
            RETURN collect(DISTINCT e.uuid) AS episode_uuids
            """,
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
            """
            MATCH (i:EvidencePublicationIntent {intent_key: $intent_key})
            WHERE i.status = 'CLAIMED'
              AND i.lease_owner = $lease_owner
              AND i.lease_token = $lease_token
              AND i.lease_generation = $lease_generation
            SET i.status = 'PENDING', i.updated_at = datetime($now)
            REMOVE i.lease_owner, i.lease_token, i.lease_expires_at
            RETURN count(i) AS released
            """,
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
            """
            MATCH (e:Episodic {uuid: $remote_episode_uuid})
            WHERE """ + tenant_scope_cypher("e") + """
              AND e.publication_intent_key = $intent_key
              AND e.evidence_finalized = true
              AND NOT coalesce(e.evidence_quarantined, false)
            OPTIONAL MATCH (e)-[episode_rel]-(n:Entity)
            WITH e, collect(DISTINCT n) AS entities,
                 [rel IN collect(DISTINCT episode_rel)
                  WHERE rel.uuid IS NOT NULL | rel.uuid] AS episode_edge_uuids
            OPTIONAL MATCH (a:Entity)-[entity_rel]-(b:Entity)
            WHERE a IN entities AND b IN entities AND entity_rel.uuid IS NOT NULL
            RETURN e.uuid AS resolved_episode_uuid,
                   [entity IN entities WHERE entity IS NOT NULL | entity.uuid] AS entity_uuids,
                   episode_edge_uuids + collect(DISTINCT entity_rel.uuid) AS edge_uuids
            """,
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
