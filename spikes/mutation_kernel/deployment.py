"""Shared-registry deployment fences for the mutation-kernel research spike.

This layer pressure-tests two production concerns that sit above the already-proven registry:
1. several Menhir processes may hold different in-process projection-definition snapshots;
2. a newer definition version may stop owning an output target that an older version materialized.

A shared registry epoch fences assertion routing at commit time. Definition-target ownership markers let
an upgrade explicitly retire projections that the new mapper no longer produces.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import uuid
from typing import Sequence

from .dirty import _assertion_payload
from .kernel import Abstention, Assertion, Retirement, View
from .neo4j_store import _hash_parts, _pairs_json, _stable_json
from .registry import (
    ProjectionDefinition,
    ProjectionRegistry,
    ProjectionTarget,
    RegisteredProjectionNeo4jStore,
    RegisteredTarget,
    _projection_storage_key,
    _registered_target_dict,
    _target_json,
)


class StaleRegistryError(RuntimeError):
    """The caller's in-process projection registry is no longer the shared current registry."""


@dataclass(frozen=True)
class RegistrySnapshot:
    epoch: int
    definitions: tuple[tuple[str, int, str], ...]

    def __post_init__(self) -> None:
        if self.epoch < 0:
            raise ValueError("RegistrySnapshot.epoch must be >= 0")

    def assert_matches(self, registry: ProjectionRegistry) -> None:
        local = tuple(
            sorted(
                (
                    definition.definition_id,
                    definition.version,
                    _definition_contract_hash(definition),
                )
                for definition in registry.definitions
            )
        )
        if local != self.definitions:
            raise StaleRegistryError(
                "in-process projection registry does not match the captured shared snapshot"
            )


@dataclass(frozen=True)
class DefinitionSyncResult:
    definition_id: str
    definition_version: int
    scanned_assertion_count: int
    current_target_count: int
    dirtied_target_count: int
    removed_target_count: int
    retired_target_count: int


@dataclass(frozen=True)
class PendingRetirement:
    target: ProjectionTarget
    effective_at: str


def _registry_storage_key(namespace: str) -> str:
    return _hash_parts(namespace, "projection-registry")


def _definition_storage_key(namespace: str, definition_id: str) -> str:
    return _hash_parts(namespace, "projection-definition", definition_id)


def _definition_contract_hash(definition: ProjectionDefinition) -> str:
    """Hash declarative metadata.

    Mapper/fold callables are intentionally not introspected. The extension promises to bump the
    definition version whenever their semantics change.
    """
    return _hash_parts(
        _stable_json(
            {
                "definition_id": definition.definition_id,
                "version": definition.version,
                "input_types": list(definition.input_types),
                "view_type": definition.view_type,
            }
        )
    )


def _owner_marker_storage_key(
    namespace: str,
    definition_id: str,
    target: ProjectionTarget,
) -> str:
    return _hash_parts(namespace, "projection-target-owner", definition_id, _target_json(target))


class CoordinatedProjectionNeo4jStore(RegisteredProjectionNeo4jStore):
    """Registered store plus a shared definition epoch and target-ownership lifecycle."""

    def activate(self) -> None:
        super().activate()
        self._neo4j.execute(
            "CREATE CONSTRAINT mutation_projection_registry_storage_key IF NOT EXISTS "
            "FOR (r:MutationProjectionRegistry) REQUIRE r.storage_key IS UNIQUE"
        )
        self._neo4j.execute(
            "CREATE CONSTRAINT mutation_projection_definition_storage_key IF NOT EXISTS "
            "FOR (d:MutationProjectionDefinition) REQUIRE d.storage_key IS UNIQUE"
        )
        self._neo4j.execute(
            "CREATE CONSTRAINT mutation_projection_target_owner_storage_key IF NOT EXISTS "
            "FOR (o:MutationProjectionTargetOwner) REQUIRE o.storage_key IS UNIQUE"
        )

    def publish_definition(self, definition: ProjectionDefinition) -> dict[str, object]:
        """Advance one shared definition monotonically and bump the global routing epoch."""
        contract_hash = _definition_contract_hash(definition)
        rows = self._neo4j.execute(
            """
            MERGE (r:MutationProjectionRegistry {storage_key:$registry_storage_key})
              ON CREATE SET
                r.namespace = $namespace,
                r.epoch = 0,
                r.created_at = datetime()
            MERGE (d:MutationProjectionDefinition {storage_key:$definition_storage_key})
              ON CREATE SET
                d.namespace = $namespace,
                d.definition_id = $definition_id,
                d.current_version = 0,
                d.created_at = datetime()
            WITH r, d,
                 coalesce(d.current_version, 0) AS old_version,
                 d.contract_hash AS old_hash
            WITH r, d, old_version, old_hash,
                 CASE
                    WHEN old_version > $definition_version THEN 'stale'
                    WHEN old_version = $definition_version
                         AND old_hash IS NOT NULL
                         AND old_hash <> $contract_hash THEN 'collision'
                    WHEN old_version = $definition_version THEN 'same'
                    ELSE 'advance'
                 END AS action
            FOREACH (_ IN CASE WHEN action = 'advance' THEN [1] ELSE [] END |
                SET d.current_version = $definition_version,
                    d.contract_hash = $contract_hash,
                    d.input_types_json = $input_types_json,
                    d.view_type = $view_type,
                    d.activated_at = datetime(),
                    r.epoch = coalesce(r.epoch, 0) + 1,
                    r.updated_at = datetime()
            )
            RETURN action AS action,
                   coalesce(r.epoch, 0) AS epoch,
                   coalesce(d.current_version, 0) AS current_version,
                   d.contract_hash AS contract_hash,
                   toString(d.activated_at) AS activated_at
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
                "input_types_json": _stable_json(list(definition.input_types)),
                "view_type": definition.view_type,
            },
        )
        if not rows:
            raise RuntimeError("projection definition publish returned no result")
        row = rows[0]
        action = str(row.get("action") or "")
        if action == "stale":
            raise StaleRegistryError(
                f"cannot publish stale {definition.definition_id} v{definition.version}; "
                f"shared version is {row.get('current_version')}"
            )
        if action == "collision":
            raise ValueError(
                "projection definition changed without a version bump: "
                f"{definition.definition_id} v{definition.version}"
            )
        return {
            "action": action,
            "epoch": int(row.get("epoch", 0) or 0),
            "current_version": int(row.get("current_version", 0) or 0),
            "activated_at": str(row.get("activated_at") or ""),
        }

    def capture_registry_snapshot(self, registry: ProjectionRegistry) -> RegistrySnapshot:
        registry_rows = self._neo4j.execute(
            """
            MATCH (r:MutationProjectionRegistry {storage_key:$registry_storage_key})
            RETURN coalesce(r.epoch, 0) AS epoch
            """,
            {"registry_storage_key": _registry_storage_key(self.namespace)},
        )
        if not registry_rows:
            raise StaleRegistryError("shared projection registry has not been published")
        definition_rows = self._neo4j.execute(
            """
            MATCH (d:MutationProjectionDefinition {namespace:$namespace})
            WHERE coalesce(d.current_version, 0) > 0
            RETURN d.definition_id AS definition_id,
                   d.current_version AS definition_version,
                   d.contract_hash AS contract_hash
            ORDER BY definition_id ASC
            """,
            {"namespace": self.namespace},
        )
        shared = tuple(
            (
                str(row["definition_id"]),
                int(row["definition_version"]),
                str(row["contract_hash"]),
            )
            for row in definition_rows
        )
        snapshot = RegistrySnapshot(
            epoch=int(registry_rows[0].get("epoch", 0) or 0),
            definitions=shared,
        )
        snapshot.assert_matches(registry)
        return snapshot

    def assert_snapshot_current(self, snapshot: RegistrySnapshot) -> None:
        rows = self._neo4j.execute(
            """
            MATCH (r:MutationProjectionRegistry {storage_key:$registry_storage_key})
            RETURN coalesce(r.epoch, 0) AS epoch
            """,
            {"registry_storage_key": _registry_storage_key(self.namespace)},
        )
        current = int(rows[0].get("epoch", -1)) if rows else -1
        if current != snapshot.epoch:
            raise StaleRegistryError(
                f"projection registry epoch changed: snapshot={snapshot.epoch} current={current}"
            )

    def record_assertion_with_snapshot(
        self,
        assertion: Assertion,
        registry: ProjectionRegistry,
        snapshot: RegistrySnapshot,
    ) -> dict[str, object]:
        """Route and commit an assertion only if the mapper snapshot is still current.

        The registry epoch check and assertion/work write happen in one Cypher transaction, closing
        the check-then-write race between independently upgraded processes.
        """
        snapshot.assert_matches(registry)
        targets = registry.targets_for_assertion(assertion)
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
            row["owner_storage_key"] = _owner_marker_storage_key(
                self.namespace, registered.definition_id, registered.target
            )
            row["target_json"] = _target_json(registered.target)
            target_rows.append(row)

        encoded_value = self.codec.encode(assertion.value)
        payload = _assertion_payload(self, assertion)
        payload_hash = _hash_parts(_stable_json(payload))
        assertion_storage_key = _hash_parts(self.namespace, assertion.assertion_id)
        write_token = uuid.uuid4().hex

        rows = self._neo4j.execute(
            """
            MATCH (r:MutationProjectionRegistry {storage_key:$registry_storage_key})
            WHERE coalesce(r.epoch, 0) = $expected_epoch
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
            WITH r, a, (a.created_token = $write_token) AS created
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
                MERGE (o:MutationProjectionTargetOwner {storage_key:target.owner_storage_key})
                  ON CREATE SET
                    o.namespace = $namespace,
                    o.definition_id = target.definition_id,
                    o.target_json = target.target_json,
                    o.created_at = datetime()
                SET o.active = true,
                    o.last_seen_version = target.definition_version,
                    o.pending_retire_version = null,
                    o.pending_retire_at = null,
                    o.updated_at = datetime()
            )
            RETURN a.payload_hash AS payload_hash,
                   created AS created,
                   coalesce(r.epoch, 0) AS epoch
            """,
            {
                "registry_storage_key": _registry_storage_key(self.namespace),
                "expected_epoch": int(snapshot.epoch),
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
                "evidence_json": _stable_json(
                    [
                        {
                            "source_id": ref.source_id,
                            "source_kind": ref.source_kind,
                            "span_start": ref.span_start,
                            "span_end": ref.span_end,
                            "note": ref.note,
                        }
                        for ref in assertion.evidence
                    ]
                ),
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
            raise StaleRegistryError(
                "projection registry changed before the assertion routing commit"
            )
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
            "epoch": int(rows[0].get("epoch", 0) or 0),
        }

    def mark_backfill_targets_with_snapshot(
        self,
        definition: ProjectionDefinition,
        targets: Sequence[ProjectionTarget],
        snapshot: RegistrySnapshot,
    ) -> int:
        unique_targets = tuple(sorted(set(targets), key=_target_json))
        if not unique_targets:
            self.assert_snapshot_current(snapshot)
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
            row["owner_storage_key"] = _owner_marker_storage_key(
                self.namespace, definition.definition_id, target
            )
            row["target_json"] = _target_json(target)
            rows_payload.append(row)

        rows = self._neo4j.execute(
            """
            MATCH (r:MutationProjectionRegistry {storage_key:$registry_storage_key})
            WHERE coalesce(r.epoch, 0) = $expected_epoch
            MATCH (d:MutationProjectionDefinition {storage_key:$definition_storage_key})
            WHERE d.current_version = $definition_version
            UNWIND $targets AS target
            MERGE (o:MutationProjectionTargetOwner {storage_key:target.owner_storage_key})
              ON CREATE SET
                o.namespace = $namespace,
                o.definition_id = target.definition_id,
                o.target_json = target.target_json,
                o.created_at = datetime()
            SET o.active = true,
                o.last_seen_version = target.definition_version,
                o.pending_retire_version = null,
                o.pending_retire_at = null,
                o.updated_at = datetime()
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
                "registry_storage_key": _registry_storage_key(self.namespace),
                "definition_storage_key": _definition_storage_key(
                    self.namespace, definition.definition_id
                ),
                "expected_epoch": int(snapshot.epoch),
                "definition_version": int(definition.version),
                "namespace": self.namespace,
                "write_token": write_token,
                "targets": rows_payload,
            },
        )
        if not rows:
            raise StaleRegistryError(
                "projection definition changed before historical backfill was registered"
            )
        return int(rows[0].get("dirtied", 0) or 0)

    def mark_removed_targets(
        self,
        definition: ProjectionDefinition,
        current_targets: Sequence[ProjectionTarget],
        snapshot: RegistrySnapshot,
    ) -> tuple[PendingRetirement, ...]:
        current_json = [_target_json(target) for target in sorted(set(current_targets), key=_target_json)]
        rows = self._neo4j.execute(
            """
            MATCH (r:MutationProjectionRegistry {storage_key:$registry_storage_key})
            WHERE coalesce(r.epoch, 0) = $expected_epoch
            MATCH (d:MutationProjectionDefinition {storage_key:$definition_storage_key})
            WHERE d.current_version = $definition_version
            OPTIONAL MATCH (o:MutationProjectionTargetOwner {
                namespace:$namespace,
                definition_id:$definition_id,
                active:true
            })
            WITH r, d, [item IN collect(o) WHERE item IS NOT NULL
                       AND NOT (item.target_json IN $current_targets_json)] AS removed
            FOREACH (o IN removed |
                SET o.pending_retire_version = $definition_version,
                    o.pending_retire_at = toString(d.activated_at),
                    o.updated_at = datetime()
            )
            RETURN [o IN removed | {
                target_json:o.target_json,
                effective_at:o.pending_retire_at
            }] AS removed
            """,
            {
                "registry_storage_key": _registry_storage_key(self.namespace),
                "definition_storage_key": _definition_storage_key(
                    self.namespace, definition.definition_id
                ),
                "expected_epoch": int(snapshot.epoch),
                "definition_version": int(definition.version),
                "namespace": self.namespace,
                "definition_id": definition.definition_id,
                "current_targets_json": current_json,
            },
        )
        if not rows:
            raise StaleRegistryError(
                "projection definition changed while removed targets were being discovered"
            )
        raw_removed = rows[0].get("removed") or []
        pending: list[PendingRetirement] = []
        for item in raw_removed:
            payload = json.loads(str(item["target_json"]))
            pending.append(
                PendingRetirement(
                    target=ProjectionTarget(
                        view_type=str(payload["view_type"]),
                        subject_id=str(payload["subject_id"]),
                        scope=str(payload.get("scope") or ""),
                        key=str(payload["key"]),
                        dimensions=tuple(
                            (str(name), str(value))
                            for name, value in payload.get("dimensions", [])
                        ),
                    ),
                    effective_at=str(item.get("effective_at") or ""),
                )
            )
        return tuple(pending)

    def retire_removed_target(
        self,
        definition: ProjectionDefinition,
        pending: PendingRetirement,
        snapshot: RegistrySnapshot,
    ) -> bool:
        target = pending.target
        previous = self.load_outcomes(
            subject_id=target.subject_id,
            view_type=target.view_type,
            scope=target.scope,
            key=target.key,
            dimensions=target.dimensions,
        )
        retired_value = None
        contributor_ids: tuple[str, ...] = ()
        if previous:
            prior = previous[0]
            if isinstance(prior, View):
                retired_value = prior.value
                contributor_ids = prior.contributor_ids
            elif isinstance(prior, Retirement):
                retired_value = prior.retired_value
                contributor_ids = prior.contributor_ids
            elif isinstance(prior, Abstention):
                contributor_ids = prior.assertion_ids

        retirement = Retirement(
            view_type=target.view_type,
            subject_id=target.subject_id,
            scope=target.scope,
            key=target.key,
            reason="definition_no_longer_maps_target",
            effective_at=pending.effective_at,
            contributor_ids=contributor_ids,
            retired_value=retired_value,
            dimensions=target.dimensions,
        )
        payload = self._encode_outcome(retirement)
        projection_storage_key = _projection_storage_key(self.namespace, target)
        projection_key = _hash_parts(
            target.view_type,
            target.subject_id,
            target.scope,
            target.key,
            _pairs_json(target.dimensions),
        )
        projection_hash = _hash_parts(_stable_json(payload))
        owner_storage_key = _owner_marker_storage_key(
            self.namespace, definition.definition_id, target
        )

        rows = self._neo4j.execute(
            """
            MATCH (r:MutationProjectionRegistry {storage_key:$registry_storage_key})
            WHERE coalesce(r.epoch, 0) = $expected_epoch
            MATCH (d:MutationProjectionDefinition {storage_key:$definition_storage_key})
            WHERE d.current_version = $definition_version
            MATCH (o:MutationProjectionTargetOwner {storage_key:$owner_storage_key})
            WHERE o.pending_retire_version = $definition_version
            MERGE (p:MutationProjection {storage_key:$projection_storage_key})
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
                p.source_generation = 0,
                p.updated_at = datetime(),
                o.active = false,
                o.retired_version = $definition_version,
                o.pending_retire_version = null,
                o.pending_retire_at = null,
                o.updated_at = datetime()
            RETURN p.projection_hash AS projection_hash
            """,
            {
                "registry_storage_key": _registry_storage_key(self.namespace),
                "definition_storage_key": _definition_storage_key(
                    self.namespace, definition.definition_id
                ),
                "owner_storage_key": owner_storage_key,
                "projection_storage_key": projection_storage_key,
                "expected_epoch": int(snapshot.epoch),
                "definition_version": int(definition.version),
                "namespace": self.namespace,
                "projection_key": projection_key,
                "view_type": target.view_type,
                "subject_id": target.subject_id,
                "scope": target.scope,
                "key": target.key,
                "dimensions_json": _pairs_json(target.dimensions),
                "status": payload["status"],
                "payload_json": _stable_json(payload),
                "projection_hash": projection_hash,
                "definition_id": definition.definition_id,
            },
        )
        if not rows:
            self.assert_snapshot_current(snapshot)
            return False
        return True


def sync_definition_targets(
    store: CoordinatedProjectionNeo4jStore,
    definition: ProjectionDefinition,
    registry: ProjectionRegistry,
    snapshot: RegistrySnapshot,
) -> DefinitionSyncResult:
    """Backfill current targets and retire targets removed by a newer definition version."""
    snapshot.assert_matches(registry)
    local = registry.resolve(definition.definition_id, definition.version)
    if local is not definition and (
        local.definition_id != definition.definition_id
        or local.version != definition.version
    ):
        raise ValueError("definition does not belong to the supplied registry")

    assertions: list[Assertion] = []
    for assertion_type in definition.input_types:
        assertions.extend(store.load_assertions(assertion_type=assertion_type))
    targets: set[ProjectionTarget] = set()
    for assertion in assertions:
        targets.update(definition.targets_for(assertion))

    dirtied = store.mark_backfill_targets_with_snapshot(
        definition, tuple(targets), snapshot
    )
    pending = store.mark_removed_targets(definition, tuple(targets), snapshot)
    retired = sum(
        1
        for item in pending
        if store.retire_removed_target(definition, item, snapshot)
    )
    return DefinitionSyncResult(
        definition_id=definition.definition_id,
        definition_version=definition.version,
        scanned_assertion_count=len(assertions),
        current_target_count=len(targets),
        dirtied_target_count=dirtied,
        removed_target_count=len(pending),
        retired_target_count=retired,
    )
