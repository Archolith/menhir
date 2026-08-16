"""Receipt-local identity generations for stale-derivation fencing.

Experiment 9 deliberately kept the identity overlay minimal. Experiment 10 needs a compare-and-swap
primitive that does not infer freshness from graph endpoint equality. Each immutable entity receipt
therefore gets a monotonically increasing ``identity_generation``. Reassigning the receipt's
``CURRENT_IDENTITY`` increments that generation in the same Neo4j statement.

This subclasses the spike identity store rather than changing Experiment 9's tested implementation.
The duplication in ``apply`` is intentional spike debt; if promoted, migration/generation belongs in
one supported persistence contract.
"""

from __future__ import annotations

from dataclasses import dataclass

from .graph import ResolvedEntity
from .identity import (
    IdentityMigrationPlan,
    IdentityMigrationResult,
    Neo4jIdentityEvolution,
    _hash_parts,
    _stable_json,
)


@dataclass(frozen=True)
class ResolvedReceiptIdentity:
    receipt_storage_key: str
    source_key: str
    entity: ResolvedEntity
    identity_generation: int

    def __post_init__(self) -> None:
        if not self.receipt_storage_key.strip() or not self.source_key.strip():
            raise ValueError("resolved receipt identity keys must be non-empty")
        if self.identity_generation < 0:
            raise ValueError("identity_generation must be >= 0")


class GenerationalNeo4jIdentityEvolution(Neo4jIdentityEvolution):
    """Identity overlay with a receipt-local monotonic generation token."""

    def bootstrap_current_identity(self) -> int:
        # Existing receipts created before this experiment start at generation zero. Bootstrap does
        # not count as an identity change, so it must not advance the generation.
        self._neo4j.execute(
            """
            MATCH (receipt:MutationEntityReceipt)
            WHERE receipt.namespace=$namespace
            SET receipt.identity_generation=coalesce(receipt.identity_generation,0)
            RETURN count(receipt) AS n
            """,
            {"namespace": self.namespace},
        )
        return super().bootstrap_current_identity()

    def resolve_receipt(
        self,
        *,
        entity_kind: str,
        entity_id: str,
        source_key: str,
    ) -> ResolvedReceiptIdentity | None:
        """Read current receipt identity and generation from one Neo4j snapshot."""
        self.graph.schema.entity_kind(entity_kind)
        receipt_storage_key = self._receipt_storage_key(entity_kind, entity_id, source_key)
        rows = self._neo4j.execute(
            """
            MATCH (receipt:MutationEntityReceipt {storage_key:$receipt_storage_key})
                  -[:CURRENT_IDENTITY]->(current:MutationEntity)
            WHERE receipt.namespace=$namespace
              AND current.namespace=$namespace
              AND current.entity_kind=$entity_kind
            RETURN receipt.source_key AS source_key,
                   coalesce(receipt.identity_generation,0) AS identity_generation,
                   current.entity_kind AS entity_kind,
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
        if not rows:
            return None
        if len(rows) != 1:
            raise ValueError("one entity receipt must resolve to exactly one current identity")
        row = rows[0]
        return ResolvedReceiptIdentity(
            receipt_storage_key=receipt_storage_key,
            source_key=str(row["source_key"]),
            identity_generation=int(row.get("identity_generation",0) or 0),
            entity=ResolvedEntity(
                entity_kind=str(row["entity_kind"]),
                entity_kind_version=int(row["entity_kind_version"]),
                entity_id=str(row["entity_id"]),
                canonical_key=str(row["canonical_key"]),
                display_name=str(row["display_name"]),
            ),
        )

    def apply(self, plan: IdentityMigrationPlan) -> IdentityMigrationResult:
        """Move receipt identity and increment its generation atomically."""
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
            int(check.get(name,0) or 0) != expected
            for name in ("expected","receipts","sources","targets","valid")
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
            SET receipt.identity_generation=coalesce(receipt.identity_generation,0)+1,
                receipt.identity_changed_at=$effective_at
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
            assignment_count=int(rows[0].get("assigned",0) or 0),
            touched_entity_count=int(rows[0].get("touched_entity_count",0) or 0),
            replay=False,
        )
