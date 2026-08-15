"""Extension registration and historical projection backfill for the mutation-kernel spike.

This experiment asks whether an extension can declare which immutable assertion types feed a View,
route new writes to the right projection work, and backfill a newly introduced projection from old
history without teaching the kernel what those assertions or Views mean.

Projection definitions own:
* input assertion types;
* mapping an assertion to zero or more output slots;
* fold semantics;
* a monotonically increasing definition version.

The store owns only generic envelopes, optimistic work generations, and current projection state.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import uuid
from typing import Callable, Sequence

from .dirty import GenerationalNeo4jEnvelopeStore, _assertion_payload
from .kernel import Abstention, Assertion, current_assertions
from .neo4j_store import (
    ProjectionOutcome,
    _evidence_json,
    _hash_parts,
    _pairs_json,
    _projection_slot,
    _stable_json,
)


@dataclass(frozen=True)
class ProjectionTarget:
    """One extension-defined output slot, independent of the assertion types that feed it."""

    view_type: str
    subject_id: str
    scope: str
    key: str
    dimensions: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for name in ("view_type", "subject_id", "key"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"ProjectionTarget.{name} must be non-empty")
        canonical = tuple(sorted((str(name), str(value)) for name, value in self.dimensions))
        names = [name for name, _ in canonical]
        if any(not name for name in names) or len(names) != len(set(names)):
            raise ValueError("ProjectionTarget.dimensions must have unique non-empty names")
        if self.dimensions != canonical:
            raise ValueError("ProjectionTarget.dimensions must be canonical")


TargetMapper = Callable[[Assertion], Sequence[ProjectionTarget]]
DefinitionFold = Callable[[list[Assertion]], ProjectionOutcome]


@dataclass(frozen=True)
class ProjectionDefinition:
    """Extension-owned declaration of evidence inputs, slot mapping, and fold behavior."""

    definition_id: str
    version: int
    input_types: tuple[str, ...]
    view_type: str
    mapper: TargetMapper
    fold: DefinitionFold

    def __post_init__(self) -> None:
        if not self.definition_id.strip():
            raise ValueError("ProjectionDefinition.definition_id must be non-empty")
        if self.version < 1:
            raise ValueError("ProjectionDefinition.version must be >= 1")
        if not self.view_type.strip():
            raise ValueError("ProjectionDefinition.view_type must be non-empty")
        canonical_inputs = tuple(sorted(str(value).strip() for value in self.input_types))
        if any(not value for value in canonical_inputs):
            raise ValueError("ProjectionDefinition.input_types must be non-empty strings")
        if not canonical_inputs:
            raise ValueError("ProjectionDefinition requires at least one input assertion type")
        if len(canonical_inputs) != len(set(canonical_inputs)):
            raise ValueError("ProjectionDefinition.input_types must be unique")
        if self.input_types != canonical_inputs:
            raise ValueError("ProjectionDefinition.input_types must be canonical")

    def targets_for(self, assertion: Assertion) -> tuple[ProjectionTarget, ...]:
        if assertion.assertion_type not in self.input_types:
            return ()
        targets = tuple(self.mapper(assertion))
        if len(targets) != len(set(targets)):
            raise ValueError(
                f"projection mapper returned duplicate targets for {self.definition_id}"
            )
        for target in targets:
            if target.view_type != self.view_type:
                raise ValueError(
                    f"projection mapper escaped declared view_type {self.view_type!r}"
                )
        return targets


@dataclass(frozen=True)
class RegisteredTarget:
    """A projection target paired with the definition/version that owns its semantics."""

    definition_id: str
    definition_version: int
    input_types: tuple[str, ...]
    target: ProjectionTarget

    def __post_init__(self) -> None:
        if not self.definition_id.strip():
            raise ValueError("RegisteredTarget.definition_id must be non-empty")
        if self.definition_version < 1:
            raise ValueError("RegisteredTarget.definition_version must be >= 1")
        if not self.input_types:
            raise ValueError("RegisteredTarget.input_types must be non-empty")


class ProjectionRegistry:
    """In-process extension registry; core only dispatches through declared contracts."""

    def __init__(self, definitions: Sequence[ProjectionDefinition]) -> None:
        rows = tuple(definitions)
        ids = [row.definition_id for row in rows]
        if len(ids) != len(set(ids)):
            raise ValueError("projection definition ids must be unique")
        # One current projection slot has one semantic owner. This keeps replacement/currentness
        # unambiguous while still allowing one definition to consume several assertion types.
        view_types = [row.view_type for row in rows]
        if len(view_types) != len(set(view_types)):
            raise ValueError("projection view types must have one owning definition")
        self._definitions = rows
        self._by_id = {row.definition_id: row for row in rows}

    @property
    def definitions(self) -> tuple[ProjectionDefinition, ...]:
        return self._definitions

    def resolve(self, definition_id: str, version: int) -> ProjectionDefinition:
        definition = self._by_id.get(definition_id)
        if definition is None:
            raise KeyError(f"unknown projection definition: {definition_id}")
        if definition.version != version:
            raise KeyError(
                f"projection definition version mismatch for {definition_id}: "
                f"registered={definition.version} requested={version}"
            )
        return definition

    def targets_for_assertion(self, assertion: Assertion) -> tuple[RegisteredTarget, ...]:
        targets: list[RegisteredTarget] = []
        for definition in self._definitions:
            for target in definition.targets_for(assertion):
                targets.append(
                    RegisteredTarget(
                        definition_id=definition.definition_id,
                        definition_version=definition.version,
                        input_types=definition.input_types,
                        target=target,
                    )
                )
        return tuple(targets)


@dataclass(frozen=True)
class RegisteredDirtyProjection:
    definition_id: str
    definition_version: int
    input_types: tuple[str, ...]
    target: ProjectionTarget
    generation: int
    projected_definition_version: int
    projected_generation: int

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("RegisteredDirtyProjection.generation must be >= 1")
        if self.projected_definition_version < 0 or self.projected_generation < 0:
            raise ValueError("projected version/generation must be >= 0")


@dataclass(frozen=True)
class RegisteredDerivation:
    outcome: ProjectionOutcome
    scanned_assertion_count: int
    current_assertion_count: int
    contributing_assertion_count: int


@dataclass(frozen=True)
class RegisteredReconcileResult:
    dirty: RegisteredDirtyProjection
    outcome: ProjectionOutcome
    scanned_assertion_count: int
    current_assertion_count: int
    contributing_assertion_count: int
    applied: bool
    changed: bool
    persisted_definition_version: int
    persisted_generation: int
    current_definition_version: int
    current_generation: int


@dataclass(frozen=True)
class BackfillResult:
    definition_id: str
    definition_version: int
    scanned_assertion_count: int
    target_count: int
    dirtied_target_count: int


def _target_json(target: ProjectionTarget) -> str:
    return _stable_json(
        {
            "view_type": target.view_type,
            "subject_id": target.subject_id,
            "scope": target.scope,
            "key": target.key,
            "dimensions": [list(pair) for pair in target.dimensions],
        }
    )


def _registered_work_key(
    namespace: str,
    definition_id: str,
    target: ProjectionTarget,
) -> str:
    return _hash_parts(namespace, definition_id, _target_json(target))


def _projection_storage_key(namespace: str, target: ProjectionTarget) -> str:
    projection_key = _hash_parts(
        target.view_type,
        target.subject_id,
        target.scope,
        target.key,
        _pairs_json(target.dimensions),
    )
    return _hash_parts(namespace, projection_key)


def _registered_target_dict(
    namespace: str,
    registered: RegisteredTarget,
) -> dict[str, object]:
    target = registered.target
    work_key = _registered_work_key(namespace, registered.definition_id, target)
    return {
        "work_storage_key": work_key,
        "projection_storage_key": _projection_storage_key(namespace, target),
        "definition_id": registered.definition_id,
        "definition_version": registered.definition_version,
        "input_types_json": _stable_json(list(registered.input_types)),
        "view_type": target.view_type,
        "subject_id": target.subject_id,
        "scope": target.scope,
        "key": target.key,
        "dimensions_json": _pairs_json(target.dimensions),
    }


class RegisteredProjectionNeo4jStore(GenerationalNeo4jEnvelopeStore):
    """Generic envelopes plus definition-aware dirty work and backfill markers."""

    def activate(self) -> None:
        super().activate()
        self._neo4j.execute(
            "CREATE CONSTRAINT mutation_registered_work_storage_key IF NOT EXISTS "
            "FOR (w:MutationRegisteredProjectionWork) REQUIRE w.storage_key IS UNIQUE"
        )
        self._neo4j.execute(
            "CREATE CONSTRAINT mutation_projection_backfill_mark_storage_key IF NOT EXISTS "
            "FOR (m:MutationProjectionBackfillMark) REQUIRE m.storage_key IS UNIQUE"
        )

    def record_assertion_for_registered_targets(
        self,
        assertion: Assertion,
        targets: Sequence[RegisteredTarget],
    ) -> dict[str, object]:
        """Atomically persist a new assertion and dirty all registry-resolved output slots."""
        targets = tuple(targets)
        if not targets:
            raise ValueError("registered assertion commit requires at least one target")
        target_rows: list[dict[str, object]] = []
        seen: set[str] = set()
        for registered in targets:
            if assertion.assertion_type not in registered.input_types:
                raise ValueError("registered target does not accept the assertion type")
            row = _registered_target_dict(self.namespace, registered)
            work_key = str(row["work_storage_key"])
            if work_key in seen:
                raise ValueError("registered projection targets must be unique")
            seen.add(work_key)
            target_rows.append(row)

        encoded_value = self.codec.encode(assertion.value)
        payload = _assertion_payload(self, assertion)
        payload_hash = _hash_parts(_stable_json(payload))
        assertion_storage_key = _hash_parts(self.namespace, assertion.assertion_id)
        write_token = uuid.uuid4().hex

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
            FOREACH (target IN CASE
                WHEN created AND a.payload_hash = $payload_hash THEN $targets
                ELSE []
            END |
                MERGE (w:MutationRegisteredProjectionWork {storage_key:target.work_storage_key})
                  ON CREATE SET
                    w.namespace = $namespace,
                    w.projection_storage_key = target.projection_storage_key,
                    w.definition_id = target.definition_id,
                    w.definition_version = target.definition_version,
                    w.input_types_json = target.input_types_json,
                    w.view_type = target.view_type,
                    w.subject_id = target.subject_id,
                    w.scope = target.scope,
                    w.key = target.key,
                    w.dimensions_json = target.dimensions_json,
                    w.generation = 0,
                    w.created_at = datetime()
                SET w.definition_version = target.definition_version,
                    w.input_types_json = target.input_types_json,
                    w.generation = coalesce(w.generation, 0) + 1,
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
                "targets": target_rows,
            },
        )
        if not rows:
            raise RuntimeError("registered assertion/dirty-slot commit returned no result")
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
            "target_count": len(targets),
        }

    def mark_backfill_targets(
        self,
        definition: ProjectionDefinition,
        targets: Sequence[ProjectionTarget],
    ) -> int:
        """Idempotently dirty each historical output slot once per definition version."""
        unique_targets = tuple(sorted(set(targets), key=_target_json))
        if not unique_targets:
            return 0
        write_token = uuid.uuid4().hex
        rows_payload: list[dict[str, object]] = []
        for target in unique_targets:
            registered = RegisteredTarget(
                definition_id=definition.definition_id,
                definition_version=definition.version,
                input_types=definition.input_types,
                target=target,
            )
            row = _registered_target_dict(self.namespace, registered)
            row["marker_storage_key"] = _hash_parts(
                self.namespace,
                definition.definition_id,
                str(definition.version),
                str(row["work_storage_key"]),
            )
            rows_payload.append(row)

        rows = self._neo4j.execute(
            """
            UNWIND $targets AS target
            MERGE (m:MutationProjectionBackfillMark {storage_key:target.marker_storage_key})
              ON CREATE SET
                m.namespace = $namespace,
                m.definition_id = target.definition_id,
                m.definition_version = target.definition_version,
                m.work_storage_key = target.work_storage_key,
                m.created_token = $write_token,
                m.created_at = datetime()
            WITH target, m, (m.created_token = $write_token) AS created
            FOREACH (_ IN CASE WHEN created THEN [1] ELSE [] END |
                MERGE (w:MutationRegisteredProjectionWork {storage_key:target.work_storage_key})
                  ON CREATE SET
                    w.namespace = $namespace,
                    w.projection_storage_key = target.projection_storage_key,
                    w.definition_id = target.definition_id,
                    w.definition_version = target.definition_version,
                    w.input_types_json = target.input_types_json,
                    w.view_type = target.view_type,
                    w.subject_id = target.subject_id,
                    w.scope = target.scope,
                    w.key = target.key,
                    w.dimensions_json = target.dimensions_json,
                    w.generation = 0,
                    w.created_at = datetime()
                SET w.definition_version = target.definition_version,
                    w.input_types_json = target.input_types_json,
                    w.generation = coalesce(w.generation, 0) + 1,
                    w.dirty_at = datetime()
            )
            RETURN sum(CASE WHEN created THEN 1 ELSE 0 END) AS dirtied
            """,
            {
                "namespace": self.namespace,
                "write_token": write_token,
                "targets": rows_payload,
            },
        )
        return int(rows[0].get("dirtied", 0) or 0) if rows else 0

    def load_registered_dirty(self, *, limit: int = 100) -> list[RegisteredDirtyProjection]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        rows = self._neo4j.execute(
            """
            MATCH (w:MutationRegisteredProjectionWork {namespace:$namespace})
            OPTIONAL MATCH (p:MutationProjection {storage_key:w.projection_storage_key})
            WITH w, p,
                 coalesce(p.definition_version, 0) AS projected_definition_version,
                 CASE
                    WHEN p.definition_id = w.definition_id
                         AND coalesce(p.definition_version, 0) = w.definition_version
                    THEN coalesce(p.source_generation, 0)
                    ELSE 0
                 END AS projected_generation
            WHERE p IS NULL
               OR p.definition_id <> w.definition_id
               OR w.definition_version > projected_definition_version
               OR (
                    w.definition_version = projected_definition_version
                    AND w.generation > projected_generation
               )
            RETURN w.definition_id AS definition_id,
                   w.definition_version AS definition_version,
                   w.input_types_json AS input_types_json,
                   w.view_type AS view_type,
                   w.subject_id AS subject_id,
                   w.scope AS scope,
                   w.key AS key,
                   w.dimensions_json AS dimensions_json,
                   w.generation AS generation,
                   projected_definition_version AS projected_definition_version,
                   projected_generation AS projected_generation,
                   w.storage_key AS storage_key
            ORDER BY w.dirty_at ASC, storage_key ASC
            LIMIT $limit
            """,
            {"namespace": self.namespace, "limit": int(limit)},
        )
        return [
            RegisteredDirtyProjection(
                definition_id=str(row["definition_id"]),
                definition_version=int(row["definition_version"]),
                input_types=tuple(
                    str(value)
                    for value in json.loads(str(row.get("input_types_json") or "[]"))
                ),
                target=ProjectionTarget(
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
                projected_definition_version=int(
                    row.get("projected_definition_version", 0) or 0
                ),
                projected_generation=int(row.get("projected_generation", 0) or 0),
            )
            for row in rows
        ]

    def count_registered_dirty(self) -> int:
        rows = self._neo4j.execute(
            """
            MATCH (w:MutationRegisteredProjectionWork {namespace:$namespace})
            OPTIONAL MATCH (p:MutationProjection {storage_key:w.projection_storage_key})
            WITH w, p,
                 coalesce(p.definition_version, 0) AS projected_definition_version,
                 CASE
                    WHEN p.definition_id = w.definition_id
                         AND coalesce(p.definition_version, 0) = w.definition_version
                    THEN coalesce(p.source_generation, 0)
                    ELSE 0
                 END AS projected_generation
            WHERE p IS NULL
               OR p.definition_id <> w.definition_id
               OR w.definition_version > projected_definition_version
               OR (
                    w.definition_version = projected_definition_version
                    AND w.generation > projected_generation
               )
            RETURN count(w) AS n
            """,
            {"namespace": self.namespace},
        )
        return int(rows[0].get("n", 0) or 0) if rows else 0

    def record_registered_outcome(
        self,
        outcome: ProjectionOutcome,
        dirty: RegisteredDirtyProjection,
    ) -> dict[str, object]:
        """Optimistically persist a registered projection without stale-version overwrite."""
        target = dirty.target
        actual_output = _projection_slot(outcome)
        expected_output = (
            target.view_type,
            target.subject_id,
            target.scope,
            target.key,
            target.dimensions,
        )
        if actual_output != expected_output:
            raise ValueError("registered projection outcome escaped its target slot")

        payload = self._encode_outcome(outcome)
        work_storage_key = _registered_work_key(
            self.namespace, dirty.definition_id, target
        )
        projection_storage_key = _projection_storage_key(self.namespace, target)
        projection_key = _hash_parts(
            target.view_type,
            target.subject_id,
            target.scope,
            target.key,
            _pairs_json(target.dimensions),
        )
        projection_hash = _hash_parts(_stable_json(payload))

        rows = self._neo4j.execute(
            """
            MATCH (w:MutationRegisteredProjectionWork {storage_key:$work_storage_key})
            MERGE (p:MutationProjection {storage_key:$projection_storage_key})
            WITH w, p,
                 p.projection_hash AS previous_hash,
                 coalesce(p.source_generation, 0) AS previous_generation,
                 coalesce(p.definition_version, 0) AS previous_definition_version,
                 p.definition_id AS previous_definition_id
            WITH w, p, previous_hash, previous_generation, previous_definition_version,
                 previous_definition_id,
                 CASE
                    WHEN previous_definition_id IS NOT NULL
                         AND previous_definition_id <> $definition_id THEN true
                    ELSE false
                 END AS owner_collision,
                 CASE
                    WHEN w.definition_version <> $definition_version THEN false
                    WHEN previous_definition_id IS NOT NULL
                         AND previous_definition_id <> $definition_id THEN false
                    WHEN $definition_version > previous_definition_version THEN true
                    WHEN $definition_version < previous_definition_version THEN false
                    WHEN $generation > previous_generation THEN true
                    WHEN $generation = previous_generation
                         AND (previous_hash IS NULL OR previous_hash = $projection_hash) THEN true
                    ELSE false
                 END AS can_apply,
                 CASE
                    WHEN previous_definition_id = $definition_id
                         AND previous_definition_version = $definition_version
                         AND previous_generation = $generation
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
                    p.definition_id = $definition_id,
                    p.definition_version = $definition_version,
                    p.source_generation = $generation,
                    p.updated_at = datetime()
            )
            RETURN previous_hash AS previous_hash,
                   previous_generation AS previous_generation,
                   previous_definition_version AS previous_definition_version,
                   p.projection_hash AS projection_hash,
                   coalesce(p.source_generation, 0) AS persisted_generation,
                   coalesce(p.definition_version, 0) AS persisted_definition_version,
                   w.generation AS current_generation,
                   w.definition_version AS current_definition_version,
                   can_apply AS applied,
                   collision AS collision,
                   owner_collision AS owner_collision
            """,
            {
                "namespace": self.namespace,
                "work_storage_key": work_storage_key,
                "projection_storage_key": projection_storage_key,
                "projection_key": projection_key,
                "view_type": target.view_type,
                "subject_id": target.subject_id,
                "scope": target.scope,
                "key": target.key,
                "dimensions_json": _pairs_json(target.dimensions),
                "status": payload["status"],
                "payload_json": _stable_json(payload),
                "projection_hash": projection_hash,
                "definition_id": dirty.definition_id,
                "definition_version": int(dirty.definition_version),
                "generation": int(dirty.generation),
            },
        )
        if not rows:
            raise ValueError("registered projection work no longer exists")
        row = rows[0]
        if bool(row.get("owner_collision")):
            raise ValueError("projection output slot already has a different definition owner")
        if bool(row.get("collision")):
            raise ValueError(
                "projection nondeterminism: one definition version/generation "
                "produced different outcomes"
            )
        previous_hash = row.get("previous_hash")
        applied = bool(row.get("applied"))
        return {
            "applied": applied,
            "changed": applied and previous_hash != projection_hash,
            "persisted_definition_version": int(
                row.get("persisted_definition_version", 0) or 0
            ),
            "persisted_generation": int(row.get("persisted_generation", 0) or 0),
            "current_definition_version": int(
                row.get("current_definition_version", 0) or 0
            ),
            "current_generation": int(row.get("current_generation", 0) or 0),
        }


def route_and_record_assertion(
    store: RegisteredProjectionNeo4jStore,
    registry: ProjectionRegistry,
    assertion: Assertion,
) -> dict[str, object]:
    """Persist one assertion, atomically dirtying only projections declared by extensions."""
    targets = registry.targets_for_assertion(assertion)
    if not targets:
        stored = store.record_assertion(assertion)
        return {**stored, "target_count": 0}
    return store.record_assertion_for_registered_targets(assertion, targets)


def backfill_definition(
    store: RegisteredProjectionNeo4jStore,
    definition: ProjectionDefinition,
) -> BackfillResult:
    """One-time historical scan that discovers output slots for a new definition/version."""
    assertions: list[Assertion] = []
    for assertion_type in definition.input_types:
        assertions.extend(store.load_assertions(assertion_type=assertion_type))
    targets: set[ProjectionTarget] = set()
    for assertion in assertions:
        targets.update(definition.targets_for(assertion))
    dirtied = store.mark_backfill_targets(definition, tuple(targets))
    return BackfillResult(
        definition_id=definition.definition_id,
        definition_version=definition.version,
        scanned_assertion_count=len(assertions),
        target_count=len(targets),
        dirtied_target_count=dirtied,
    )


def derive_registered_projection(
    store: RegisteredProjectionNeo4jStore,
    dirty: RegisteredDirtyProjection,
    definition: ProjectionDefinition,
) -> RegisteredDerivation:
    """Rebuild one registered target from all current input types selected by its mapper."""
    if (
        definition.definition_id != dirty.definition_id
        or definition.version != dirty.definition_version
    ):
        raise ValueError("dirty projection does not match resolved definition")
    if definition.input_types != dirty.input_types:
        raise ValueError("dirty projection input types drifted from the definition")

    scanned: list[Assertion] = []
    for assertion_type in definition.input_types:
        scanned.extend(
            store.load_assertions(
                subject_id=dirty.target.subject_id,
                assertion_type=assertion_type,
                scope=dirty.target.scope,
            )
        )
    # Supersession is applied before target mapping so a corrected interpretation that moves out of
    # this output slot can still remove the older interpretation from current state.
    current = current_assertions(scanned)
    contributing = [
        row
        for row in current
        if dirty.target in definition.targets_for(row)
    ]
    if contributing:
        outcome = definition.fold(contributing)
    else:
        outcome = Abstention(
            view_type=dirty.target.view_type,
            subject_id=dirty.target.subject_id,
            scope=dirty.target.scope,
            key=dirty.target.key,
            reason="no_current_evidence",
            assertion_ids=tuple(row.assertion_id for row in scanned),
            dimensions=dirty.target.dimensions,
        )

    actual_output = _projection_slot(outcome)
    expected_output = (
        dirty.target.view_type,
        dirty.target.subject_id,
        dirty.target.scope,
        dirty.target.key,
        dirty.target.dimensions,
    )
    if actual_output != expected_output:
        raise ValueError(
            "projection definition fold returned an outcome for a different output slot"
        )
    return RegisteredDerivation(
        outcome=outcome,
        scanned_assertion_count=len(scanned),
        current_assertion_count=len(current),
        contributing_assertion_count=len(contributing),
    )


def reconcile_registered_dirty(
    store: RegisteredProjectionNeo4jStore,
    registry: ProjectionRegistry,
    dirty: RegisteredDirtyProjection,
) -> RegisteredReconcileResult:
    definition = registry.resolve(dirty.definition_id, dirty.definition_version)
    derived = derive_registered_projection(store, dirty, definition)
    write = store.record_registered_outcome(derived.outcome, dirty)
    return RegisteredReconcileResult(
        dirty=dirty,
        outcome=derived.outcome,
        scanned_assertion_count=derived.scanned_assertion_count,
        current_assertion_count=derived.current_assertion_count,
        contributing_assertion_count=derived.contributing_assertion_count,
        applied=bool(write["applied"]),
        changed=bool(write["changed"]),
        persisted_definition_version=int(write["persisted_definition_version"]),
        persisted_generation=int(write["persisted_generation"]),
        current_definition_version=int(write["current_definition_version"]),
        current_generation=int(write["current_generation"]),
    )


def reconcile_registered_batch(
    store: RegisteredProjectionNeo4jStore,
    registry: ProjectionRegistry,
    *,
    limit: int = 100,
) -> list[RegisteredReconcileResult]:
    dirty = store.load_registered_dirty(limit=limit)
    return [reconcile_registered_dirty(store, registry, item) for item in dirty]
