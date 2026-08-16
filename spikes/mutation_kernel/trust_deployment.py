"""Versioned deployment, invalidation, and stale-writer fencing for purpose-trust resolvers.

Experiment 15 made purpose resolvers belief-forming dependencies: changing resolver semantics can
change a View's winner, contributor set, counterevidence, or whether the fold abstains. This module
therefore treats a resolver definition as deployable semantic state.

The coordinator owns only generic deployment mechanics:
* monotonic resolver definitions and a shared registry epoch;
* dependency registration from a resolver to generic ProjectionTargets;
* dirty-work generations when a resolver advances; and
* a transaction-held registry gate preventing stale resolver workers from committing after upgrade.

Resolver callables remain extension-owned. Their code is not introspected; changing semantics without
bumping the declared resolver version remains an extension contract violation, as in the earlier
projection/graph deployment experiments.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import uuid
from typing import Any, Callable, Sequence, TypeVar

from .neo4j_store import _hash_parts, _stable_json
from .registry import ProjectionTarget
from .trust import PurposeTrustResolver


class StaleTrustResolverRegistryError(RuntimeError):
    """The caller's purpose-resolver snapshot is no longer shared-current."""


@dataclass(frozen=True)
class PurposeTrustResolverDefinition:
    resolver_id: str
    version: int
    purposes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.resolver_id.strip() or self.version < 1:
            raise ValueError("trust resolver definition requires ID and version >= 1")
        canonical = tuple(sorted(str(value).strip() for value in self.purposes))
        if not canonical or any(not value for value in canonical):
            raise ValueError("trust resolver definition requires non-empty purposes")
        if len(canonical) != len(set(canonical)):
            raise ValueError("trust resolver purposes must be unique")
        if self.purposes != canonical:
            raise ValueError("trust resolver purposes must be canonical")

    @classmethod
    def from_resolver(
        cls,
        resolver: PurposeTrustResolver,
        *,
        purposes: Sequence[str],
    ) -> "PurposeTrustResolverDefinition":
        return cls(
            resolver_id=str(resolver.resolver_id),
            version=int(resolver.version),
            purposes=tuple(sorted(str(value).strip() for value in purposes)),
        )

    def assert_resolver_matches(self, resolver: PurposeTrustResolver) -> None:
        if (
            str(resolver.resolver_id) != self.resolver_id
            or int(resolver.version) != self.version
        ):
            raise StaleTrustResolverRegistryError(
                "in-process purpose resolver does not match its declared definition"
            )


@dataclass(frozen=True)
class TrustResolverRegistrySnapshot:
    epoch: int
    resolver_id: str
    resolver_version: int
    contract_hash: str

    def __post_init__(self) -> None:
        if self.epoch < 0 or self.resolver_version < 1:
            raise ValueError("trust resolver snapshot contains invalid epoch/version")
        if not self.resolver_id.strip() or not self.contract_hash.strip():
            raise ValueError("trust resolver snapshot requires ID and contract hash")

    def assert_matches(self, definition: PurposeTrustResolverDefinition) -> None:
        if (
            self.resolver_id != definition.resolver_id
            or self.resolver_version != definition.version
            or self.contract_hash != resolver_definition_contract_hash(definition)
        ):
            raise StaleTrustResolverRegistryError(
                "local purpose resolver definition does not match captured shared snapshot"
            )


@dataclass(frozen=True)
class TrustResolverWork:
    resolver_id: str
    resolver_version: int
    target: ProjectionTarget
    generation: int
    completed_generation: int

    @property
    def pending(self) -> bool:
        return self.generation > self.completed_generation


def resolver_definition_contract_hash(definition: PurposeTrustResolverDefinition) -> str:
    return _hash_parts(
        _stable_json(
            {
                "resolver_id": definition.resolver_id,
                "version": definition.version,
                "purposes": list(definition.purposes),
            }
        )
    )


def _registry_storage_key(namespace: str) -> str:
    return _hash_parts(namespace, "trust-resolver-registry")


def _definition_storage_key(namespace: str, resolver_id: str) -> str:
    return _hash_parts(namespace, "trust-resolver-definition", resolver_id)


def _target_payload(target: ProjectionTarget) -> dict[str, object]:
    return {
        "view_type": target.view_type,
        "subject_id": target.subject_id,
        "scope": target.scope,
        "key": target.key,
        "dimensions": [list(pair) for pair in target.dimensions],
    }


def _target_json(target: ProjectionTarget) -> str:
    return _stable_json(_target_payload(target))


def _target_from_json(value: str) -> ProjectionTarget:
    payload = json.loads(value)
    return ProjectionTarget(
        view_type=str(payload["view_type"]),
        subject_id=str(payload["subject_id"]),
        scope=str(payload.get("scope") or ""),
        key=str(payload["key"]),
        dimensions=tuple(
            (str(name), str(item))
            for name, item in payload.get("dimensions", [])
        ),
    )


def _dependency_storage_key(namespace: str, resolver_id: str, target: ProjectionTarget) -> str:
    return _hash_parts(namespace, "trust-resolver-dependency", resolver_id, _target_json(target))


def _work_storage_key(namespace: str, resolver_id: str, target: ProjectionTarget) -> str:
    return _hash_parts(namespace, "trust-resolver-work", resolver_id, _target_json(target))


T = TypeVar("T")


class TrustResolverDeploymentCoordinator:
    """Shared resolver registry plus dependency invalidation and commit-time stale fencing."""

    def __init__(self, neo4j: Any, *, namespace: str) -> None:
        if not namespace.strip():
            raise ValueError("TrustResolverDeploymentCoordinator.namespace must be non-empty")
        self._neo4j = neo4j
        self.namespace = namespace.strip()

    def activate(self) -> None:
        self._neo4j.execute(
            "CREATE CONSTRAINT mutation_trust_resolver_registry_storage_key IF NOT EXISTS "
            "FOR (r:MutationTrustResolverRegistry) REQUIRE r.storage_key IS UNIQUE"
        )
        self._neo4j.execute(
            "CREATE CONSTRAINT mutation_trust_resolver_definition_storage_key IF NOT EXISTS "
            "FOR (d:MutationTrustResolverDefinition) REQUIRE d.storage_key IS UNIQUE"
        )
        self._neo4j.execute(
            "CREATE CONSTRAINT mutation_trust_resolver_dependency_storage_key IF NOT EXISTS "
            "FOR (d:MutationTrustResolverDependency) REQUIRE d.storage_key IS UNIQUE"
        )
        self._neo4j.execute(
            "CREATE CONSTRAINT mutation_trust_resolver_work_storage_key IF NOT EXISTS "
            "FOR (w:MutationTrustResolverWork) REQUIRE w.storage_key IS UNIQUE"
        )

    def publish_definition(self, definition: PurposeTrustResolverDefinition) -> dict[str, object]:
        """Publish monotonically and dirty all registered dependent targets in the same transaction."""
        contract_hash = resolver_definition_contract_hash(definition)
        registry_key = _registry_storage_key(self.namespace)
        definition_key = _definition_storage_key(self.namespace, definition.resolver_id)
        publish_token = uuid.uuid4().hex
        driver = self._neo4j._get_driver()
        with driver.session(database=self._neo4j.database) as session:
            tx = session.begin_transaction()
            try:
                rows = [
                    record.data()
                    for record in tx.run(
                        """
                        MERGE (registry:MutationTrustResolverRegistry {storage_key:$registry_key})
                          ON CREATE SET registry.namespace=$namespace,
                                        registry.epoch=0,
                                        registry.created_at=datetime()
                        SET registry.active_publish_token=$publish_token
                        MERGE (definition:MutationTrustResolverDefinition {storage_key:$definition_key})
                          ON CREATE SET definition.namespace=$namespace,
                                        definition.resolver_id=$resolver_id,
                                        definition.current_version=0,
                                        definition.created_at=datetime()
                        RETURN coalesce(registry.epoch,0) AS epoch,
                               coalesce(definition.current_version,0) AS old_version,
                               definition.contract_hash AS old_hash
                        """,
                        registry_key=registry_key,
                        definition_key=definition_key,
                        namespace=self.namespace,
                        publish_token=publish_token,
                        resolver_id=definition.resolver_id,
                    )
                ]
                if len(rows) != 1:
                    raise RuntimeError("trust resolver publish bootstrap returned no result")
                old_version = int(rows[0].get("old_version", 0) or 0)
                old_hash = str(rows[0].get("old_hash") or "")
                old_epoch = int(rows[0].get("epoch", 0) or 0)

                if old_version > definition.version:
                    raise StaleTrustResolverRegistryError(
                        f"cannot publish stale resolver {definition.resolver_id} v{definition.version}; "
                        f"shared version is {old_version}"
                    )
                if old_version == definition.version:
                    if old_hash and old_hash != contract_hash:
                        raise ValueError(
                            "purpose resolver contract changed without a version bump: "
                            f"{definition.resolver_id} v{definition.version}"
                        )
                    action = "same"
                    invalidated = 0
                    epoch = old_epoch
                else:
                    action = "advance"
                    epoch = old_epoch + 1
                    update_rows = [
                        record.data()
                        for record in tx.run(
                            """
                            MATCH (registry:MutationTrustResolverRegistry {storage_key:$registry_key})
                            MATCH (definition:MutationTrustResolverDefinition {storage_key:$definition_key})
                            SET registry.epoch=$epoch,
                                registry.updated_at=datetime(),
                                definition.current_version=$resolver_version,
                                definition.contract_hash=$contract_hash,
                                definition.purposes_json=$purposes_json,
                                definition.activated_at=datetime()
                            RETURN registry.epoch AS epoch
                            """,
                            registry_key=registry_key,
                            definition_key=definition_key,
                            epoch=epoch,
                            resolver_version=int(definition.version),
                            contract_hash=contract_hash,
                            purposes_json=_stable_json(list(definition.purposes)),
                        )
                    ]
                    if len(update_rows) != 1:
                        raise RuntimeError("trust resolver definition advance returned no result")

                    invalidated = 0
                    if old_version > 0:
                        dirty_rows = [
                            record.data()
                            for record in tx.run(
                                """
                                MATCH (dependency:MutationTrustResolverDependency {
                                    namespace:$namespace,
                                    resolver_id:$resolver_id
                                })
                                MERGE (work:MutationTrustResolverWork {
                                    storage_key:dependency.work_storage_key
                                })
                                  ON CREATE SET work.namespace=$namespace,
                                                work.resolver_id=$resolver_id,
                                                work.target_json=dependency.target_json,
                                                work.generation=0,
                                                work.completed_generation=0,
                                                work.created_at=datetime()
                                SET work.resolver_version=$resolver_version,
                                    work.generation=coalesce(work.generation,0)+1,
                                    work.dirty_at=datetime(),
                                    dependency.last_invalidated_version=$resolver_version,
                                    dependency.updated_at=datetime()
                                RETURN count(work) AS invalidated
                                """,
                                namespace=self.namespace,
                                resolver_id=definition.resolver_id,
                                resolver_version=int(definition.version),
                            )
                        ]
                        invalidated = int(dirty_rows[0].get("invalidated", 0) or 0) if dirty_rows else 0

                cleanup = [
                    record.data()
                    for record in tx.run(
                        """
                        MATCH (registry:MutationTrustResolverRegistry {storage_key:$registry_key})
                        WHERE registry.active_publish_token=$publish_token
                        REMOVE registry.active_publish_token
                        RETURN count(registry) AS n
                        """,
                        registry_key=registry_key,
                        publish_token=publish_token,
                    )
                ]
                if not cleanup or int(cleanup[0].get("n", 0) or 0) != 1:
                    raise RuntimeError("trust resolver publish lock cleanup failed")
                tx.commit()
                return {
                    "action": action,
                    "epoch": epoch,
                    "current_version": definition.version,
                    "invalidated_target_count": invalidated,
                }
            except Exception:
                tx.rollback()
                raise

    def register_dependency(
        self,
        definition: PurposeTrustResolverDefinition,
        target: ProjectionTarget,
    ) -> dict[str, object]:
        """Register one generic View slot whose fold semantics depend on this resolver."""
        if dict(target.dimensions).get("purpose") not in definition.purposes:
            raise ValueError("resolver dependency target purpose is not declared by the definition")
        rows = self._neo4j.execute(
            """
            MATCH (definition:MutationTrustResolverDefinition {storage_key:$definition_key})
            WHERE definition.current_version=$resolver_version
              AND definition.contract_hash=$contract_hash
            MERGE (dependency:MutationTrustResolverDependency {storage_key:$dependency_key})
              ON CREATE SET dependency.namespace=$namespace,
                            dependency.resolver_id=$resolver_id,
                            dependency.target_json=$target_json,
                            dependency.work_storage_key=$work_key,
                            dependency.created_at=datetime()
            RETURN dependency.storage_key AS storage_key
            """,
            {
                "definition_key": _definition_storage_key(self.namespace, definition.resolver_id),
                "resolver_version": int(definition.version),
                "contract_hash": resolver_definition_contract_hash(definition),
                "dependency_key": _dependency_storage_key(
                    self.namespace, definition.resolver_id, target
                ),
                "work_key": _work_storage_key(self.namespace, definition.resolver_id, target),
                "namespace": self.namespace,
                "resolver_id": definition.resolver_id,
                "target_json": _target_json(target),
            },
        )
        if not rows:
            raise StaleTrustResolverRegistryError(
                "cannot register dependency against a non-current resolver definition"
            )
        return {"storage_key": str(rows[0]["storage_key"]), "target": target}

    def capture_snapshot(
        self,
        definition: PurposeTrustResolverDefinition,
    ) -> TrustResolverRegistrySnapshot:
        rows = self._neo4j.execute(
            """
            MATCH (registry:MutationTrustResolverRegistry {storage_key:$registry_key})
            MATCH (definition:MutationTrustResolverDefinition {storage_key:$definition_key})
            RETURN coalesce(registry.epoch,0) AS epoch,
                   definition.current_version AS resolver_version,
                   definition.contract_hash AS contract_hash
            """,
            {
                "registry_key": _registry_storage_key(self.namespace),
                "definition_key": _definition_storage_key(self.namespace, definition.resolver_id),
            },
        )
        if len(rows) != 1:
            raise StaleTrustResolverRegistryError("shared trust resolver definition is missing")
        snapshot = TrustResolverRegistrySnapshot(
            epoch=int(rows[0].get("epoch", -1)),
            resolver_id=definition.resolver_id,
            resolver_version=int(rows[0].get("resolver_version", 0) or 0),
            contract_hash=str(rows[0].get("contract_hash") or ""),
        )
        snapshot.assert_matches(definition)
        return snapshot

    def run_fenced(
        self,
        definition: PurposeTrustResolverDefinition,
        resolver: PurposeTrustResolver,
        snapshot: TrustResolverRegistrySnapshot,
        operation: Callable[[], T],
    ) -> T:
        """Commit a resolver-derived outcome only while its definition is still shared-current."""
        definition.assert_resolver_matches(resolver)
        snapshot.assert_matches(definition)
        guard_token = uuid.uuid4().hex
        driver = self._neo4j._get_driver()
        with driver.session(database=self._neo4j.database) as session:
            tx = session.begin_transaction()
            try:
                rows = [
                    record.data()
                    for record in tx.run(
                        """
                        MATCH (registry:MutationTrustResolverRegistry {storage_key:$registry_key})
                        SET registry.active_guard_token=$guard_token
                        WITH registry
                        MATCH (definition:MutationTrustResolverDefinition {storage_key:$definition_key})
                        RETURN coalesce(registry.epoch,0) AS epoch,
                               definition.current_version AS resolver_version,
                               definition.contract_hash AS contract_hash
                        """,
                        registry_key=_registry_storage_key(self.namespace),
                        definition_key=_definition_storage_key(
                            self.namespace, definition.resolver_id
                        ),
                        guard_token=guard_token,
                    )
                ]
                if len(rows) != 1:
                    raise StaleTrustResolverRegistryError("shared trust resolver registry is missing")
                row = rows[0]
                if (
                    int(row.get("epoch", -1)) != snapshot.epoch
                    or int(row.get("resolver_version", 0) or 0) != snapshot.resolver_version
                    or str(row.get("contract_hash") or "") != snapshot.contract_hash
                ):
                    raise StaleTrustResolverRegistryError(
                        "trust resolver changed before derived outcome commit"
                    )

                result = operation()

                cleanup = [
                    record.data()
                    for record in tx.run(
                        """
                        MATCH (registry:MutationTrustResolverRegistry {storage_key:$registry_key})
                        WHERE registry.active_guard_token=$guard_token
                        REMOVE registry.active_guard_token
                        RETURN count(registry) AS n
                        """,
                        registry_key=_registry_storage_key(self.namespace),
                        guard_token=guard_token,
                    )
                ]
                if not cleanup or int(cleanup[0].get("n", 0) or 0) != 1:
                    raise RuntimeError("trust resolver guard cleanup failed")
                tx.commit()
                return result
            except Exception:
                tx.rollback()
                raise

    def pending_work(self, resolver_id: str) -> tuple[TrustResolverWork, ...]:
        rows = self._neo4j.execute(
            """
            MATCH (work:MutationTrustResolverWork {
                namespace:$namespace,
                resolver_id:$resolver_id
            })
            WHERE coalesce(work.generation,0) > coalesce(work.completed_generation,0)
            RETURN work.resolver_version AS resolver_version,
                   work.target_json AS target_json,
                   work.generation AS generation,
                   coalesce(work.completed_generation,0) AS completed_generation
            ORDER BY work.target_json ASC
            """,
            {"namespace": self.namespace, "resolver_id": resolver_id},
        )
        return tuple(
            TrustResolverWork(
                resolver_id=resolver_id,
                resolver_version=int(row["resolver_version"]),
                target=_target_from_json(str(row["target_json"])),
                generation=int(row["generation"]),
                completed_generation=int(row["completed_generation"]),
            )
            for row in rows
        )

    def complete_work(
        self,
        work: TrustResolverWork,
        *,
        definition: PurposeTrustResolverDefinition,
    ) -> None:
        """CAS-complete one rebuild only if generation and current resolver version still match."""
        if work.resolver_id != definition.resolver_id:
            raise ValueError("trust resolver work belongs to a different resolver")
        rows = self._neo4j.execute(
            """
            MATCH (definition:MutationTrustResolverDefinition {storage_key:$definition_key})
            MATCH (work:MutationTrustResolverWork {storage_key:$work_key})
            WHERE definition.current_version=$resolver_version
              AND definition.contract_hash=$contract_hash
              AND work.resolver_version=$resolver_version
              AND work.generation=$generation
            SET work.completed_generation=$generation,
                work.completed_at=datetime()
            RETURN count(work) AS n
            """,
            {
                "definition_key": _definition_storage_key(self.namespace, definition.resolver_id),
                "work_key": _work_storage_key(self.namespace, work.resolver_id, work.target),
                "resolver_version": int(definition.version),
                "contract_hash": resolver_definition_contract_hash(definition),
                "generation": int(work.generation),
            },
        )
        if not rows or int(rows[0].get("n", 0) or 0) != 1:
            raise StaleTrustResolverRegistryError(
                "trust resolver work changed before rebuild completion"
            )
