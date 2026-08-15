"""Generic entity-identity evolution for the mutation-kernel research spike.

Source receipts remain immutable evidence of what a source originally referred to. Identity migrations
change only a separate ``CURRENT_IDENTITY`` resolution edge. This lets an extension merge or split
previously materialized identities without rewriting source observations or deleting historical
entities.

The migration plan is extension-owned: core-like machinery does not decide which receipts should
move. It validates that every source receipt and target entity exists, applies the supplied mapping,
and keeps the prior ``EVIDENCE_FOR`` topology untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

from .graph import Neo4jGraphMaterializer, ResolvedEntity


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_parts(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(str(part).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


@dataclass(frozen=True)
class ReceiptIdentityAssignment:
    """Move one immutable source receipt to a new current identity."""

    source_entity_id: str
    source_key: str
    target_entity_id: str

    def __post_init__(self) -> None:
        for name in ("source_entity_id", "source_key", "target_entity_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"ReceiptIdentityAssignment.{name} must be non-empty")


@dataclass(frozen=True)
class IdentityMigrationPlan:
    """Extension-supplied receipt reassignment for one entity kind."""

    migration_id: str
    version: int
    entity_kind: str
    effective_at: str
    reason: str
    assignments: tuple[ReceiptIdentityAssignment, ...]

    def __post_init__(self) -> None:
        for name in ("migration_id", "entity_kind", "effective_at", "reason"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"IdentityMigrationPlan.{name} must be non-empty")
        if self.version < 1:
            raise ValueError("IdentityMigrationPlan.version must be >= 1")
        if not self.assignments:
            raise ValueError("IdentityMigrationPlan requires at least one receipt assignment")
        identities = [(row.source_entity_id, row.source_key) for row in self.assignments]
        if len(identities) != len(set(identities)):
            raise ValueError("IdentityMigrationPlan cannot assign the same source receipt twice")


@dataclass(frozen=True)
class IdentityResolution:
    """Current interpretation of a durable historical entity reference."""

    status: str
    entities: tuple[ResolvedEntity, ...]

    def __post_init__(self) -> None:
        if self.status not in {"resolved", "ambiguous", "missing"}:
            raise ValueError("IdentityResolution.status must be resolved, ambiguous, or missing")
        if self.status == "resolved" and len(self.entities) != 1:
            raise ValueError("resolved identity requires exactly one entity")
        if self.status == "ambiguous" and len(self.entities) < 2:
            raise ValueError("ambiguous identity requires at least two entities")
        if self.status == "missing" and self.entities:
            raise ValueError("missing identity cannot contain entities")

    @property
    def entity(self) -> ResolvedEntity | None:
        return self.entities[0] if self.status == "resolved" else None


@dataclass(frozen=True)
class IdentityMigrationResult:
    migration_id: str
    version: int
    assignment_count: int
    touched_entity_count: int
    replay: bool


class Neo4jIdentityEvolution:
    """Versioned current-identity overlays on immutable entity receipts."""

    def __init__(self, graph: Neo4jGraphMaterializer) -> None:
        self.graph = graph
        self._neo4j = graph._neo4j  # spike-local coupling; not a proposed public API
        self.namespace = graph.namespace

    def activate(self) -> None:
        self._neo4j.execute(
            "CREATE CONSTRAINT mutation_identity_migration_storage_key IF NOT EXISTS "
            "FOR (m:MutationIdentityMigration) REQUIRE m.storage_key IS UNIQUE"
        )
        self.bootstrap_current_identity()

    def bootstrap_current_identity(self) -> int:
        """Give receipts with no current overlay their original evidence-backed identity."""
        rows = self._neo4j.execute(
            """
            MATCH (receipt:MutationEntityReceipt)-[:EVIDENCE_FOR]->(entity:MutationEntity)
            WHERE receipt.namespace=$namespace
              AND entity.namespace=$namespace
              AND NOT EXISTS {
                  MATCH (receipt)-[:CURRENT_IDENTITY]->(:MutationEntity)
              }
            MERGE (receipt)-[:CURRENT_IDENTITY]->(entity)
            SET entity.identity_active=true
            RETURN count(receipt) AS n
            """,
            {"namespace": self.namespace},
        )
        return int(rows[0].get("n", 0) or 0) if rows else 0

    def _entity_storage_key(self, entity_kind: str, entity_id: str) -> str:
        return self.graph._entity_storage_key(entity_kind, entity_id)

    def _receipt_storage_key(
        self,
        entity_kind: str,
        source_entity_id: str,
        source_key: str,
    ) -> str:
        source_entity_key = self._entity_storage_key(entity_kind, source_entity_id)
        return _hash_parts(self.namespace, "entity-receipt", source_entity_key, source_key)

    def _assignment_rows(self, plan: IdentityMigrationPlan) -> list[dict[str, str]]:
        return [
            {
                "receipt_storage_key": self._receipt_storage_key(
                    plan.entity_kind, row.source_entity_id, row.source_key
                ),
                "source_entity_storage_key": self._entity_storage_key(
                    plan.entity_kind, row.source_entity_id
                ),
                "target_entity_storage_key": self._entity_storage_key(
                    plan.entity_kind, row.target_entity_id
                ),
                "source_entity_id": row.source_entity_id,
                "source_key": row.source_key,
                "target_entity_id": row.target_entity_id,
            }
            for row in plan.assignments
        ]

    def apply(self, plan: IdentityMigrationPlan) -> IdentityMigrationResult:
        """Apply one explicit merge/split mapping without mutating source receipts."""
        self.graph.schema.entity_kind(plan.entity_kind)
        self.bootstrap_current_identity()
        assignments = self._assignment_rows(plan)

        validation = self._neo4j.execute(
            """
            UNWIND $assignments AS assignment
            OPTIONAL MATCH (receipt:MutationEntityReceipt {
                storage_key:assignment.receipt_storage_key
            })
            OPTIONAL MATCH (source:MutationEntity {
                storage_key:assignment.source_entity_storage_key
            })
            OPTIONAL MATCH (target:MutationEntity {
                storage_key:assignment.target_entity_storage_key
            })
            RETURN count(assignment) AS expected,
                   count(receipt) AS receipts,
                   count(source) AS sources,
                   count(target) AS targets,
                   count(CASE
                       WHEN receipt.namespace=$namespace
                        AND source.namespace=$namespace
                        AND target.namespace=$namespace
                        AND source.entity_kind=$entity_kind
                        AND target.entity_kind=$entity_kind
                       THEN 1
                   END) AS valid
            """,
            {
                "namespace": self.namespace,
                "entity_kind": plan.entity_kind,
                "assignments": assignments,
            },
        )
        if not validation:
            raise ValueError("identity migration validation returned no result")
        check = validation[0]
        expected = len(assignments)
        if any(
            int(check.get(name, 0) or 0) != expected
            for name in ("expected", "receipts", "sources", "targets", "valid")
        ):
            raise ValueError("identity migration references missing or wrong-kind entities/receipts")

        payload = {
            "migration_id": plan.migration_id,
            "version": plan.version,
            "entity_kind": plan.entity_kind,
            "effective_at": plan.effective_at,
            "reason": plan.reason,
            "assignments": assignments,
        }
        payload_hash = _hash_parts(_stable_json(payload))
        migration_storage_key = _hash_parts(
            self.namespace, "identity-migration", plan.migration_id
        )

        existing = self._neo4j.execute(
            """
            MATCH (m:MutationIdentityMigration {storage_key:$storage_key})
            RETURN m.payload_hash AS payload_hash
            """,
            {"storage_key": migration_storage_key},
        )
        if existing:
            existing_hash = str(existing[0].get("payload_hash") or "")
            if existing_hash != payload_hash:
                raise ValueError(
                    "identity migration ID collision: existing migration has different content"
                )
            return IdentityMigrationResult(
                migration_id=plan.migration_id,
                version=plan.version,
                assignment_count=len(assignments),
                touched_entity_count=0,
                replay=True,
            )

        rows = self._neo4j.execute(
            """
            UNWIND $assignments AS assignment
            MATCH (receipt:MutationEntityReceipt {
                storage_key:assignment.receipt_storage_key
            })
            MATCH (source:MutationEntity {
                storage_key:assignment.source_entity_storage_key
            })
            MATCH (target:MutationEntity {
                storage_key:assignment.target_entity_storage_key
            })
            OPTIONAL MATCH (receipt)-[prior:CURRENT_IDENTITY]->(:MutationEntity)
            DELETE prior
            MERGE (receipt)-[:CURRENT_IDENTITY]->(target)
            WITH collect(DISTINCT source.storage_key)
                 + collect(DISTINCT target.storage_key) AS touched,
                 count(*) AS assigned
            MERGE (migration:MutationIdentityMigration {storage_key:$migration_storage_key})
              ON CREATE SET migration.namespace=$namespace,
                            migration.migration_id=$migration_id,
                            migration.version=$version,
                            migration.entity_kind=$entity_kind,
                            migration.effective_at=$effective_at,
                            migration.reason=$reason,
                            migration.payload_hash=$payload_hash,
                            migration.created_at=datetime()
            WITH migration, assigned, touched
            UNWIND touched AS touched_key
            MATCH (entity:MutationEntity {storage_key:touched_key})
            OPTIONAL MATCH (receipt:MutationEntityReceipt)-[:CURRENT_IDENTITY]->(entity)
            WITH migration, assigned, entity, count(receipt) AS current_receipts
            SET entity.identity_active=(current_receipts > 0),
                entity.identity_retired_at=CASE
                    WHEN current_receipts=0 THEN $effective_at
                    ELSE null
                END
            WITH migration, assigned, count(DISTINCT entity) AS touched_entity_count
            RETURN migration.payload_hash AS payload_hash,
                   assigned AS assigned,
                   touched_entity_count AS touched_entity_count
            """,
            {
                "namespace": self.namespace,
                "migration_storage_key": migration_storage_key,
                "migration_id": plan.migration_id,
                "version": int(plan.version),
                "entity_kind": plan.entity_kind,
                "effective_at": plan.effective_at,
                "reason": plan.reason,
                "payload_hash": payload_hash,
                "assignments": assignments,
            },
        )
        if not rows:
            raise RuntimeError("identity migration returned no row")
        if str(rows[0].get("payload_hash") or "") != payload_hash:
            raise ValueError("identity migration payload changed during apply")
        return IdentityMigrationResult(
            migration_id=plan.migration_id,
            version=plan.version,
            assignment_count=int(rows[0].get("assigned", 0) or 0),
            touched_entity_count=int(rows[0].get("touched_entity_count", 0) or 0),
            replay=False,
        )

    def resolve(
        self,
        *,
        entity_kind: str,
        entity_id: str,
        source_key: str | None = None,
    ) -> IdentityResolution:
        """Resolve an old entity reference through receipt-level current identity."""
        self.graph.schema.entity_kind(entity_kind)
        source_storage_key = self._entity_storage_key(entity_kind, entity_id)

        if source_key is not None:
            receipt_storage_key = self._receipt_storage_key(
                entity_kind, entity_id, source_key
            )
            rows = self._neo4j.execute(
                """
                MATCH (receipt:MutationEntityReceipt {
                    storage_key:$receipt_storage_key
                })-[:CURRENT_IDENTITY]->(current:MutationEntity)
                WHERE receipt.namespace=$namespace
                  AND current.namespace=$namespace
                  AND current.entity_kind=$entity_kind
                RETURN DISTINCT current.entity_kind AS entity_kind,
                       current.entity_kind_version AS entity_kind_version,
                       current.entity_id AS entity_id,
                       current.canonical_key AS canonical_key,
                       current.display_name AS display_name
                """,
                {
                    "namespace": self.namespace,
                    "entity_kind": entity_kind,
                    "receipt_storage_key": receipt_storage_key,
                },
            )
        else:
            rows = self._neo4j.execute(
                """
                MATCH (source:MutationEntity {storage_key:$source_storage_key})
                WHERE source.namespace=$namespace
                  AND source.entity_kind=$entity_kind
                OPTIONAL MATCH (receipt:MutationEntityReceipt)-[:EVIDENCE_FOR]->(source)
                OPTIONAL MATCH (receipt)-[:CURRENT_IDENTITY]->(current:MutationEntity)
                WITH source, collect(DISTINCT current) AS current_entities
                UNWIND CASE
                    WHEN size([entity IN current_entities WHERE entity IS NOT NULL])=0
                         AND coalesce(source.identity_active,true)=true
                    THEN [source]
                    ELSE [entity IN current_entities WHERE entity IS NOT NULL]
                END AS current
                RETURN DISTINCT current.entity_kind AS entity_kind,
                       current.entity_kind_version AS entity_kind_version,
                       current.entity_id AS entity_id,
                       current.canonical_key AS canonical_key,
                       current.display_name AS display_name
                """,
                {
                    "namespace": self.namespace,
                    "entity_kind": entity_kind,
                    "source_storage_key": source_storage_key,
                },
            )

        entities = tuple(
            ResolvedEntity(
                entity_kind=str(row["entity_kind"]),
                entity_kind_version=int(row["entity_kind_version"]),
                entity_id=str(row["entity_id"]),
                canonical_key=str(row["canonical_key"]),
                display_name=str(row["display_name"]),
            )
            for row in rows
        )
        if not entities:
            return IdentityResolution(status="missing", entities=())
        if len(entities) == 1:
            return IdentityResolution(status="resolved", entities=entities)
        return IdentityResolution(
            status="ambiguous",
            entities=tuple(sorted(entities, key=lambda row: row.entity_id)),
        )

    def receipt_resolution_rows(
        self,
        *,
        entity_kind: str,
        source_entity_id: str,
    ) -> list[dict[str, str]]:
        """Expose original-vs-current identity for audit-oriented spike assertions."""
        source_storage_key = self._entity_storage_key(entity_kind, source_entity_id)
        rows = self._neo4j.execute(
            """
            MATCH (receipt:MutationEntityReceipt)-[:EVIDENCE_FOR]->(original:MutationEntity {
                storage_key:$source_storage_key
            })
            WHERE receipt.namespace=$namespace
            OPTIONAL MATCH (receipt)-[:CURRENT_IDENTITY]->(current:MutationEntity)
            RETURN receipt.source_key AS source_key,
                   original.entity_id AS original_entity_id,
                   current.entity_id AS current_entity_id
            ORDER BY receipt.source_key ASC
            """,
            {
                "namespace": self.namespace,
                "source_storage_key": source_storage_key,
            },
        )
        return [
            {
                "source_key": str(row.get("source_key") or ""),
                "original_entity_id": str(row.get("original_entity_id") or ""),
                "current_entity_id": str(row.get("current_entity_id") or ""),
            }
            for row in rows
        ]
