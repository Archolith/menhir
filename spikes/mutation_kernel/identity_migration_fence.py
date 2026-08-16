"""Transactional fencing for concurrent identity migrations in the mutation-kernel spike.

A migration can be planned outside a transaction, but that plan is only advisory. ``prepare`` records
the exact generation of every immutable receipt it intends to reassign. ``apply_prepared`` then:

1. claims/validates the migration ID inside one Neo4j write transaction;
2. write-locks every affected receipt in stable storage-key order;
3. verifies every receipt is still at the generation captured by the prepared plan;
4. moves all CURRENT_IDENTITY edges and advances all generations; and
5. updates affected entity activity and releases temporary lock markers before commit.

A stale plan therefore fails before any receipt is reassigned. A multi-receipt merge/split is all or
nothing. Exact migration replay remains idempotent even though its original expected generations are
now old.
"""

from __future__ import annotations

from dataclasses import dataclass
import uuid
from typing import Any

from .identity import (
    IdentityMigrationPlan,
    IdentityMigrationResult,
    _hash_parts,
    _stable_json,
)
from .identity_generation import GenerationalNeo4jIdentityEvolution


@dataclass(frozen=True)
class PreparedReceiptIdentityAssignment:
    receipt_storage_key: str
    source_entity_storage_key: str
    target_entity_storage_key: str
    source_entity_id: str
    source_key: str
    target_entity_id: str
    expected_identity_generation: int

    def __post_init__(self) -> None:
        for name in (
            "receipt_storage_key",
            "source_entity_storage_key",
            "target_entity_storage_key",
            "source_entity_id",
            "source_key",
            "target_entity_id",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"PreparedReceiptIdentityAssignment.{name} must be non-empty")
        if self.expected_identity_generation < 0:
            raise ValueError("expected_identity_generation must be >= 0")


@dataclass(frozen=True)
class PreparedIdentityMigration:
    plan: IdentityMigrationPlan
    assignments: tuple[PreparedReceiptIdentityAssignment, ...]
    payload_hash: str
    migration_storage_key: str

    def __post_init__(self) -> None:
        if not self.payload_hash.strip() or not self.migration_storage_key.strip():
            raise ValueError("prepared migration hashes/keys must be non-empty")
        if len(self.assignments) != len(self.plan.assignments):
            raise ValueError("prepared migration must cover every plan assignment")
        keys = [row.receipt_storage_key for row in self.assignments]
        if len(keys) != len(set(keys)):
            raise ValueError("prepared migration receipt assignments must be unique")
        if tuple(sorted(self.assignments, key=lambda row: row.receipt_storage_key)) != self.assignments:
            raise ValueError("prepared migration assignments must be in stable receipt order")


class FencedGenerationalNeo4jIdentityEvolution(GenerationalNeo4jIdentityEvolution):
    """Identity migration with generation CAS and deterministic multi-receipt transaction locks."""

    _CLAIM_MIGRATION = """
        MERGE (migration:MutationIdentityMigration {storage_key:$migration_storage_key})
          ON CREATE SET migration.namespace=$namespace,
                        migration.migration_id=$migration_id,
                        migration.version=$version,
                        migration.entity_kind=$entity_kind,
                        migration.effective_at=$effective_at,
                        migration.reason=$reason,
                        migration.payload_hash=$payload_hash,
                        migration.claim_token=$claim_token,
                        migration.created_at=datetime()
        RETURN migration.payload_hash AS payload_hash,
               migration.claim_token AS claim_token,
               migration.applied_at AS applied_at
    """

    _LOCK_RECEIPTS = """
        UNWIND $assignments AS assignment
        MATCH (receipt:MutationEntityReceipt {storage_key:assignment.receipt_storage_key})
        MATCH (source:MutationEntity {storage_key:assignment.source_entity_storage_key})
        MATCH (target:MutationEntity {storage_key:assignment.target_entity_storage_key})
        WHERE receipt.namespace=$namespace
          AND receipt.source_key=assignment.source_key
          AND source.namespace=$namespace
          AND target.namespace=$namespace
          AND source.entity_kind=$entity_kind
          AND target.entity_kind=$entity_kind
        WITH receipt, source, target, assignment
        ORDER BY receipt.storage_key ASC
        SET receipt.mutation_identity_migration_lock=$lock_token
        RETURN receipt.storage_key AS receipt_storage_key,
               coalesce(receipt.identity_generation,0) AS actual_generation,
               assignment.expected_identity_generation AS expected_generation,
               source.storage_key AS source_storage_key,
               target.storage_key AS target_storage_key
    """

    _APPLY_ASSIGNMENTS = """
        UNWIND $assignments AS assignment
        MATCH (receipt:MutationEntityReceipt {storage_key:assignment.receipt_storage_key})
        MATCH (source:MutationEntity {storage_key:assignment.source_entity_storage_key})
        MATCH (target:MutationEntity {storage_key:assignment.target_entity_storage_key})
        OPTIONAL MATCH (receipt)-[prior:CURRENT_IDENTITY]->(:MutationEntity)
        DELETE prior
        SET receipt.identity_generation=coalesce(receipt.identity_generation,0)+1,
            receipt.identity_changed_at=$effective_at
        MERGE (receipt)-[:CURRENT_IDENTITY]->(target)
        WITH collect(DISTINCT source.storage_key)
             + collect(DISTINCT target.storage_key) AS touched,
             count(*) AS assigned
        UNWIND touched AS touched_key
        WITH DISTINCT touched_key, assigned
        MATCH (entity:MutationEntity {storage_key:touched_key})
        OPTIONAL MATCH (current_receipt:MutationEntityReceipt)-[:CURRENT_IDENTITY]->(entity)
        WITH assigned, entity, count(current_receipt) AS current_receipts
        SET entity.identity_active=(current_receipts > 0),
            entity.identity_retired_at=CASE
                WHEN current_receipts=0 THEN $effective_at
                ELSE null
            END
        RETURN assigned AS assigned,
               count(DISTINCT entity) AS touched_entity_count
    """

    _FINALIZE_MIGRATION = """
        MATCH (migration:MutationIdentityMigration {storage_key:$migration_storage_key})
        WHERE migration.namespace=$namespace
          AND migration.payload_hash=$payload_hash
        SET migration.applied_at=datetime()
        REMOVE migration.claim_token
        RETURN migration.payload_hash AS payload_hash
    """

    _UNLOCK_RECEIPTS = """
        UNWIND $receipt_storage_keys AS storage_key
        MATCH (receipt:MutationEntityReceipt {storage_key:storage_key})
        WHERE receipt.namespace=$namespace
          AND receipt.mutation_identity_migration_lock=$lock_token
        REMOVE receipt.mutation_identity_migration_lock
        RETURN count(receipt) AS unlocked
    """

    def prepare(self, plan: IdentityMigrationPlan) -> PreparedIdentityMigration:
        """Capture receipt generations for an explicit plan; no identity mutation occurs here."""
        self.graph.schema.entity_kind(plan.entity_kind)
        self.bootstrap_current_identity()
        assignment_rows = self._assignment_rows(plan)
        rows = self._neo4j.execute(
            """
            UNWIND $assignments AS assignment
            MATCH (receipt:MutationEntityReceipt {storage_key:assignment.receipt_storage_key})
                  -[:CURRENT_IDENTITY]->(current:MutationEntity)
            MATCH (source:MutationEntity {storage_key:assignment.source_entity_storage_key})
            MATCH (target:MutationEntity {storage_key:assignment.target_entity_storage_key})
            WHERE receipt.namespace=$namespace
              AND receipt.source_key=assignment.source_key
              AND current.namespace=$namespace
              AND source.namespace=$namespace
              AND target.namespace=$namespace
              AND source.entity_kind=$entity_kind
              AND target.entity_kind=$entity_kind
            RETURN assignment.receipt_storage_key AS receipt_storage_key,
                   assignment.source_entity_storage_key AS source_entity_storage_key,
                   assignment.target_entity_storage_key AS target_entity_storage_key,
                   assignment.source_entity_id AS source_entity_id,
                   assignment.source_key AS source_key,
                   assignment.target_entity_id AS target_entity_id,
                   coalesce(receipt.identity_generation,0) AS identity_generation
            ORDER BY receipt_storage_key ASC
            """,
            {
                "namespace": self.namespace,
                "entity_kind": plan.entity_kind,
                "assignments": assignment_rows,
            },
        )
        if len(rows) != len(assignment_rows):
            raise ValueError("identity migration preparation could not resolve every receipt/entity")

        prepared = tuple(
            PreparedReceiptIdentityAssignment(
                receipt_storage_key=str(row["receipt_storage_key"]),
                source_entity_storage_key=str(row["source_entity_storage_key"]),
                target_entity_storage_key=str(row["target_entity_storage_key"]),
                source_entity_id=str(row["source_entity_id"]),
                source_key=str(row["source_key"]),
                target_entity_id=str(row["target_entity_id"]),
                expected_identity_generation=int(row.get("identity_generation",0) or 0),
            )
            for row in rows
        )
        payload = {
            "migration_id": plan.migration_id,
            "version": plan.version,
            "entity_kind": plan.entity_kind,
            "effective_at": plan.effective_at,
            "reason": plan.reason,
            "assignments": assignment_rows,
        }
        return PreparedIdentityMigration(
            plan=plan,
            assignments=prepared,
            payload_hash=_hash_parts(_stable_json(payload)),
            migration_storage_key=_hash_parts(
                self.namespace, "identity-migration", plan.migration_id
            ),
        )

    def apply_prepared(self, prepared: PreparedIdentityMigration) -> IdentityMigrationResult:
        """Apply the prepared migration atomically or fail if any receipt generation is stale."""
        plan = prepared.plan
        self.graph.schema.entity_kind(plan.entity_kind)
        assignment_rows = [
            {
                "receipt_storage_key": row.receipt_storage_key,
                "source_entity_storage_key": row.source_entity_storage_key,
                "target_entity_storage_key": row.target_entity_storage_key,
                "source_entity_id": row.source_entity_id,
                "source_key": row.source_key,
                "target_entity_id": row.target_entity_id,
                "expected_identity_generation": int(row.expected_identity_generation),
            }
            for row in prepared.assignments
        ]
        receipt_keys = [row.receipt_storage_key for row in prepared.assignments]
        claim_token = uuid.uuid4().hex
        lock_token = uuid.uuid4().hex

        def _transaction(tx: Any) -> IdentityMigrationResult:
            claim_rows = [
                record.data()
                for record in tx.run(
                    self._CLAIM_MIGRATION,
                    migration_storage_key=prepared.migration_storage_key,
                    namespace=self.namespace,
                    migration_id=plan.migration_id,
                    version=int(plan.version),
                    entity_kind=plan.entity_kind,
                    effective_at=plan.effective_at,
                    reason=plan.reason,
                    payload_hash=prepared.payload_hash,
                    claim_token=claim_token,
                )
            ]
            if len(claim_rows) != 1:
                raise RuntimeError("identity migration claim returned an unexpected row count")
            claim = claim_rows[0]
            existing_hash = str(claim.get("payload_hash") or "")
            if existing_hash != prepared.payload_hash:
                raise ValueError(
                    "identity migration ID collision: existing migration has different content"
                )
            created_by_us = str(claim.get("claim_token") or "") == claim_token
            if not created_by_us:
                if claim.get("applied_at") is None:
                    raise RuntimeError("identity migration exists without a completed applied_at marker")
                return IdentityMigrationResult(
                    migration_id=plan.migration_id,
                    version=plan.version,
                    assignment_count=len(prepared.assignments),
                    touched_entity_count=0,
                    replay=True,
                )

            locked = [
                record.data()
                for record in tx.run(
                    self._LOCK_RECEIPTS,
                    assignments=assignment_rows,
                    namespace=self.namespace,
                    entity_kind=plan.entity_kind,
                    lock_token=lock_token,
                )
            ]
            if len(locked) != len(assignment_rows):
                raise ValueError("identity migration could not lock/validate every prepared receipt")
            for row in locked:
                actual = int(row.get("actual_generation",0) or 0)
                expected = int(row.get("expected_generation",-1))
                if actual != expected:
                    raise ValueError(
                        "stale identity migration: receipt generation changed after prepare"
                    )

            applied_rows = [
                record.data()
                for record in tx.run(
                    self._APPLY_ASSIGNMENTS,
                    assignments=assignment_rows,
                    effective_at=plan.effective_at,
                )
            ]
            if len(applied_rows) != 1:
                raise RuntimeError("identity migration assignment application returned no summary")
            applied = applied_rows[0]
            if int(applied.get("assigned",0) or 0) != len(assignment_rows):
                raise RuntimeError("identity migration did not apply every prepared assignment")

            finalized = [
                record.data()
                for record in tx.run(
                    self._FINALIZE_MIGRATION,
                    migration_storage_key=prepared.migration_storage_key,
                    namespace=self.namespace,
                    payload_hash=prepared.payload_hash,
                )
            ]
            if len(finalized) != 1:
                raise RuntimeError("identity migration could not finalize its durable receipt")

            unlocked = [
                record.data()
                for record in tx.run(
                    self._UNLOCK_RECEIPTS,
                    receipt_storage_keys=receipt_keys,
                    namespace=self.namespace,
                    lock_token=lock_token,
                )
            ]
            if not unlocked or int(unlocked[0].get("unlocked",0) or 0) != len(receipt_keys):
                raise RuntimeError("identity migration lock cleanup did not cover every receipt")

            return IdentityMigrationResult(
                migration_id=plan.migration_id,
                version=plan.version,
                assignment_count=int(applied.get("assigned",0) or 0),
                touched_entity_count=int(applied.get("touched_entity_count",0) or 0),
                replay=False,
            )

        driver = self._neo4j._get_driver()
        with driver.session(database=self._neo4j.database) as session:
            return session.execute_write(_transaction)
