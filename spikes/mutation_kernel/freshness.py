"""Certified read-side freshness for semantic projections.

Experiment 16 deliberately left old projection rows in place while resolver-driven rebuild work was
pending. A read path therefore needs more than "does a projection row exist?".

This experiment makes freshness a certificate over three pieces of durable state:
* the current semantic-definition version;
* the exact work generation that was rebuilt; and
* the exact persisted projection hash produced by that rebuild.

A work-generation acknowledgement alone is insufficient. If a worker marks generation N complete
without writing/certifying the expected projection, readers continue to treat the outcome as stale.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Literal

from .neo4j_store import (
    Neo4jEnvelopeStore,
    ProjectionOutcome,
    _hash_parts,
    _pairs_json,
    _projection_slot,
    _stable_json,
)
from .registry import ProjectionTarget
from .trust_deployment import (
    PurposeTrustResolverDefinition,
    _definition_storage_key,
    _dependency_storage_key,
    _target_json,
    _work_storage_key,
    resolver_definition_contract_hash,
)


FreshReadPolicy = Literal["require_fresh", "allow_stale_with_marker"]
FreshReadState = Literal["fresh", "stale", "unavailable"]


@dataclass(frozen=True)
class ProjectionFreshnessCertificate:
    resolver_id: str
    resolver_version: int
    target: ProjectionTarget
    generation: int
    projection_hash: str
    derivation_id: str

    def __post_init__(self) -> None:
        if not self.resolver_id.strip() or self.resolver_version < 1:
            raise ValueError("freshness certificate requires resolver ID/version")
        if self.generation < 0:
            raise ValueError("freshness certificate generation must be >= 0")
        if not self.projection_hash.strip() or not self.derivation_id.strip():
            raise ValueError("freshness certificate requires projection hash and derivation ID")


@dataclass(frozen=True)
class FreshProjectionRead:
    state: FreshReadState
    policy: FreshReadPolicy
    reason: str
    resolver_id: str
    current_resolver_version: int | None
    target: ProjectionTarget
    work_generation: int | None
    completed_generation: int | None
    certified_resolver_version: int | None
    certified_projection_hash: str | None
    projection_hash: str | None
    derivation_id: str | None
    outcome: ProjectionOutcome | None

    @property
    def fresh(self) -> bool:
        return self.state == "fresh"

    @property
    def stale(self) -> bool:
        return self.state == "stale"


class TrustResolverProjectionFreshness:
    """Certify and read one resolver-dependent generic projection target."""

    def __init__(
        self,
        neo4j: Any,
        *,
        namespace: str,
        envelope_store: Neo4jEnvelopeStore | None = None,
    ) -> None:
        if not namespace.strip():
            raise ValueError("TrustResolverProjectionFreshness.namespace must be non-empty")
        self._neo4j = neo4j
        self.namespace = namespace.strip()
        self._store = envelope_store or Neo4jEnvelopeStore(
            neo4j,
            namespace=self.namespace,
        )

    @staticmethod
    def _assert_target_matches_outcome(
        target: ProjectionTarget,
        outcome: ProjectionOutcome,
    ) -> None:
        if _projection_slot(outcome) != (
            target.view_type,
            target.subject_id,
            target.scope,
            target.key,
            target.dimensions,
        ):
            raise ValueError("freshness certificate target does not match projection outcome")

    def _projection_identity(self, target: ProjectionTarget) -> tuple[str, str]:
        projection_key = _hash_parts(
            target.view_type,
            target.subject_id,
            target.scope,
            target.key,
            _pairs_json(target.dimensions),
        )
        return projection_key, _hash_parts(self.namespace, projection_key)

    def expected_projection_hash(self, outcome: ProjectionOutcome) -> str:
        payload = self._store._encode_outcome(outcome)  # spike-local reuse of generic envelope codec
        return _hash_parts(_stable_json(payload))

    def certify(
        self,
        definition: PurposeTrustResolverDefinition,
        *,
        target: ProjectionTarget,
        outcome: ProjectionOutcome,
        derivation_id: str,
        derivation_resolver_version: int,
        generation: int,
    ) -> ProjectionFreshnessCertificate:
        """Certify the exact persisted outcome for one current resolver/work generation."""
        if not derivation_id.strip():
            raise ValueError("freshness derivation_id must be non-empty")
        if derivation_resolver_version != definition.version:
            raise ValueError("derivation resolver version does not match current definition")
        if generation < 0:
            raise ValueError("freshness generation must be >= 0")
        if dict(target.dimensions).get("purpose") not in definition.purposes:
            raise ValueError("freshness target purpose is not declared by resolver definition")
        self._assert_target_matches_outcome(target, outcome)

        _projection_key, projection_storage_key = self._projection_identity(target)
        expected_hash = self.expected_projection_hash(outcome)
        rows = self._neo4j.execute(
            """
            MATCH (definition:MutationTrustResolverDefinition {storage_key:$definition_key})
            WHERE definition.current_version=$resolver_version
              AND definition.contract_hash=$contract_hash
            MATCH (dependency:MutationTrustResolverDependency {storage_key:$dependency_key})
            MERGE (work:MutationTrustResolverWork {storage_key:$work_key})
              ON CREATE SET work.namespace=$namespace,
                            work.resolver_id=$resolver_id,
                            work.target_json=$target_json,
                            work.resolver_version=$resolver_version,
                            work.generation=0,
                            work.completed_generation=0,
                            work.created_at=datetime()
            WITH definition, dependency, work
            WHERE work.resolver_version=$resolver_version
              AND coalesce(work.generation,0)=$generation
            MATCH (projection:MutationProjection {storage_key:$projection_storage_key})
            WHERE projection.namespace=$namespace
              AND projection.projection_hash=$expected_projection_hash
            SET work.completed_generation=$generation,
                work.certified_resolver_version=$resolver_version,
                work.certified_projection_hash=$expected_projection_hash,
                work.certified_derivation_id=$derivation_id,
                work.certified_at=datetime(),
                work.completed_at=datetime()
            RETURN work.certified_projection_hash AS projection_hash,
                   work.certified_derivation_id AS derivation_id,
                   work.completed_generation AS completed_generation
            """,
            {
                "definition_key": _definition_storage_key(
                    self.namespace, definition.resolver_id
                ),
                "resolver_version": int(definition.version),
                "contract_hash": resolver_definition_contract_hash(definition),
                "dependency_key": _dependency_storage_key(
                    self.namespace, definition.resolver_id, target
                ),
                "work_key": _work_storage_key(
                    self.namespace, definition.resolver_id, target
                ),
                "namespace": self.namespace,
                "resolver_id": definition.resolver_id,
                "target_json": _target_json(target),
                "generation": int(generation),
                "projection_storage_key": projection_storage_key,
                "expected_projection_hash": expected_hash,
                "derivation_id": derivation_id,
            },
        )
        if len(rows) != 1:
            raise ValueError(
                "cannot certify freshness: current definition/work/projection does not match derivation"
            )
        return ProjectionFreshnessCertificate(
            resolver_id=definition.resolver_id,
            resolver_version=definition.version,
            target=target,
            generation=generation,
            projection_hash=str(rows[0]["projection_hash"]),
            derivation_id=str(rows[0]["derivation_id"]),
        )

    def read(
        self,
        *,
        resolver_id: str,
        target: ProjectionTarget,
        policy: FreshReadPolicy = "require_fresh",
    ) -> FreshProjectionRead:
        """Read projection + semantic/work freshness from one Neo4j query snapshot."""
        if policy not in {"require_fresh", "allow_stale_with_marker"}:
            raise ValueError("unsupported freshness read policy")
        if not resolver_id.strip():
            raise ValueError("resolver_id must be non-empty")
        _projection_key, projection_storage_key = self._projection_identity(target)
        rows = self._neo4j.execute(
            """
            MATCH (definition:MutationTrustResolverDefinition {storage_key:$definition_key})
            OPTIONAL MATCH (dependency:MutationTrustResolverDependency {storage_key:$dependency_key})
            OPTIONAL MATCH (work:MutationTrustResolverWork {storage_key:$work_key})
            OPTIONAL MATCH (projection:MutationProjection {storage_key:$projection_storage_key})
            RETURN definition.current_version AS current_resolver_version,
                   dependency.storage_key AS dependency_key,
                   work.resolver_version AS work_resolver_version,
                   work.generation AS work_generation,
                   work.completed_generation AS completed_generation,
                   work.certified_resolver_version AS certified_resolver_version,
                   work.certified_projection_hash AS certified_projection_hash,
                   work.certified_derivation_id AS derivation_id,
                   projection.projection_hash AS projection_hash,
                   projection.payload_json AS payload_json
            """,
            {
                "definition_key": _definition_storage_key(self.namespace, resolver_id),
                "dependency_key": _dependency_storage_key(
                    self.namespace, resolver_id, target
                ),
                "work_key": _work_storage_key(self.namespace, resolver_id, target),
                "projection_storage_key": projection_storage_key,
            },
        )
        if len(rows) != 1:
            return FreshProjectionRead(
                state="unavailable",
                policy=policy,
                reason="resolver_definition_missing",
                resolver_id=resolver_id,
                current_resolver_version=None,
                target=target,
                work_generation=None,
                completed_generation=None,
                certified_resolver_version=None,
                certified_projection_hash=None,
                projection_hash=None,
                derivation_id=None,
                outcome=None,
            )

        row = rows[0]
        current_version = int(row.get("current_resolver_version", 0) or 0)
        dependency_exists = bool(row.get("dependency_key"))
        work_generation = (
            None if row.get("work_generation") is None else int(row["work_generation"])
        )
        completed_generation = (
            None
            if row.get("completed_generation") is None
            else int(row["completed_generation"])
        )
        certified_version = (
            None
            if row.get("certified_resolver_version") is None
            else int(row["certified_resolver_version"])
        )
        certified_hash = (
            None
            if row.get("certified_projection_hash") is None
            else str(row["certified_projection_hash"])
        )
        projection_hash = (
            None if row.get("projection_hash") is None else str(row["projection_hash"])
        )
        derivation_id = (
            None if row.get("derivation_id") is None else str(row["derivation_id"])
        )
        payload_json = row.get("payload_json")
        outcome = (
            None
            if payload_json is None
            else self._store._hydrate_outcome(json.loads(str(payload_json)))
        )

        if not dependency_exists:
            return FreshProjectionRead(
                state="unavailable",
                policy=policy,
                reason="unregistered_dependency",
                resolver_id=resolver_id,
                current_resolver_version=current_version,
                target=target,
                work_generation=work_generation,
                completed_generation=completed_generation,
                certified_resolver_version=certified_version,
                certified_projection_hash=certified_hash,
                projection_hash=projection_hash,
                derivation_id=derivation_id,
                outcome=None,
            )
        if outcome is None or projection_hash is None:
            return FreshProjectionRead(
                state="unavailable",
                policy=policy,
                reason="projection_missing",
                resolver_id=resolver_id,
                current_resolver_version=current_version,
                target=target,
                work_generation=work_generation,
                completed_generation=completed_generation,
                certified_resolver_version=certified_version,
                certified_projection_hash=certified_hash,
                projection_hash=projection_hash,
                derivation_id=derivation_id,
                outcome=None,
            )

        reasons: list[str] = []
        if work_generation is None or completed_generation is None:
            reasons.append("uncertified_work")
        elif work_generation != completed_generation:
            reasons.append("pending_generation")
        if certified_version != current_version:
            reasons.append("resolver_version_not_certified")
        if certified_hash != projection_hash:
            reasons.append("projection_hash_not_certified")
        if not derivation_id:
            reasons.append("derivation_not_certified")

        if not reasons:
            return FreshProjectionRead(
                state="fresh",
                policy=policy,
                reason="certified_current",
                resolver_id=resolver_id,
                current_resolver_version=current_version,
                target=target,
                work_generation=work_generation,
                completed_generation=completed_generation,
                certified_resolver_version=certified_version,
                certified_projection_hash=certified_hash,
                projection_hash=projection_hash,
                derivation_id=derivation_id,
                outcome=outcome,
            )

        reason = ",".join(reasons)
        if policy == "allow_stale_with_marker":
            return FreshProjectionRead(
                state="stale",
                policy=policy,
                reason=reason,
                resolver_id=resolver_id,
                current_resolver_version=current_version,
                target=target,
                work_generation=work_generation,
                completed_generation=completed_generation,
                certified_resolver_version=certified_version,
                certified_projection_hash=certified_hash,
                projection_hash=projection_hash,
                derivation_id=derivation_id,
                outcome=outcome,
            )
        return FreshProjectionRead(
            state="unavailable",
            policy=policy,
            reason=reason,
            resolver_id=resolver_id,
            current_resolver_version=current_version,
            target=target,
            work_generation=work_generation,
            completed_generation=completed_generation,
            certified_resolver_version=certified_version,
            certified_projection_hash=certified_hash,
            projection_hash=projection_hash,
            derivation_id=derivation_id,
            outcome=None,
        )
