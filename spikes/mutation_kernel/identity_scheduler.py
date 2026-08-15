"""Identity-driven graph invalidation and atomic receipt guards for the mutation spike.

Identity migration and graph projection are intentionally decoupled commits. A durable snapshot of
receipt -> current-identity mappings lets a scheduler discover changes after a crash and dirty only
extension-declared graph materializer slots. The graph write itself is guarded by the exact receipt
resolutions used by the worker, so a stale pre-migration worker cannot restore topology after an
identity correction even if scheduling has not caught up yet.

This file deliberately wraps/duplicates a small part of the spike graph write path. If the experiment
survives, receipt guards belong in the supported generic materialization interface rather than in a
specialized wrapper.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import uuid
from typing import Any, Callable, Sequence

from .graph import EdgeSet, GraphOutcome, Neo4jGraphMaterializer


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_parts(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(str(part).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


@dataclass(frozen=True)
class ReceiptIdentityGuard:
    receipt_storage_key: str
    source_key: str
    expected_current_entity_id: str

    def __post_init__(self) -> None:
        if not self.receipt_storage_key.strip():
            raise ValueError("ReceiptIdentityGuard.receipt_storage_key must be non-empty")
        if not self.source_key.strip():
            raise ValueError("ReceiptIdentityGuard.source_key must be non-empty")
        if not self.expected_current_entity_id.strip():
            raise ValueError("ReceiptIdentityGuard.expected_current_entity_id must be non-empty")


@dataclass(frozen=True, order=True)
class GraphMaterializerTarget:
    materializer_id: str
    slot_key: str

    def __post_init__(self) -> None:
        if not self.materializer_id.strip() or not self.slot_key.strip():
            raise ValueError("GraphMaterializerTarget fields must be non-empty")


@dataclass(frozen=True)
class IdentityMappingChange:
    entity_kind: str
    original_entity_id: str
    source_key: str
    before_current_entity_id: str | None
    after_current_entity_id: str | None


ChangeMapper = Callable[[tuple[IdentityMappingChange, ...]], Sequence[GraphMaterializerTarget]]


@dataclass(frozen=True)
class IdentityDependencyDefinition:
    definition_id: str
    version: int
    entity_kind: str
    mapper: ChangeMapper

    def __post_init__(self) -> None:
        if not self.definition_id.strip() or not self.entity_kind.strip():
            raise ValueError("IdentityDependencyDefinition IDs must be non-empty")
        if self.version < 1:
            raise ValueError("IdentityDependencyDefinition.version must be >= 1")


@dataclass(frozen=True)
class IdentityDirtyGraphWork:
    definition_id: str
    definition_version: int
    target: GraphMaterializerTarget
    generation: int
    projected_generation: int
    identity_fingerprint: str

    def __post_init__(self) -> None:
        if self.generation < 1 or self.projected_generation < 0:
            raise ValueError("graph work generations are invalid")
        if not self.identity_fingerprint.strip():
            raise ValueError("identity_fingerprint must be non-empty")


@dataclass(frozen=True)
class IdentityDependencySyncResult:
    definition_id: str
    definition_version: int
    changed_mapping_count: int
    target_count: int
    dirtied_target_count: int
    identity_fingerprint: str


@dataclass(frozen=True)
class GuardedGraphDerivation:
    outcome: GraphOutcome
    guards: tuple[ReceiptIdentityGuard, ...]


class GuardedNeo4jGraphMaterializer(Neo4jGraphMaterializer):
    """Graph materializer whose write verifies receipt identity inside the same Cypher query."""

    def reconcile_edges_guarded(
        self,
        outcome: GraphOutcome,
        guards: Sequence[ReceiptIdentityGuard],
    ) -> dict[str, object]:
        guards = tuple(guards)
        guard_keys = [row.receipt_storage_key for row in guards]
        if len(guard_keys) != len(set(guard_keys)):
            raise ValueError("receipt identity guards must be unique")
        if not guards:
            return super().reconcile_edges(outcome)

        edges = outcome.edges if isinstance(outcome, EdgeSet) else ()
        for edge in edges:
            self.schema.validate_edge(edge)

        edge_rows: list[dict[str, Any]] = []
        for edge in edges:
            source_key = self._entity_storage_key(edge.source_entity_kind, edge.source_entity_id)
            target_key = self._entity_storage_key(edge.target_entity_kind, edge.target_entity_id)
            storage_key = _hash_parts(
                self.namespace,
                outcome.materializer_id,
                outcome.slot_key,
                edge.edge_kind,
                edge.source_entity_kind,
                edge.source_entity_id,
                edge.target_entity_kind,
                edge.target_entity_id,
                edge.valid_from,
            )
            edge_rows.append(
                {
                    "storage_key": storage_key,
                    "source_storage_key": source_key,
                    "source_entity_kind": edge.source_entity_kind,
                    "target_storage_key": target_key,
                    "target_entity_kind": edge.target_entity_kind,
                    "edge_kind": edge.edge_kind,
                    "valid_from": edge.valid_from,
                    "contributor_ids_json": _stable_json(list(edge.contributor_ids)),
                    "effective_authority": edge.effective_authority,
                    "confidence": float(edge.confidence),
                    "properties_json": _stable_json(dict(edge.properties)),
                }
            )

        guard_rows = [
            {
                "receipt_storage_key": row.receipt_storage_key,
                "source_key": row.source_key,
                "expected_current_entity_id": row.expected_current_entity_id,
            }
            for row in guards
        ]
        desired_keys = [str(row["storage_key"]) for row in edge_rows]
        projection_hash = _hash_parts(
            _stable_json(
                {
                    "materializer_id": outcome.materializer_id,
                    "slot_key": outcome.slot_key,
                    "effective_at": outcome.effective_at,
                    "status": "edges" if isinstance(outcome, EdgeSet) else "abstention",
                    "reason": None if isinstance(outcome, EdgeSet) else outcome.reason,
                    "assertion_ids": list(outcome.assertion_ids),
                    "edges": edge_rows,
                    "identity_guards": guard_rows,
                }
            )
        )

        rows = self._neo4j.execute(
            """
            UNWIND $guards AS guard
            MATCH (receipt:MutationEntityReceipt {storage_key:guard.receipt_storage_key})
                  -[:CURRENT_IDENTITY]->(current_identity:MutationEntity)
            WHERE receipt.namespace=$namespace
              AND current_identity.namespace=$namespace
            WITH guard, collect(current_identity.entity_id) AS current_ids
            WITH collect({expected:guard.expected_current_entity_id, current_ids:current_ids})
                 AS guard_checks
            WHERE size(guard_checks)=size($guards)
              AND all(check IN guard_checks WHERE
                  size(check.current_ids)=1
                  AND check.current_ids[0]=check.expected
              )
            WITH 1 AS identity_guard_ok
            UNWIND CASE WHEN size($edges)=0 THEN [null] ELSE $edges END AS candidate
            OPTIONAL MATCH (source:MutationEntity)
              WHERE candidate IS NOT NULL
                AND source.storage_key=candidate.source_storage_key
                AND source.namespace=$namespace
                AND source.entity_kind=candidate.source_entity_kind
            OPTIONAL MATCH (target:MutationEntity)
              WHERE candidate IS NOT NULL
                AND target.storage_key=candidate.target_storage_key
                AND target.namespace=$namespace
                AND target.entity_kind=candidate.target_entity_kind
            WITH collect({edge:candidate, source:source, target:target}) AS candidates
            WITH [row IN candidates WHERE row.edge IS NOT NULL] AS edge_rows
            WITH edge_rows,
                 size([row IN edge_rows WHERE row.source IS NULL OR row.target IS NULL]) AS missing
            WHERE missing=0
            OPTIONAL MATCH ()-[current:MUTATION_EDGE]->()
              WHERE current.namespace=$namespace
                AND current.materializer_id=$materializer_id
                AND current.slot_key=$slot_key
                AND coalesce(current.active,false)=true
            WITH edge_rows, collect(current) AS current_edges
            WITH edge_rows,
                 [r IN current_edges WHERE NOT r.storage_key IN $desired_keys] AS retiring,
                 [r IN current_edges | r.storage_key] AS previous_active_keys
            FOREACH (r IN retiring |
                SET r.active=false,
                    r.retired_at=$effective_at,
                    r.updated_at=datetime()
            )
            WITH edge_rows, retiring, previous_active_keys
            CALL {
                WITH edge_rows
                UNWIND edge_rows AS row
                WITH row.edge AS edge, row.source AS source, row.target AS target
                MERGE (source)-[rel:MUTATION_EDGE {storage_key:edge.storage_key}]->(target)
                  ON CREATE SET rel.created_at=datetime()
                SET rel.namespace=$namespace,
                    rel.edge_kind=edge.edge_kind,
                    rel.materializer_id=$materializer_id,
                    rel.slot_key=$slot_key,
                    rel.active=true,
                    rel.valid_from=edge.valid_from,
                    rel.retired_at=null,
                    rel.contributor_ids_json=edge.contributor_ids_json,
                    rel.effective_authority=edge.effective_authority,
                    rel.confidence=edge.confidence,
                    rel.properties_json=edge.properties_json,
                    rel.projection_hash=$projection_hash,
                    rel.updated_at=datetime()
                RETURN count(rel) AS upserted
            }
            RETURN previous_active_keys AS previous_active_keys,
                   size(retiring) AS retired,
                   upserted AS upserted
            """,
            {
                "namespace": self.namespace,
                "materializer_id": outcome.materializer_id,
                "slot_key": outcome.slot_key,
                "effective_at": outcome.effective_at,
                "desired_keys": desired_keys,
                "projection_hash": projection_hash,
                "edges": edge_rows,
                "guards": guard_rows,
            },
        )
        if not rows:
            raise ValueError(
                "identity guard changed before graph write or graph endpoints are unresolved"
            )
        row = rows[0]
        previous_active = tuple(str(value) for value in (row.get("previous_active_keys") or []))
        return {
            "changed": set(previous_active) != set(desired_keys),
            "retired": int(row.get("retired", 0) or 0),
            "upserted": int(row.get("upserted", 0) or 0),
            "projection_hash": projection_hash,
            "active_edge_count": len(desired_keys),
            "status": "edges" if isinstance(outcome, EdgeSet) else "abstention",
        }


class IdentityDependencyScheduler:
    """Discover graph work by diffing durable receipt->current-identity state."""

    def __init__(self, neo4j: Any, *, namespace: str) -> None:
        if not namespace.strip():
            raise ValueError("IdentityDependencyScheduler.namespace must be non-empty")
        self._neo4j = neo4j
        self.namespace = namespace.strip()

    def activate(self) -> None:
        self._neo4j.execute(
            "CREATE CONSTRAINT mutation_identity_dependency_state_storage_key IF NOT EXISTS "
            "FOR (s:MutationIdentityDependencyState) REQUIRE s.storage_key IS UNIQUE"
        )
        self._neo4j.execute(
            "CREATE CONSTRAINT mutation_identity_graph_work_storage_key IF NOT EXISTS "
            "FOR (w:MutationIdentityGraphWork) REQUIRE w.storage_key IS UNIQUE"
        )

    def _state_key(self, definition: IdentityDependencyDefinition) -> str:
        return _hash_parts(self.namespace, "identity-dependency-state", definition.definition_id)

    def _work_key(
        self,
        definition: IdentityDependencyDefinition,
        target: GraphMaterializerTarget,
    ) -> str:
        return _hash_parts(
            self.namespace,
            "identity-graph-work",
            definition.definition_id,
            target.materializer_id,
            target.slot_key,
        )

    def _snapshot(self, entity_kind: str) -> list[dict[str, str]]:
        rows = self._neo4j.execute(
            """
            MATCH (receipt:MutationEntityReceipt)-[:EVIDENCE_FOR]->(original:MutationEntity)
            WHERE receipt.namespace=$namespace
              AND original.namespace=$namespace
              AND original.entity_kind=$entity_kind
            OPTIONAL MATCH (receipt)-[:CURRENT_IDENTITY]->(current:MutationEntity)
            RETURN original.entity_id AS original_entity_id,
                   receipt.source_key AS source_key,
                   current.entity_id AS current_entity_id
            ORDER BY original_entity_id ASC, source_key ASC
            """,
            {"namespace": self.namespace, "entity_kind": entity_kind},
        )
        return [
            {
                "original_entity_id": str(row.get("original_entity_id") or ""),
                "source_key": str(row.get("source_key") or ""),
                "current_entity_id": str(row.get("current_entity_id") or ""),
            }
            for row in rows
        ]

    def _load_state(
        self,
        definition: IdentityDependencyDefinition,
    ) -> tuple[str, list[dict[str, str]]]:
        rows = self._neo4j.execute(
            """
            MATCH (state:MutationIdentityDependencyState {storage_key:$storage_key})
            RETURN state.identity_fingerprint AS identity_fingerprint,
                   state.snapshot_json AS snapshot_json
            """,
            {"storage_key": self._state_key(definition)},
        )
        if not rows:
            return "", []
        return (
            str(rows[0].get("identity_fingerprint") or ""),
            list(json.loads(str(rows[0].get("snapshot_json") or "[]"))),
        )

    def bootstrap(self, definition: IdentityDependencyDefinition) -> str:
        snapshot = self._snapshot(definition.entity_kind)
        fingerprint = _hash_parts(_stable_json(snapshot))
        self._neo4j.execute(
            """
            MERGE (state:MutationIdentityDependencyState {storage_key:$storage_key})
              ON CREATE SET state.namespace=$namespace,
                            state.definition_id=$definition_id,
                            state.definition_version=$definition_version,
                            state.entity_kind=$entity_kind,
                            state.identity_fingerprint=$identity_fingerprint,
                            state.snapshot_json=$snapshot_json,
                            state.created_at=datetime()
            RETURN state.identity_fingerprint AS identity_fingerprint
            """,
            {
                "storage_key": self._state_key(definition),
                "namespace": self.namespace,
                "definition_id": definition.definition_id,
                "definition_version": int(definition.version),
                "entity_kind": definition.entity_kind,
                "identity_fingerprint": fingerprint,
                "snapshot_json": _stable_json(snapshot),
            },
        )
        return fingerprint

    @staticmethod
    def _changes(
        *,
        entity_kind: str,
        before: Sequence[dict[str, str]],
        after: Sequence[dict[str, str]],
    ) -> tuple[IdentityMappingChange, ...]:
        def keyed(rows: Sequence[dict[str, str]]) -> dict[tuple[str, str], str]:
            return {
                (
                    str(row.get("original_entity_id") or ""),
                    str(row.get("source_key") or ""),
                ): str(row.get("current_entity_id") or "")
                for row in rows
            }

        old = keyed(before)
        new = keyed(after)
        changes: list[IdentityMappingChange] = []
        for key in sorted(set(old) | set(new)):
            previous = old.get(key)
            current = new.get(key)
            if previous == current:
                continue
            changes.append(
                IdentityMappingChange(
                    entity_kind=entity_kind,
                    original_entity_id=key[0],
                    source_key=key[1],
                    before_current_entity_id=previous or None,
                    after_current_entity_id=current or None,
                )
            )
        return tuple(changes)

    def sync(self, definition: IdentityDependencyDefinition) -> IdentityDependencySyncResult:
        previous_fingerprint, before = self._load_state(definition)
        after = self._snapshot(definition.entity_kind)
        fingerprint = _hash_parts(_stable_json(after))
        if previous_fingerprint == fingerprint:
            return IdentityDependencySyncResult(
                definition_id=definition.definition_id,
                definition_version=definition.version,
                changed_mapping_count=0,
                target_count=0,
                dirtied_target_count=0,
                identity_fingerprint=fingerprint,
            )

        changes = self._changes(
            entity_kind=definition.entity_kind,
            before=before,
            after=after,
        )
        targets = tuple(sorted(set(definition.mapper(changes))))
        target_rows = [
            {
                "storage_key": self._work_key(definition, target),
                "materializer_id": target.materializer_id,
                "slot_key": target.slot_key,
            }
            for target in targets
        ]
        write_token = uuid.uuid4().hex

        rows = self._neo4j.execute(
            """
            MERGE (state:MutationIdentityDependencyState {storage_key:$state_storage_key})
              ON CREATE SET state.namespace=$namespace,
                            state.definition_id=$definition_id,
                            state.entity_kind=$entity_kind,
                            state.identity_fingerprint="",
                            state.snapshot_json="[]",
                            state.created_at=datetime()
            WITH state
            WHERE coalesce(state.identity_fingerprint,"")=$previous_fingerprint
            SET state.definition_version=$definition_version,
                state.identity_fingerprint=$identity_fingerprint,
                state.snapshot_json=$snapshot_json,
                state.updated_at=datetime()
            FOREACH (target IN $targets |
                MERGE (work:MutationIdentityGraphWork {storage_key:target.storage_key})
                  ON CREATE SET work.namespace=$namespace,
                                work.definition_id=$definition_id,
                                work.materializer_id=target.materializer_id,
                                work.slot_key=target.slot_key,
                                work.generation=0,
                                work.projected_generation=0,
                                work.created_at=datetime()
                SET work.definition_version=$definition_version,
                    work.generation=coalesce(work.generation,0)+1,
                    work.identity_fingerprint=$identity_fingerprint,
                    work.dirty_token=$write_token,
                    work.dirty_at=datetime()
            )
            RETURN state.identity_fingerprint AS identity_fingerprint,
                   size($targets) AS dirtied
            """,
            {
                "state_storage_key": self._state_key(definition),
                "namespace": self.namespace,
                "definition_id": definition.definition_id,
                "definition_version": int(definition.version),
                "entity_kind": definition.entity_kind,
                "previous_fingerprint": previous_fingerprint,
                "identity_fingerprint": fingerprint,
                "snapshot_json": _stable_json(after),
                "targets": target_rows,
                "write_token": write_token,
            },
        )
        if not rows:
            raise ValueError(
                "identity dependency state changed concurrently; retry from a fresh snapshot"
            )
        return IdentityDependencySyncResult(
            definition_id=definition.definition_id,
            definition_version=definition.version,
            changed_mapping_count=len(changes),
            target_count=len(targets),
            dirtied_target_count=int(rows[0].get("dirtied", 0) or 0),
            identity_fingerprint=fingerprint,
        )

    def load_dirty(self, *, limit: int = 100) -> list[IdentityDirtyGraphWork]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        rows = self._neo4j.execute(
            """
            MATCH (work:MutationIdentityGraphWork {namespace:$namespace})
            WHERE coalesce(work.generation,0) > coalesce(work.projected_generation,0)
            RETURN work.definition_id AS definition_id,
                   work.definition_version AS definition_version,
                   work.materializer_id AS materializer_id,
                   work.slot_key AS slot_key,
                   work.generation AS generation,
                   coalesce(work.projected_generation,0) AS projected_generation,
                   work.identity_fingerprint AS identity_fingerprint
            ORDER BY work.dirty_at ASC, work.storage_key ASC
            LIMIT $limit
            """,
            {"namespace": self.namespace, "limit": int(limit)},
        )
        return [
            IdentityDirtyGraphWork(
                definition_id=str(row["definition_id"]),
                definition_version=int(row["definition_version"]),
                target=GraphMaterializerTarget(
                    materializer_id=str(row["materializer_id"]),
                    slot_key=str(row["slot_key"]),
                ),
                generation=int(row["generation"]),
                projected_generation=int(row.get("projected_generation", 0) or 0),
                identity_fingerprint=str(row["identity_fingerprint"]),
            )
            for row in rows
        ]

    def mark_projected(self, dirty: IdentityDirtyGraphWork, *, projection_hash: str) -> bool:
        rows = self._neo4j.execute(
            """
            MATCH (work:MutationIdentityGraphWork {
                namespace:$namespace,
                definition_id:$definition_id,
                materializer_id:$materializer_id,
                slot_key:$slot_key
            })
            WITH work,
                 work.generation=$generation
                 AND work.identity_fingerprint=$identity_fingerprint AS can_apply
            FOREACH (_ IN CASE WHEN can_apply THEN [1] ELSE [] END |
                SET work.projected_generation=$generation,
                    work.projected_identity_fingerprint=$identity_fingerprint,
                    work.projection_hash=$projection_hash,
                    work.projected_at=datetime()
            )
            RETURN can_apply AS applied
            """,
            {
                "namespace": self.namespace,
                "definition_id": dirty.definition_id,
                "materializer_id": dirty.target.materializer_id,
                "slot_key": dirty.target.slot_key,
                "generation": int(dirty.generation),
                "identity_fingerprint": dirty.identity_fingerprint,
                "projection_hash": projection_hash,
            },
        )
        return bool(rows and rows[0].get("applied"))
