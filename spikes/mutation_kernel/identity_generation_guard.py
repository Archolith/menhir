"""Generation-based graph write fencing for identity-sensitive materializers.

The earlier Experiment 10 attempts inferred freshness from ``CURRENT_IDENTITY`` endpoint equality or
tried to filter a compound Cypher statement by a guard predicate. Those attempts are intentionally
left in ``identity_scheduler.py`` as failed spike evidence.

This variant uses a receipt-local generation and an explicit Neo4j transaction. The worker acquires a
write lock on every guarded receipt, checks the generation it derived against, reconciles graph edges,
and releases the guard marker only at transaction completion. Identity migration also writes the
receipt node when it advances the generation, so the two operations serialize on the same durable
object.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import uuid
from typing import Any, Sequence

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
class GenerationReceiptIdentityGuard:
    receipt_storage_key: str
    source_key: str
    expected_current_entity_id: str
    expected_identity_generation: int

    def __post_init__(self) -> None:
        for name in ("receipt_storage_key", "source_key", "expected_current_entity_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"GenerationReceiptIdentityGuard.{name} must be non-empty")
        if self.expected_identity_generation < 0:
            raise ValueError("expected_identity_generation must be >= 0")


@dataclass(frozen=True)
class GenerationalGuardedGraphDerivation:
    outcome: GraphOutcome
    guards: tuple[GenerationReceiptIdentityGuard, ...]


class GenerationalGuardedNeo4jGraphMaterializer(Neo4jGraphMaterializer):
    """Reconcile topology under receipt locks only at the generations the worker observed."""

    _LOCK_QUERY = """
        UNWIND $guards AS guard
        MATCH (receipt:MutationEntityReceipt {storage_key:guard.receipt_storage_key})
        WHERE receipt.namespace=$namespace
          AND receipt.source_key=guard.source_key
        WITH receipt, guard
        ORDER BY receipt.storage_key ASC
        SET receipt.mutation_identity_guard_token=$guard_token
        RETURN receipt.storage_key AS receipt_storage_key,
               receipt.source_key AS source_key,
               coalesce(receipt.identity_generation,0) AS actual_generation,
               guard.expected_identity_generation AS expected_generation
    """

    _EDGE_QUERY = """
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
        CALL (edge_rows) {
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
    """

    _UNLOCK_QUERY = """
        UNWIND $guard_keys AS storage_key
        MATCH (receipt:MutationEntityReceipt {storage_key:storage_key})
        WHERE receipt.namespace=$namespace
          AND receipt.mutation_identity_guard_token=$guard_token
        REMOVE receipt.mutation_identity_guard_token
        RETURN count(receipt) AS unlocked
    """

    def reconcile_edges_guarded(
        self,
        outcome: GraphOutcome,
        guards: Sequence[GenerationReceiptIdentityGuard],
    ) -> dict[str, object]:
        guards = tuple(sorted(guards, key=lambda row: row.receipt_storage_key))
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
            source_storage_key = self._entity_storage_key(
                edge.source_entity_kind, edge.source_entity_id
            )
            target_storage_key = self._entity_storage_key(
                edge.target_entity_kind, edge.target_entity_id
            )
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
                    "source_storage_key": source_storage_key,
                    "source_entity_kind": edge.source_entity_kind,
                    "target_storage_key": target_storage_key,
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
                "expected_identity_generation": int(row.expected_identity_generation),
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
        guard_token = uuid.uuid4().hex

        edge_params = {
            "namespace": self.namespace,
            "materializer_id": outcome.materializer_id,
            "slot_key": outcome.slot_key,
            "effective_at": outcome.effective_at,
            "desired_keys": desired_keys,
            "projection_hash": projection_hash,
            "edges": edge_rows,
        }

        def _transaction(tx: Any) -> list[dict[str, Any]]:
            locked = [
                record.data()
                for record in tx.run(
                    self._LOCK_QUERY,
                    guards=guard_rows,
                    namespace=self.namespace,
                    guard_token=guard_token,
                )
            ]
            if len(locked) != len(guard_rows):
                raise ValueError("identity guard changed before graph write: receipt missing")
            for row in locked:
                if int(row.get("actual_generation",0) or 0) != int(
                    row.get("expected_generation",-1)
                ):
                    raise ValueError("identity guard changed before graph write: generation advanced")

            rows = [record.data() for record in tx.run(self._EDGE_QUERY, **edge_params)]
            if not rows:
                raise ValueError("graph edge materialization could not resolve all endpoint entities")

            unlocked = [
                record.data()
                for record in tx.run(
                    self._UNLOCK_QUERY,
                    guard_keys=guard_keys,
                    namespace=self.namespace,
                    guard_token=guard_token,
                )
            ]
            if not unlocked or int(unlocked[0].get("unlocked",0) or 0) != len(guard_rows):
                raise RuntimeError("identity guard lock cleanup did not cover every receipt")
            return rows

        # Spike-local use of the production adapter's private driver seam. This proves the transaction
        # semantics we need; a promoted implementation should expose a supported transaction API.
        driver = self._neo4j._get_driver()
        with driver.session(database=self._neo4j.database) as session:
            rows = session.execute_write(_transaction)

        row = rows[0]
        previous_active = tuple(str(value) for value in (row.get("previous_active_keys") or []))
        return {
            "changed": set(previous_active) != set(desired_keys),
            "retired": int(row.get("retired",0) or 0),
            "upserted": int(row.get("upserted",0) or 0),
            "projection_hash": projection_hash,
            "active_edge_count": len(desired_keys),
            "status": "edges" if isinstance(outcome, EdgeSet) else "abstention",
        }
