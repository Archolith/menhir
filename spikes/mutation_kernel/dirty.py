"""Generation-backed dirty-slot scheduling for the mutation-kernel research spike.

This layer tests whether projection discovery can stay domain-neutral without scanning all durable
assertions after every write. A new assertion is committed together with one or more generic dirty
projection slots in a single Neo4j statement. Workers discover only slots whose dirty generation is
ahead of the generation recorded on the disposable current projection.

The scheduler deliberately uses optimistic generations rather than leases. Duplicate workers may do
redundant fold work, but an older generation cannot overwrite a newer persisted projection. Leasing
can therefore remain an operational optimization if this experiment holds.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import uuid
from typing import Callable, Sequence

from .kernel import Assertion
from .lifecycle import FoldFunction, ProjectionSlot, derive_projection
from .neo4j_store import (
    Neo4jEnvelopeStore,
    ProjectionOutcome,
    _evidence_json,
    _hash_parts,
    _pairs_json,
    _projection_slot,
    _stable_json,
)


@dataclass(frozen=True)
class DirtyProjection:
    """One projection slot observed dirty at a specific optimistic generation."""

    slot: ProjectionSlot
    generation: int
    projected_generation: int

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("DirtyProjection.generation must be >= 1")
        if self.projected_generation < 0:
            raise ValueError("DirtyProjection.projected_generation must be >= 0")
        if self.projected_generation >= self.generation:
            raise ValueError("DirtyProjection must actually be dirty")


@dataclass(frozen=True)
class DirtyReconcileResult:
    """One generation-aware rebuild attempt."""

    dirty: DirtyProjection
    outcome: ProjectionOutcome
    assertion_count: int
    current_assertion_count: int
    applied: bool
    changed: bool
    previous_hash: str | None
    projection_hash: str | None
    previous_generation: int
    persisted_generation: int
    current_generation: int


FoldResolver = Callable[[ProjectionSlot], FoldFunction]


def _assertion_payload(store: Neo4jEnvelopeStore, assertion: Assertion) -> dict[str, object]:
    encoded_value = store.codec.encode(assertion.value)
    return {
        "assertion_id": assertion.assertion_id,
        "source_key": assertion.source_key,
        "assertion_type": assertion.assertion_type,
        "subject_id": assertion.subject_id,
        "scope": assertion.scope,
        "key": assertion.key,
        "value": encoded_value,
        "valid_at": assertion.valid_at,
        "learned_at": assertion.learned_at,
        "authority": assertion.authority,
        "confidence": assertion.confidence,
        "evidence": json.loads(_evidence_json(assertion.evidence)),
        "supersedes": list(assertion.supersedes),
        "interpreter_version": assertion.interpreter_version,
        "metadata": [list(pair) for pair in assertion.metadata],
        "dimensions": [list(pair) for pair in assertion.dimensions],
    }


def _work_storage_key(namespace: str, slot: ProjectionSlot) -> str:
    return _hash_parts(
        namespace,
        slot.assertion_type,
        slot.view_type,
        slot.subject_id,
        slot.scope,
        slot.key,
        _pairs_json(slot.dimensions),
    )


def _projection_storage_key(namespace: str, slot: ProjectionSlot) -> str:
    projection_key = _hash_parts(
        slot.view_type,
        slot.subject_id,
        slot.scope,
        slot.key,
        _pairs_json(slot.dimensions),
    )
    return _hash_parts(namespace, projection_key)


def _validate_assertion_slot(assertion: Assertion, slot: ProjectionSlot) -> None:
    expected = (
        assertion.assertion_type,
        assertion.subject_id,
        assertion.scope,
        assertion.key,
        assertion.dimensions,
    )
    actual = (
        slot.assertion_type,
        slot.subject_id,
        slot.scope,
        slot.key,
        slot.dimensions,
    )
    if actual != expected:
        raise ValueError(
            "dirty projection slot does not describe the assertion being committed: "
            f"assertion={expected!r} slot={actual!r}"
        )


class GenerationalNeo4jEnvelopeStore(Neo4jEnvelopeStore):
    """Neo4j envelope store plus domain-neutral projection work generations."""

    def activate(self) -> None:
        super().activate()
        self._neo4j.execute(
            "CREATE CONSTRAINT mutation_projection_work_storage_key IF NOT EXISTS "
            "FOR (w:MutationProjectionWork) REQUIRE w.storage_key IS UNIQUE"
        )

    def record_assertion_for_slots(
        self,
        assertion: Assertion,
        slots: Sequence[ProjectionSlot],
    ) -> dict[str, object]:
        """Atomically commit one immutable assertion and dirty its projection targets.

        Only creation of a new assertion advances generations. Replaying the same immutable
        assertion is a no-op, and a payload collision fails closed before any work generation is
        advanced.
        """
        slots = tuple(slots)
        if not slots:
            raise ValueError("record_assertion_for_slots requires at least one projection slot")
        for slot in slots:
            _validate_assertion_slot(assertion, slot)
        work_keys = [_work_storage_key(self.namespace, slot) for slot in slots]
        if len(work_keys) != len(set(work_keys)):
            raise ValueError("projection slots must be unique")

        encoded_value = self.codec.encode(assertion.value)
        payload = _assertion_payload(self, assertion)
        payload_hash = _hash_parts(_stable_json(payload))
        assertion_storage_key = _hash_parts(self.namespace, assertion.assertion_id)
        write_token = uuid.uuid4().hex
        dirty_slots = [
            {
                "storage_key": work_key,
                "projection_storage_key": _projection_storage_key(self.namespace, slot),
                "assertion_type": slot.assertion_type,
                "view_type": slot.view_type,
                "subject_id": slot.subject_id,
                "scope": slot.scope,
                "key": slot.key,
                "dimensions_json": _pairs_json(slot.dimensions),
            }
            for slot, work_key in zip(slots, work_keys)
        ]

        rows = self._neo4j.execute(
            """
            MERGE (a:MutationAssertion {storage_key:$storage_key})
              ON CREATE SET
                a.namespace = $namespace,
                a.assertion_id = $assertion_id,
                a.source_key = $source_key,
                a.assertion_type = $assertion_type,
                a.subject_id = $subject_id,
                a.scope = $scope,
                a.key = $key,
                a.value_json = $value_json,
                a.valid_at = $valid_at,
                a.learned_at = $learned_at,
                a.authority = $authority,
                a.confidence = $confidence,
                a.evidence_json = $evidence_json,
                a.supersedes_json = $supersedes_json,
                a.interpreter_version = $interpreter_version,
                a.metadata_json = $metadata_json,
                a.dimensions_json = $dimensions_json,
                a.payload_hash = $payload_hash,
                a.created_token = $write_token,
                a.created_at = datetime()
            WITH a, (a.created_token = $write_token) AS created
            FOREACH (slot IN CASE
                WHEN created AND a.payload_hash = $payload_hash THEN $dirty_slots
                ELSE []
            END |
                MERGE (w:MutationProjectionWork {storage_key:slot.storage_key})
                  ON CREATE SET
                    w.namespace = $namespace,
                    w.projection_storage_key = slot.projection_storage_key,
                    w.assertion_type = slot.assertion_type,
                    w.view_type = slot.view_type,
                    w.subject_id = slot.subject_id,
                    w.scope = slot.scope,
                    w.key = slot.key,
                    w.dimensions_json = slot.dimensions_json,
                    w.generation = 0,
                    w.created_at = datetime()
                SET w.generation = coalesce(w.generation, 0) + 1,
                    w.dirty_at = datetime()
            )
            RETURN a.payload_hash AS payload_hash, created AS created
            """,
            {
                "storage_key": assertion_storage_key,
                "namespace": self.namespace,
                "assertion_id": assertion.assertion_id,
                "source_key": assertion.source_key,
                "assertion_type": assertion.assertion_type,
                "subject_id": assertion.subject_id,
                "scope": assertion.scope,
                "key": assertion.key,
                "value_json": _stable_json(encoded_value),
                "valid_at": assertion.valid_at,
                "learned_at": assertion.learned_at,
                "authority": assertion.authority,
                "confidence": float(assertion.confidence),
                "evidence_json": _evidence_json(assertion.evidence),
                "supersedes_json": _stable_json(list(assertion.supersedes)),
                "interpreter_version": assertion.interpreter_version,
                "metadata_json": _pairs_json(assertion.metadata),
                "dimensions_json": _pairs_json(assertion.dimensions),
                "payload_hash": payload_hash,
                "write_token": write_token,
                "dirty_slots": dirty_slots,
            },
        )
        if not rows:
            raise RuntimeError("assertion/dirty-slot commit returned no result")
        existing_hash = str(rows[0].get("payload_hash") or "")
        if existing_hash != payload_hash:
            raise ValueError(
                "assertion identity collision: persisted envelope differs for "
                f"{assertion.assertion_id}"
            )
        return {
            "storage_key": assertion_storage_key,
            "payload_hash": payload_hash,
            "created": bool(rows[0].get("created")),
            "slot_count": len(slots),
        }

    def load_dirty_slots(self, *, limit: int = 100) -> list[DirtyProjection]:
        """Discover only projection slots whose assertion generation is not yet projected."""
        if limit < 1:
            raise ValueError("limit must be >= 1")
        rows = self._neo4j.execute(
            """
            MATCH (w:MutationProjectionWork {namespace:$namespace})
            OPTIONAL MATCH (p:MutationProjection {storage_key:w.projection_storage_key})
            WITH w, coalesce(p.source_generation, 0) AS projected_generation
            WHERE w.generation > projected_generation
            RETURN w.assertion_type AS assertion_type,
                   w.view_type AS view_type,
                   w.subject_id AS subject_id,
                   w.scope AS scope,
                   w.key AS key,
                   w.dimensions_json AS dimensions_json,
                   w.generation AS generation,
                   projected_generation AS projected_generation,
                   w.storage_key AS storage_key
            ORDER BY w.dirty_at ASC, storage_key ASC
            LIMIT $limit
            """,
            {"namespace": self.namespace, "limit": int(limit)},
        )
        return [
            DirtyProjection(
                slot=ProjectionSlot(
                    assertion_type=str(row["assertion_type"]),
                    view_type=str(row["view_type"]),
                    subject_id=str(row["subject_id"]),
                    scope=str(row.get("scope") or ""),
                    key=str(row["key"]),
                    dimensions=tuple(
                        (str(name), str(value))
                        for name, value in json.loads(
                            str(row.get("dimensions_json") or "[]")
                        )
                    ),
                ),
                generation=int(row["generation"]),
                projected_generation=int(row.get("projected_generation", 0) or 0),
            )
            for row in rows
        ]

    def count_dirty_slots(self) -> int:
        rows = self._neo4j.execute(
            """
            MATCH (w:MutationProjectionWork {namespace:$namespace})
            OPTIONAL MATCH (p:MutationProjection {storage_key:w.projection_storage_key})
            WITH w, coalesce(p.source_generation, 0) AS projected_generation
            WHERE w.generation > projected_generation
            RETURN count(w) AS n
            """,
            {"namespace": self.namespace},
        )
        return int(rows[0].get("n", 0) or 0) if rows else 0

    def record_outcome_at_generation(
        self,
        outcome: ProjectionOutcome,
        *,
        slot: ProjectionSlot,
        generation: int,
    ) -> dict[str, object]:
        """Persist an outcome only if its optimistic generation is not older than current state.

        A generation lower than the persisted projection is rejected as stale. The same generation
        producing a different projection hash is treated as nondeterminism and fails closed.
        """
        if generation < 1:
            raise ValueError("generation must be >= 1")
        actual_output = _projection_slot(outcome)
        expected_output = (
            slot.view_type,
            slot.subject_id,
            slot.scope,
            slot.key,
            slot.dimensions,
        )
        if actual_output != expected_output:
            raise ValueError("outcome does not belong to the requested projection slot")

        payload = self._encode_outcome(outcome)
        projection_key = _hash_parts(
            slot.view_type,
            slot.subject_id,
            slot.scope,
            slot.key,
            _pairs_json(slot.dimensions),
        )
        projection_storage_key = _hash_parts(self.namespace, projection_key)
        work_storage_key = _work_storage_key(self.namespace, slot)
        projection_hash = _hash_parts(_stable_json(payload))

        rows = self._neo4j.execute(
            """
            MATCH (w:MutationProjectionWork {storage_key:$work_storage_key})
            WHERE w.namespace = $namespace AND w.generation >= $generation
            MERGE (p:MutationProjection {storage_key:$projection_storage_key})
            WITH w, p,
                 p.projection_hash AS previous_hash,
                 coalesce(p.source_generation, 0) AS previous_generation
            WITH w, p, previous_hash, previous_generation,
                 CASE
                    WHEN $generation > previous_generation THEN true
                    WHEN $generation = previous_generation
                         AND (previous_hash IS NULL OR previous_hash = $projection_hash) THEN true
                    ELSE false
                 END AS can_apply,
                 CASE
                    WHEN $generation = previous_generation
                         AND previous_hash IS NOT NULL
                         AND previous_hash <> $projection_hash THEN true
                    ELSE false
                 END AS collision
            FOREACH (_ IN CASE WHEN can_apply THEN [1] ELSE [] END |
                SET p.namespace = $namespace,
                    p.projection_key = $projection_key,
                    p.view_type = $view_type,
                    p.subject_id = $subject_id,
                    p.scope = $scope,
                    p.key = $key,
                    p.dimensions_json = $dimensions_json,
                    p.status = $status,
                    p.payload_json = $payload_json,
                    p.projection_hash = $projection_hash,
                    p.source_generation = $generation,
                    p.updated_at = datetime()
            )
            RETURN previous_hash AS previous_hash,
                   previous_generation AS previous_generation,
                   p.projection_hash AS projection_hash,
                   coalesce(p.source_generation, 0) AS persisted_generation,
                   w.generation AS current_generation,
                   can_apply AS applied,
                   collision AS collision
            """,
            {
                "namespace": self.namespace,
                "work_storage_key": work_storage_key,
                "projection_storage_key": projection_storage_key,
                "projection_key": projection_key,
                "view_type": slot.view_type,
                "subject_id": slot.subject_id,
                "scope": slot.scope,
                "key": slot.key,
                "dimensions_json": _pairs_json(slot.dimensions),
                "status": payload["status"],
                "payload_json": _stable_json(payload),
                "projection_hash": projection_hash,
                "generation": int(generation),
            },
        )
        if not rows:
            raise ValueError("generation is ahead of the registered dirty projection slot")
        row = rows[0]
        if bool(row.get("collision")):
            raise ValueError(
                "projection nondeterminism: one generation produced two different outcomes"
            )
        previous_hash = row.get("previous_hash")
        applied = bool(row.get("applied"))
        return {
            "applied": applied,
            "changed": applied and previous_hash != projection_hash,
            "previous_hash": None if previous_hash is None else str(previous_hash),
            "projection_hash": (
                None if row.get("projection_hash") is None else str(row["projection_hash"])
            ),
            "previous_generation": int(row.get("previous_generation", 0) or 0),
            "persisted_generation": int(row.get("persisted_generation", 0) or 0),
            "current_generation": int(row.get("current_generation", 0) or 0),
        }


def rebuild_dirty_projection(
    store: GenerationalNeo4jEnvelopeStore,
    dirty: DirtyProjection,
    fold: FoldFunction,
) -> DirtyReconcileResult:
    """Rebuild one discovered dirty slot and attach its observed generation to the write."""
    derived = derive_projection(store, dirty.slot, fold)
    write = store.record_outcome_at_generation(
        derived.outcome,
        slot=dirty.slot,
        generation=dirty.generation,
    )
    return DirtyReconcileResult(
        dirty=dirty,
        outcome=derived.outcome,
        assertion_count=derived.assertion_count,
        current_assertion_count=derived.current_assertion_count,
        applied=bool(write["applied"]),
        changed=bool(write["changed"]),
        previous_hash=(
            None if write.get("previous_hash") is None else str(write["previous_hash"])
        ),
        projection_hash=(
            None if write.get("projection_hash") is None else str(write["projection_hash"])
        ),
        previous_generation=int(write["previous_generation"]),
        persisted_generation=int(write["persisted_generation"]),
        current_generation=int(write["current_generation"]),
    )


def reconcile_dirty_batch(
    store: GenerationalNeo4jEnvelopeStore,
    fold_resolver: FoldResolver,
    *,
    limit: int = 100,
) -> list[DirtyReconcileResult]:
    """Rebuild a bounded batch discovered from work metadata rather than assertion scans."""
    dirty_slots = store.load_dirty_slots(limit=limit)
    return [
        rebuild_dirty_projection(store, dirty, fold_resolver(dirty.slot))
        for dirty in dirty_slots
    ]
