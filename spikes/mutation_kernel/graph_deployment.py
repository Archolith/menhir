"""Shared deployment fencing for graph extension definitions.

Projection definitions already proved a shared registry epoch can prevent stale mapper writes. This
experiment applies the same idea to graph-facing extension contracts: entity identity resolvers, edge
schemas, and graph materializers.

Rather than duplicating every persistence query, ``run_fenced`` holds a write lock on one shared
registry node while an already-atomic graph operation runs through its normal repository path. A
publisher must write that same registry node to advance the epoch, so the two operations serialize:

* publisher commits first -> stale writer acquires the lock later, sees a newer epoch, and never runs;
* writer acquires the lock first -> its old-version operation legitimately completes before the
  publication; the publisher waits and advances the epoch afterward.

This is a correctness experiment, not a performance recommendation. A production design could shard
registry epochs by extension/definition to reduce upgrade-time contention.
"""

from __future__ import annotations

from dataclasses import dataclass
import uuid
from typing import Any, Callable, Sequence, TypeVar

from .neo4j_store import _hash_parts, _stable_json


class StaleGraphRegistryError(RuntimeError):
    """The caller's graph-extension snapshot is no longer shared-current."""


@dataclass(frozen=True, order=True)
class EntityKindContract:
    kind_id: str
    version: int

    def __post_init__(self) -> None:
        if not self.kind_id.strip() or self.version < 1:
            raise ValueError("entity kind contract requires non-empty ID and version >= 1")


@dataclass(frozen=True, order=True)
class EdgeKindContract:
    kind_id: str
    version: int
    source_kinds: tuple[str, ...]
    target_kinds: tuple[str, ...]
    temporal: bool
    provenance_required: bool

    def __post_init__(self) -> None:
        if not self.kind_id.strip() or self.version < 1:
            raise ValueError("edge kind contract requires non-empty ID and version >= 1")
        if self.source_kinds != tuple(sorted(self.source_kinds)):
            raise ValueError("source kinds must be canonical")
        if self.target_kinds != tuple(sorted(self.target_kinds)):
            raise ValueError("target kinds must be canonical")
        if not self.source_kinds or not self.target_kinds:
            raise ValueError("edge kind contract requires source and target kinds")


@dataclass(frozen=True, order=True)
class GraphMaterializerContract:
    materializer_id: str
    version: int

    def __post_init__(self) -> None:
        if not self.materializer_id.strip() or self.version < 1:
            raise ValueError("materializer contract requires non-empty ID and version >= 1")


@dataclass(frozen=True)
class GraphExtensionDefinition:
    definition_id: str
    version: int
    entity_kinds: tuple[EntityKindContract, ...]
    edge_kinds: tuple[EdgeKindContract, ...]
    materializers: tuple[GraphMaterializerContract, ...]

    def __post_init__(self) -> None:
        if not self.definition_id.strip() or self.version < 1:
            raise ValueError("graph extension definition requires ID and version >= 1")
        if self.entity_kinds != tuple(sorted(self.entity_kinds)):
            raise ValueError("entity kind contracts must be canonical")
        if self.edge_kinds != tuple(sorted(self.edge_kinds)):
            raise ValueError("edge kind contracts must be canonical")
        if self.materializers != tuple(sorted(self.materializers)):
            raise ValueError("materializer contracts must be canonical")
        for rows, label in (
            (self.entity_kinds, "entity kind"),
            (self.edge_kinds, "edge kind"),
            (self.materializers, "materializer"),
        ):
            ids = [
                row.kind_id if hasattr(row, "kind_id") else row.materializer_id
                for row in rows
            ]
            if len(ids) != len(set(ids)):
                raise ValueError(f"graph definition {label} IDs must be unique")


@dataclass(frozen=True)
class GraphRegistrySnapshot:
    epoch: int
    definitions: tuple[tuple[str, int, str], ...]

    def __post_init__(self) -> None:
        if self.epoch < 0:
            raise ValueError("graph registry epoch must be >= 0")

    def assert_matches(self, definitions: Sequence[GraphExtensionDefinition]) -> None:
        local = tuple(
            sorted(
                (
                    definition.definition_id,
                    definition.version,
                    graph_definition_contract_hash(definition),
                )
                for definition in definitions
            )
        )
        if local != self.definitions:
            raise StaleGraphRegistryError(
                "in-process graph extension definitions do not match the captured shared snapshot"
            )


def _registry_storage_key(namespace: str) -> str:
    return _hash_parts(namespace, "graph-extension-registry")


def _definition_storage_key(namespace: str, definition_id: str) -> str:
    return _hash_parts(namespace, "graph-extension-definition", definition_id)


def graph_definition_contract_hash(definition: GraphExtensionDefinition) -> str:
    """Hash declarative contract metadata; callables still require a semantic version bump."""
    return _hash_parts(
        _stable_json(
            {
                "definition_id": definition.definition_id,
                "version": definition.version,
                "entity_kinds": [
                    {"kind_id": row.kind_id, "version": row.version}
                    for row in definition.entity_kinds
                ],
                "edge_kinds": [
                    {
                        "kind_id": row.kind_id,
                        "version": row.version,
                        "source_kinds": list(row.source_kinds),
                        "target_kinds": list(row.target_kinds),
                        "temporal": row.temporal,
                        "provenance_required": row.provenance_required,
                    }
                    for row in definition.edge_kinds
                ],
                "materializers": [
                    {
                        "materializer_id": row.materializer_id,
                        "version": row.version,
                    }
                    for row in definition.materializers
                ],
            }
        )
    )


T = TypeVar("T")


class GraphDeploymentCoordinator:
    """Shared graph-definition registry plus a transaction-held stale-process gate."""

    def __init__(self, neo4j: Any, *, namespace: str) -> None:
        if not namespace.strip():
            raise ValueError("GraphDeploymentCoordinator.namespace must be non-empty")
        self._neo4j = neo4j
        self.namespace = namespace.strip()

    def activate(self) -> None:
        self._neo4j.execute(
            "CREATE CONSTRAINT mutation_graph_registry_storage_key IF NOT EXISTS "
            "FOR (r:MutationGraphRegistry) REQUIRE r.storage_key IS UNIQUE"
        )
        self._neo4j.execute(
            "CREATE CONSTRAINT mutation_graph_definition_storage_key IF NOT EXISTS "
            "FOR (d:MutationGraphDefinition) REQUIRE d.storage_key IS UNIQUE"
        )

    def publish_definition(self, definition: GraphExtensionDefinition) -> dict[str, object]:
        contract_hash = graph_definition_contract_hash(definition)
        rows = self._neo4j.execute(
            """
            MERGE (registry:MutationGraphRegistry {storage_key:$registry_storage_key})
              ON CREATE SET registry.namespace=$namespace,
                            registry.epoch=0,
                            registry.created_at=datetime()
            MERGE (definition:MutationGraphDefinition {storage_key:$definition_storage_key})
              ON CREATE SET definition.namespace=$namespace,
                            definition.definition_id=$definition_id,
                            definition.current_version=0,
                            definition.created_at=datetime()
            WITH registry, definition,
                 coalesce(definition.current_version,0) AS old_version,
                 definition.contract_hash AS old_hash
            WITH registry, definition, old_version, old_hash,
                 CASE
                   WHEN old_version > $definition_version THEN 'stale'
                   WHEN old_version = $definition_version
                        AND old_hash IS NOT NULL
                        AND old_hash <> $contract_hash THEN 'collision'
                   WHEN old_version = $definition_version THEN 'same'
                   ELSE 'advance'
                 END AS action
            FOREACH (_ IN CASE WHEN action='advance' THEN [1] ELSE [] END |
                SET definition.current_version=$definition_version,
                    definition.contract_hash=$contract_hash,
                    definition.contract_json=$contract_json,
                    definition.activated_at=datetime(),
                    registry.epoch=coalesce(registry.epoch,0)+1,
                    registry.updated_at=datetime()
            )
            RETURN action AS action,
                   coalesce(registry.epoch,0) AS epoch,
                   coalesce(definition.current_version,0) AS current_version
            """,
            {
                "registry_storage_key": _registry_storage_key(self.namespace),
                "definition_storage_key": _definition_storage_key(
                    self.namespace, definition.definition_id
                ),
                "namespace": self.namespace,
                "definition_id": definition.definition_id,
                "definition_version": int(definition.version),
                "contract_hash": contract_hash,
                "contract_json": _stable_json(
                    {
                        "entity_kinds": [row.__dict__ for row in definition.entity_kinds],
                        "edge_kinds": [row.__dict__ for row in definition.edge_kinds],
                        "materializers": [row.__dict__ for row in definition.materializers],
                    }
                ),
            },
        )
        if not rows:
            raise RuntimeError("graph definition publish returned no result")
        row = rows[0]
        action = str(row.get("action") or "")
        if action == "stale":
            raise StaleGraphRegistryError(
                f"cannot publish stale graph definition {definition.definition_id} "
                f"v{definition.version}"
            )
        if action == "collision":
            raise ValueError(
                "graph extension contract changed without a definition version bump: "
                f"{definition.definition_id} v{definition.version}"
            )
        return {
            "action": action,
            "epoch": int(row.get("epoch",0) or 0),
            "current_version": int(row.get("current_version",0) or 0),
        }

    def capture_snapshot(
        self,
        definitions: Sequence[GraphExtensionDefinition],
    ) -> GraphRegistrySnapshot:
        registry_rows = self._neo4j.execute(
            """
            MATCH (registry:MutationGraphRegistry {storage_key:$storage_key})
            RETURN coalesce(registry.epoch,0) AS epoch
            """,
            {"storage_key": _registry_storage_key(self.namespace)},
        )
        if not registry_rows:
            raise StaleGraphRegistryError("shared graph extension registry has not been published")
        definition_rows = self._neo4j.execute(
            """
            MATCH (definition:MutationGraphDefinition {namespace:$namespace})
            WHERE coalesce(definition.current_version,0)>0
            RETURN definition.definition_id AS definition_id,
                   definition.current_version AS definition_version,
                   definition.contract_hash AS contract_hash
            ORDER BY definition_id ASC
            """,
            {"namespace": self.namespace},
        )
        snapshot = GraphRegistrySnapshot(
            epoch=int(registry_rows[0].get("epoch",0) or 0),
            definitions=tuple(
                (
                    str(row["definition_id"]),
                    int(row["definition_version"]),
                    str(row["contract_hash"]),
                )
                for row in definition_rows
            ),
        )
        snapshot.assert_matches(definitions)
        return snapshot

    def run_fenced(
        self,
        definitions: Sequence[GraphExtensionDefinition],
        snapshot: GraphRegistrySnapshot,
        operation: Callable[[], T],
    ) -> T:
        """Run one already-atomic graph operation while publication is serialized behind us."""
        snapshot.assert_matches(definitions)
        guard_token = uuid.uuid4().hex
        driver = self._neo4j._get_driver()
        with driver.session(database=self._neo4j.database) as session:
            tx = session.begin_transaction()
            try:
                rows = [
                    record.data()
                    for record in tx.run(
                        """
                        MATCH (registry:MutationGraphRegistry {storage_key:$storage_key})
                        SET registry.active_guard_token=$guard_token
                        RETURN coalesce(registry.epoch,0) AS epoch
                        """,
                        storage_key=_registry_storage_key(self.namespace),
                        guard_token=guard_token,
                    )
                ]
                if len(rows) != 1:
                    raise StaleGraphRegistryError("shared graph registry is missing")
                current_epoch = int(rows[0].get("epoch",-1))
                if current_epoch != snapshot.epoch:
                    raise StaleGraphRegistryError(
                        f"graph registry epoch changed: snapshot={snapshot.epoch} "
                        f"current={current_epoch}"
                    )

                result = operation()

                cleanup = [
                    record.data()
                    for record in tx.run(
                        """
                        MATCH (registry:MutationGraphRegistry {storage_key:$storage_key})
                        WHERE registry.active_guard_token=$guard_token
                        REMOVE registry.active_guard_token
                        RETURN count(registry) AS n
                        """,
                        storage_key=_registry_storage_key(self.namespace),
                        guard_token=guard_token,
                    )
                ]
                if not cleanup or int(cleanup[0].get("n",0) or 0) != 1:
                    raise RuntimeError("graph registry guard cleanup failed")
                tx.commit()
                return result
            except Exception:
                tx.rollback()
                raise
