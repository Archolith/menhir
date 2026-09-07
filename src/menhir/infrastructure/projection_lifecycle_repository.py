"""Durable projection lifecycle sidecars and generation/version fencing.

This repository owns no assertion semantics and no View payload schema. It stores only the shared
semantic-definition version, durable target membership/work generations, immutable derivation
receipts, and the freshness certificate for the currently applied generation.

The materialization callback runs inside the same Neo4j transaction as the work fence. It receives a
transaction-scoped ``execute`` adapter, so an existing View repository can be instantiated against
that adapter. If the fence fails, any View mutation performed by the callback rolls back with it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from menhir.domain.projection import ProjectionDefinition, ProjectionTarget
from menhir.domain.projection_lifecycle import (
    ProjectionDefinitionNotPublishedError,
    ProjectionDefinitionPublication,
    ProjectionFreshnessAssessment,
    ProjectionFreshnessCertificate,
    ProjectionLifecycleCorruptionError,
    ProjectionWorkAlreadyCompletedError,
    ProjectionWorkToken,
    StaleProjectionDefinitionError,
    StaleProjectionWorkError,
)
from menhir.infrastructure.neo4j import Neo4jTransaction

__all__ = [
    "PROJECTION_LIFECYCLE_SCHEMA_QUERIES",
    "ProjectionLifecycleRepository",
    "projection_lifecycle_schema_queries",
]


PROJECTION_LIFECYCLE_SCHEMA_QUERIES: tuple[str, ...] = (
    "CREATE CONSTRAINT projection_definition_state_id_unique IF NOT EXISTS "
    "FOR (d:ProjectionDefinitionState) REQUIRE d.definition_id IS UNIQUE",
    "CREATE CONSTRAINT projection_work_state_key_unique IF NOT EXISTS "
    "FOR (w:ProjectionWorkState) REQUIRE w.work_key IS UNIQUE",
    "CREATE INDEX projection_work_state_definition_idx IF NOT EXISTS "
    "FOR (w:ProjectionWorkState) ON (w.definition_id)",
    "CREATE CONSTRAINT projection_derivation_receipt_key_unique IF NOT EXISTS "
    "FOR (r:ProjectionDerivationReceipt) REQUIRE r.receipt_key IS UNIQUE",
    "CREATE INDEX projection_derivation_receipt_derivation_idx IF NOT EXISTS "
    "FOR (r:ProjectionDerivationReceipt) ON (r.derivation_id)",
)


def projection_lifecycle_schema_queries() -> list[str]:
    """Return the additive DDL required by the projection lifecycle sidecars."""

    return list(PROJECTION_LIFECYCLE_SCHEMA_QUERIES)


def _require_nonblank(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")
    return value.strip()


def _target_payload(target: ProjectionTarget) -> dict[str, object]:
    if not isinstance(target, ProjectionTarget):
        raise TypeError("target must be a ProjectionTarget")
    return {
        "namespace": target.namespace,
        "subject_id": target.subject_id,
        "key": list(target.key),
    }


def _stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _target_json(target: ProjectionTarget) -> str:
    return _stable_json(_target_payload(target))


def _target_from_json(raw: object) -> ProjectionTarget:
    try:
        payload = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ProjectionLifecycleCorruptionError("persisted projection target is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ProjectionLifecycleCorruptionError("persisted projection target is not an object")
    try:
        namespace = payload.get("namespace")
        subject_id = payload["subject_id"]
        key = payload.get("key", [])
        if namespace is not None and not isinstance(namespace, str):
            raise TypeError("namespace must be a string or null")
        if not isinstance(subject_id, str):
            raise TypeError("subject_id must be a string")
        if not isinstance(key, list):
            raise TypeError("key must be a list")
        if any(not isinstance(part, str) for part in key):
            raise TypeError("key parts must be strings")
        return ProjectionTarget(
            namespace=namespace,
            subject_id=subject_id,
            key=tuple(key),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProjectionLifecycleCorruptionError(
            "persisted projection target does not satisfy ProjectionTarget"
        ) from exc


def _work_key(definition_id: str, target: ProjectionTarget) -> str:
    material = f"{definition_id}\0{_target_json(target)}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _receipt_key(work_key: str, generation: int) -> str:
    return hashlib.sha256(f"{work_key}\0{generation}".encode("utf-8")).hexdigest()


def _unique_targets(targets: Iterable[ProjectionTarget]) -> tuple[ProjectionTarget, ...]:
    rows = tuple(targets)
    keys: set[str] = set()
    for target in rows:
        if not isinstance(target, ProjectionTarget):
            raise TypeError("targets must contain ProjectionTarget values")
        encoded = _target_json(target)
        if encoded in keys:
            raise ValueError("projection targets must be unique")
        keys.add(encoded)
    return tuple(sorted(rows, key=lambda target: target.sort_key))


def _work_token_from_row(row: dict[str, Any]) -> ProjectionWorkToken:
    target = _target_from_json(row.get("target_json"))
    definition_id = str(row.get("definition_id") or "")
    work_key = str(row.get("work_key") or "")
    expected_key = _work_key(definition_id, target)
    if work_key != expected_key:
        raise ProjectionLifecycleCorruptionError(
            "persisted projection work_key does not match definition/target identity"
        )
    try:
        definition_version = int(row["definition_version"])
        generation = int(row["generation"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ProjectionLifecycleCorruptionError(
            "persisted projection work is missing version/generation"
        ) from exc
    target_present = row.get("target_present")
    if not isinstance(target_present, bool):
        raise ProjectionLifecycleCorruptionError(
            "persisted projection target_present must be boolean"
        )
    reason = str(row.get("reason") or "")
    try:
        return ProjectionWorkToken(
            work_key=work_key,
            definition_id=definition_id,
            definition_version=definition_version,
            target=target,
            generation=generation,
            target_present=target_present,
            reason=reason,
        )
    except (TypeError, ValueError) as exc:
        raise ProjectionLifecycleCorruptionError(
            "persisted projection work token is invalid"
        ) from exc


MaterializationCallback = Callable[[Neo4jTransaction, ProjectionWorkToken], str]


class ProjectionLifecycleRepository:
    """Neo4j-backed lifecycle sidecar for generic projection definitions."""

    def __init__(self, neo4j: Any) -> None:
        self._neo4j = neo4j

    def activate(self) -> None:
        """Create only the additive lifecycle constraints/indexes."""

        for query in PROJECTION_LIFECYCLE_SCHEMA_QUERIES:
            self._neo4j.execute(query)

    @staticmethod
    def work_key(definition_id: str, target: ProjectionTarget) -> str:
        """Return the stable durable work identity for one definition/target."""

        return _work_key(_require_nonblank("definition_id", definition_id), target)

    def publish_definition(
        self,
        definition: ProjectionDefinition,
        *,
        semantic_hash: str,
    ) -> ProjectionDefinitionPublication:
        """Publish one shared-current semantic version and invalidate all known targets atomically.

        Replaying the exact same version/hash is idempotent. Reusing a version with a different
        semantic hash fails closed, and an older publisher can never move the shared version back.
        """

        if not isinstance(definition, ProjectionDefinition):
            raise TypeError("definition must be a ProjectionDefinition")
        semantic_hash = _require_nonblank("semantic_hash", semantic_hash)

        def work(tx: Neo4jTransaction) -> ProjectionDefinitionPublication:
            rows = tx.execute(
                """
                MERGE (d:ProjectionDefinitionState {definition_id:$definition_id})
                  ON CREATE SET d.current_version=0,
                                d.semantic_hash='',
                                d.created_at=datetime(),
                                d.coordination_epoch=0
                SET d.coordination_epoch=coalesce(d.coordination_epoch,0)+1
                RETURN d.current_version AS current_version,
                       d.semantic_hash AS current_semantic_hash
                """,
                {"definition_id": definition.definition_id},
            )
            if len(rows) != 1:
                raise ProjectionLifecycleCorruptionError(
                    "definition publication did not resolve exactly one state row"
                )
            current_version = int(rows[0].get("current_version", 0) or 0)
            current_hash = str(rows[0].get("current_semantic_hash") or "")

            if current_version > definition.version:
                raise StaleProjectionDefinitionError(
                    f"projection definition {definition.definition_id!r} is already at "
                    f"version {current_version}, not {definition.version}"
                )
            if current_version == definition.version:
                if current_hash != semantic_hash:
                    raise ProjectionLifecycleCorruptionError(
                        "projection semantic hash changed without a definition version bump"
                    )
                return ProjectionDefinitionPublication(
                    definition_id=definition.definition_id,
                    version=definition.version,
                    semantic_hash=semantic_hash,
                    changed=False,
                    invalidated_targets=0,
                )

            updated = tx.execute(
                """
                MATCH (d:ProjectionDefinitionState {definition_id:$definition_id})
                SET d.current_version=$definition_version,
                    d.semantic_hash=$semantic_hash,
                    d.published_at=datetime()
                WITH d
                OPTIONAL MATCH (w:ProjectionWorkState {definition_id:$definition_id})
                WITH d, collect(w) AS works
                FOREACH (w IN works |
                    SET w.definition_version=$definition_version,
                        w.generation=coalesce(w.generation,0)+1,
                        w.reason='definition_published',
                        w.dirty_at=datetime(),
                        w.certified_definition_version=null,
                        w.certified_generation=null,
                        w.certified_projection_hash=null,
                        w.certified_derivation_id=null,
                        w.certified_receipt_key=null,
                        w.certified_at=null
                )
                RETURN size(works) AS invalidated_targets
                """,
                {
                    "definition_id": definition.definition_id,
                    "definition_version": definition.version,
                    "semantic_hash": semantic_hash,
                },
            )
            if len(updated) != 1:
                raise ProjectionLifecycleCorruptionError(
                    "definition publication failed to update shared-current state"
                )
            return ProjectionDefinitionPublication(
                definition_id=definition.definition_id,
                version=definition.version,
                semantic_hash=semantic_hash,
                changed=True,
                invalidated_targets=int(updated[0].get("invalidated_targets", 0) or 0),
            )

        return self._neo4j.execute_write(work)

    def _require_current_definition(
        self,
        tx: Neo4jTransaction,
        *,
        definition_id: str,
        definition_version: int,
    ) -> None:
        rows = tx.execute(
            """
            MATCH (d:ProjectionDefinitionState {definition_id:$definition_id})
            SET d.coordination_epoch=coalesce(d.coordination_epoch,0)+1
            RETURN d.current_version AS current_version
            """,
            {"definition_id": definition_id},
        )
        if not rows:
            raise ProjectionDefinitionNotPublishedError(
                f"projection definition {definition_id!r} is not published"
            )
        if len(rows) != 1:
            raise ProjectionLifecycleCorruptionError(
                "projection definition identity is not unique"
            )
        current_version = int(rows[0].get("current_version", 0) or 0)
        if current_version != definition_version:
            raise StaleProjectionDefinitionError(
                f"projection definition {definition_id!r} current version is "
                f"{current_version}, not {definition_version}"
            )

    def _dirty_targets_tx(
        self,
        tx: Neo4jTransaction,
        *,
        definition_id: str,
        definition_version: int,
        targets: Sequence[ProjectionTarget],
        target_present: bool,
        reason: str,
    ) -> tuple[ProjectionWorkToken, ...]:
        payloads = [
            {
                "work_key": _work_key(definition_id, target),
                "target_json": _target_json(target),
            }
            for target in targets
        ]
        if not payloads:
            return ()
        rows = tx.execute(
            """
            UNWIND $targets AS row
            MERGE (w:ProjectionWorkState {work_key:row.work_key})
              ON CREATE SET w.definition_id=$definition_id,
                            w.target_json=row.target_json,
                            w.generation=0,
                            w.applied_generation=0,
                            w.created_at=datetime()
            SET w.definition_version=$definition_version,
                w.generation=coalesce(w.generation,0)+1,
                w.target_present=$target_present,
                w.reason=$reason,
                w.dirty_at=datetime(),
                w.certified_definition_version=null,
                w.certified_generation=null,
                w.certified_projection_hash=null,
                w.certified_derivation_id=null,
                w.certified_receipt_key=null,
                w.certified_at=null
            RETURN w.work_key AS work_key,
                   w.definition_id AS definition_id,
                   w.definition_version AS definition_version,
                   w.target_json AS target_json,
                   w.generation AS generation,
                   w.target_present AS target_present,
                   w.reason AS reason
            """,
            {
                "definition_id": definition_id,
                "definition_version": definition_version,
                "targets": payloads,
                "target_present": target_present,
                "reason": reason,
            },
        )
        if len(rows) != len(payloads):
            raise ProjectionLifecycleCorruptionError(
                "dirty target write did not return every requested work row"
            )
        tokens = tuple(_work_token_from_row(dict(row)) for row in rows)
        expected = {_target_json(target) for target in targets}
        actual = {_target_json(token.target) for token in tokens}
        if expected != actual:
            raise ProjectionLifecycleCorruptionError(
                "dirty target write returned a different target set"
            )
        return tuple(sorted(tokens, key=lambda token: token.target.sort_key))

    def dirty_targets(
        self,
        definition: ProjectionDefinition,
        targets: Sequence[ProjectionTarget],
        *,
        reason: str,
    ) -> tuple[ProjectionWorkToken, ...]:
        """Advance generations only for explicitly affected targets."""

        if not isinstance(definition, ProjectionDefinition):
            raise TypeError("definition must be a ProjectionDefinition")
        reason = _require_nonblank("reason", reason)
        targets = _unique_targets(targets)
        if not targets:
            raise ValueError("dirty_targets requires at least one target")

        def work(tx: Neo4jTransaction) -> tuple[ProjectionWorkToken, ...]:
            self._require_current_definition(
                tx,
                definition_id=definition.definition_id,
                definition_version=definition.version,
            )
            return self._dirty_targets_tx(
                tx,
                definition_id=definition.definition_id,
                definition_version=definition.version,
                targets=targets,
                target_present=True,
                reason=reason,
            )

        return self._neo4j.execute_write(work)

    def retire_targets(
        self,
        definition: ProjectionDefinition,
        targets: Sequence[ProjectionTarget],
        *,
        reason: str,
    ) -> tuple[ProjectionWorkToken, ...]:
        """Advance generations for explicit removals so workers can retire persisted targets."""

        if not isinstance(definition, ProjectionDefinition):
            raise TypeError("definition must be a ProjectionDefinition")
        reason = _require_nonblank("reason", reason)
        targets = _unique_targets(targets)
        if not targets:
            raise ValueError("retire_targets requires at least one target")

        def work(tx: Neo4jTransaction) -> tuple[ProjectionWorkToken, ...]:
            self._require_current_definition(
                tx,
                definition_id=definition.definition_id,
                definition_version=definition.version,
            )
            return self._dirty_targets_tx(
                tx,
                definition_id=definition.definition_id,
                definition_version=definition.version,
                targets=targets,
                target_present=False,
                reason=reason,
            )

        return self._neo4j.execute_write(work)

    def reconcile_all_targets(
        self,
        definition: ProjectionDefinition,
        desired_targets: Sequence[ProjectionTarget],
        *,
        reason: str = "target_set_reconciled",
    ) -> tuple[ProjectionWorkToken, ...]:
        """Reconcile the authoritative complete target set for one definition.

        This is deliberately named ``all``: callers with only a partial affected set must use
        ``dirty_targets``. Existing present targets omitted from this complete desired set are dirtied
        with ``target_present=False``; previously retired targets that return are dirtied present
        again. Unchanged membership does not advance a generation.
        """

        if not isinstance(definition, ProjectionDefinition):
            raise TypeError("definition must be a ProjectionDefinition")
        reason = _require_nonblank("reason", reason)
        desired_targets = _unique_targets(desired_targets)

        def work(tx: Neo4jTransaction) -> tuple[ProjectionWorkToken, ...]:
            self._require_current_definition(
                tx,
                definition_id=definition.definition_id,
                definition_version=definition.version,
            )
            existing_rows = tx.execute(
                """
                MATCH (w:ProjectionWorkState {definition_id:$definition_id})
                RETURN w.work_key AS work_key,
                       w.definition_id AS definition_id,
                       w.definition_version AS definition_version,
                       w.target_json AS target_json,
                       w.generation AS generation,
                       w.target_present AS target_present,
                       w.reason AS reason
                """,
                {"definition_id": definition.definition_id},
            )
            existing: dict[str, ProjectionWorkToken] = {}
            for row in existing_rows:
                token = _work_token_from_row(dict(row))
                if token.definition_version != definition.version:
                    raise ProjectionLifecycleCorruptionError(
                        "target index contains work from a non-current definition version"
                    )
                encoded = _target_json(token.target)
                if encoded in existing:
                    raise ProjectionLifecycleCorruptionError(
                        "target index contains duplicate projection identities"
                    )
                existing[encoded] = token

            desired = {_target_json(target): target for target in desired_targets}
            make_present = tuple(
                desired[key]
                for key in sorted(desired)
                if key not in existing or not existing[key].target_present
            )
            make_absent = tuple(
                token.target
                for key, token in sorted(existing.items())
                if token.target_present and key not in desired
            )

            changed: list[ProjectionWorkToken] = []
            changed.extend(
                self._dirty_targets_tx(
                    tx,
                    definition_id=definition.definition_id,
                    definition_version=definition.version,
                    targets=make_present,
                    target_present=True,
                    reason=reason,
                )
            )
            changed.extend(
                self._dirty_targets_tx(
                    tx,
                    definition_id=definition.definition_id,
                    definition_version=definition.version,
                    targets=make_absent,
                    target_present=False,
                    reason=reason,
                )
            )
            return tuple(sorted(changed, key=lambda token: token.target.sort_key))

        return self._neo4j.execute_write(work)

    def pending(self, *, limit: int = 100) -> tuple[ProjectionWorkToken, ...]:
        """Return bounded dirty work; queue emptiness is not a freshness certificate."""

        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be an integer >= 1")
        rows = self._neo4j.execute(
            """
            MATCH (w:ProjectionWorkState)
            WHERE w.generation > coalesce(w.applied_generation,0)
            OPTIONAL MATCH (d:ProjectionDefinitionState {definition_id:w.definition_id})
            RETURN w.work_key AS work_key,
                   w.definition_id AS definition_id,
                   w.definition_version AS definition_version,
                   d.current_version AS current_definition_version,
                   w.target_json AS target_json,
                   w.generation AS generation,
                   w.target_present AS target_present,
                   w.reason AS reason
            ORDER BY w.dirty_at ASC, w.work_key ASC
            LIMIT $limit
            """,
            {"limit": limit},
        )
        tokens: list[ProjectionWorkToken] = []
        for row in rows:
            current_version = row.get("current_definition_version")
            if current_version is None:
                raise ProjectionLifecycleCorruptionError(
                    "projection work references a missing definition"
                )
            token = _work_token_from_row(dict(row))
            if token.definition_version != int(current_version):
                raise ProjectionLifecycleCorruptionError(
                    "projection work version disagrees with shared-current definition"
                )
            tokens.append(token)
        return tuple(tokens)

    def commit(
        self,
        token: ProjectionWorkToken,
        *,
        derivation_id: str,
        materialize: MaterializationCallback,
    ) -> ProjectionFreshnessCertificate:
        """Fence, materialize, receipt, and certify one work generation atomically.

        ``materialize`` must use only the supplied transaction adapter and must return a non-blank
        hash of the exact persisted state it installed. For a retirement, adapters should hash their
        canonical retired/absent state rather than returning ``None``. If any fence check fails, the
        callback is not invoked; if certification fails after the callback, the whole transaction
        rolls back, including the materialization.
        """

        if not isinstance(token, ProjectionWorkToken):
            raise TypeError("token must be a ProjectionWorkToken")
        derivation_id = _require_nonblank("derivation_id", derivation_id)
        if not callable(materialize):
            raise TypeError("materialize must be callable")

        def work(tx: Neo4jTransaction) -> ProjectionFreshnessCertificate:
            self._require_current_definition(
                tx,
                definition_id=token.definition_id,
                definition_version=token.definition_version,
            )
            rows = tx.execute(
                """
                MATCH (w:ProjectionWorkState {work_key:$work_key})
                SET w.coordination_epoch=coalesce(w.coordination_epoch,0)+1
                RETURN w.work_key AS work_key,
                       w.definition_id AS definition_id,
                       w.definition_version AS definition_version,
                       w.target_json AS target_json,
                       w.generation AS generation,
                       coalesce(w.applied_generation,0) AS applied_generation,
                       w.target_present AS target_present,
                       w.reason AS reason
                """,
                {"work_key": token.work_key},
            )
            if not rows:
                raise StaleProjectionWorkError("projection work token no longer exists")
            if len(rows) != 1:
                raise ProjectionLifecycleCorruptionError("projection work identity is not unique")
            persisted = _work_token_from_row(dict(rows[0]))
            if (
                persisted.definition_id != token.definition_id
                or persisted.definition_version != token.definition_version
                or persisted.target != token.target
            ):
                raise ProjectionLifecycleCorruptionError(
                    "projection work token identity disagrees with persisted work"
                )
            if (
                persisted.generation != token.generation
                or persisted.target_present != token.target_present
            ):
                raise StaleProjectionWorkError(
                    "projection work token no longer names the current generation/state"
                )
            applied_generation = int(rows[0].get("applied_generation", 0) or 0)
            if applied_generation == token.generation:
                raise ProjectionWorkAlreadyCompletedError(
                    "projection work generation is already completed"
                )
            if applied_generation > token.generation:
                raise StaleProjectionWorkError(
                    "projection work token predates the applied generation"
                )

            projection_hash = materialize(tx, token)
            projection_hash = _require_nonblank("materialized projection_hash", projection_hash)
            receipt_key = _receipt_key(token.work_key, token.generation)

            certified = tx.execute(
                """
                MATCH (w:ProjectionWorkState {work_key:$work_key})
                WHERE w.definition_id=$definition_id
                  AND w.definition_version=$definition_version
                  AND w.generation=$generation
                  AND coalesce(w.applied_generation,0) < $generation
                MERGE (r:ProjectionDerivationReceipt {receipt_key:$receipt_key})
                  ON CREATE SET r.work_key=$work_key,
                                r.definition_id=$definition_id,
                                r.definition_version=$definition_version,
                                r.target_json=$target_json,
                                r.target_present=$target_present,
                                r.generation=$generation,
                                r.derivation_id=$derivation_id,
                                r.projection_hash=$projection_hash,
                                r.created_at=datetime()
                WITH w, r
                WHERE r.work_key=$work_key
                  AND r.definition_id=$definition_id
                  AND r.definition_version=$definition_version
                  AND r.target_json=$target_json
                  AND r.target_present=$target_present
                  AND r.generation=$generation
                  AND r.derivation_id=$derivation_id
                  AND r.projection_hash=$projection_hash
                SET w.applied_generation=$generation,
                    w.applied_definition_version=$definition_version,
                    w.applied_projection_hash=$projection_hash,
                    w.certified_definition_version=$definition_version,
                    w.certified_generation=$generation,
                    w.certified_projection_hash=$projection_hash,
                    w.certified_derivation_id=$derivation_id,
                    w.certified_receipt_key=$receipt_key,
                    w.completed_at=datetime(),
                    w.certified_at=datetime()
                RETURN w.certified_projection_hash AS projection_hash,
                       w.certified_derivation_id AS derivation_id,
                       w.certified_generation AS certified_generation
                """,
                {
                    "work_key": token.work_key,
                    "definition_id": token.definition_id,
                    "definition_version": token.definition_version,
                    "target_json": _target_json(token.target),
                    "target_present": token.target_present,
                    "generation": token.generation,
                    "derivation_id": derivation_id,
                    "projection_hash": projection_hash,
                    "receipt_key": receipt_key,
                },
            )
            if len(certified) != 1:
                raise ProjectionLifecycleCorruptionError(
                    "projection derivation receipt/certificate did not match the fenced write"
                )
            return ProjectionFreshnessCertificate(
                definition_id=token.definition_id,
                definition_version=token.definition_version,
                target=token.target,
                generation=token.generation,
                target_present=token.target_present,
                projection_hash=str(certified[0]["projection_hash"]),
                derivation_id=str(certified[0]["derivation_id"]),
            )

        return self._neo4j.execute_write(work)

    def assess_freshness(
        self,
        *,
        definition_id: str,
        target: ProjectionTarget,
        current_projection_hash: str | None,
    ) -> ProjectionFreshnessAssessment:
        """Assess exact current-state freshness; queue emptiness alone is never sufficient."""

        definition_id = _require_nonblank("definition_id", definition_id)
        if not isinstance(target, ProjectionTarget):
            raise TypeError("target must be a ProjectionTarget")
        if current_projection_hash is not None:
            current_projection_hash = _require_nonblank(
                "current_projection_hash",
                current_projection_hash,
            )
        work_key = _work_key(definition_id, target)
        rows = self._neo4j.execute(
            """
            OPTIONAL MATCH (d:ProjectionDefinitionState {definition_id:$definition_id})
            OPTIONAL MATCH (w:ProjectionWorkState {work_key:$work_key})
            OPTIONAL MATCH (r:ProjectionDerivationReceipt {receipt_key:w.certified_receipt_key})
            RETURN d.current_version AS current_definition_version,
                   w.definition_id AS work_definition_id,
                   w.definition_version AS work_definition_version,
                   w.target_json AS target_json,
                   w.generation AS work_generation,
                   coalesce(w.applied_generation,0) AS applied_generation,
                   w.target_present AS target_present,
                   w.certified_definition_version AS certified_definition_version,
                   w.certified_generation AS certified_generation,
                   w.certified_projection_hash AS certified_projection_hash,
                   w.certified_derivation_id AS certified_derivation_id,
                   w.certified_receipt_key AS certified_receipt_key,
                   r.receipt_key AS receipt_key,
                   r.definition_id AS receipt_definition_id,
                   r.definition_version AS receipt_definition_version,
                   r.target_json AS receipt_target_json,
                   r.target_present AS receipt_target_present,
                   r.generation AS receipt_generation,
                   r.projection_hash AS receipt_projection_hash,
                   r.derivation_id AS receipt_derivation_id
            """,
            {"definition_id": definition_id, "work_key": work_key},
        )
        if len(rows) != 1:
            raise ProjectionLifecycleCorruptionError(
                "freshness read did not produce exactly one snapshot row"
            )
        row = rows[0]
        current_version_raw = row.get("current_definition_version")
        if current_version_raw is None:
            if row.get("work_definition_id") is not None:
                raise ProjectionLifecycleCorruptionError(
                    "projection work exists without its shared-current definition"
                )
            return self._assessment(
                state="unavailable",
                reason="definition_not_published",
                definition_id=definition_id,
                target=target,
                current_projection_hash=current_projection_hash,
            )
        current_version = int(current_version_raw)

        if row.get("work_definition_id") is None:
            return self._assessment(
                state="unavailable",
                reason="target_not_registered",
                definition_id=definition_id,
                target=target,
                current_definition_version=current_version,
                current_projection_hash=current_projection_hash,
            )
        if str(row.get("work_definition_id")) != definition_id:
            raise ProjectionLifecycleCorruptionError(
                "freshness work_key resolved to a different definition"
            )
        persisted_target = _target_from_json(row.get("target_json"))
        if persisted_target != target:
            raise ProjectionLifecycleCorruptionError(
                "freshness work_key resolved to a different target"
            )

        work_version = int(row.get("work_definition_version", 0) or 0)
        work_generation = int(row.get("work_generation", 0) or 0)
        applied_generation = int(row.get("applied_generation", 0) or 0)
        if work_version != current_version:
            raise ProjectionLifecycleCorruptionError(
                "freshness work version disagrees with shared-current definition"
            )
        if work_generation < 1 or applied_generation < 0 or applied_generation > work_generation:
            raise ProjectionLifecycleCorruptionError(
                "freshness work generations are invalid"
            )

        certified_version = (
            None
            if row.get("certified_definition_version") is None
            else int(row["certified_definition_version"])
        )
        certified_generation = (
            None
            if row.get("certified_generation") is None
            else int(row["certified_generation"])
        )
        certified_hash = (
            None
            if row.get("certified_projection_hash") is None
            else str(row["certified_projection_hash"])
        )
        derivation_id = (
            None
            if row.get("certified_derivation_id") is None
            else str(row["certified_derivation_id"])
        )

        base = dict(
            definition_id=definition_id,
            target=target,
            current_definition_version=current_version,
            target_present=row.get("target_present"),
            work_generation=work_generation,
            applied_generation=applied_generation,
            certified_definition_version=certified_version,
            certified_generation=certified_generation,
            certified_projection_hash=certified_hash,
            current_projection_hash=current_projection_hash,
            derivation_id=derivation_id,
        )
        if current_projection_hash is None:
            return ProjectionFreshnessAssessment(
                state="unavailable",
                reason="projection_state_unavailable",
                **base,
            )

        reasons: list[str] = []
        if applied_generation != work_generation:
            reasons.append("pending_generation")
        if certified_version != current_version:
            reasons.append("definition_version_not_certified")
        if certified_generation != work_generation:
            reasons.append("generation_not_certified")
        if certified_hash != current_projection_hash:
            reasons.append("projection_hash_not_certified")
        if not derivation_id:
            reasons.append("derivation_not_certified")

        receipt_key = row.get("certified_receipt_key")
        if not receipt_key or row.get("receipt_key") != receipt_key:
            reasons.append("derivation_receipt_missing")
        else:
            if str(row.get("receipt_definition_id") or "") != definition_id:
                reasons.append("derivation_receipt_definition_mismatch")
            if int(row.get("receipt_definition_version", 0) or 0) != current_version:
                reasons.append("derivation_receipt_version_mismatch")
            if row.get("receipt_target_json") != _target_json(target):
                reasons.append("derivation_receipt_target_mismatch")
            if row.get("receipt_target_present") != row.get("target_present"):
                reasons.append("derivation_receipt_presence_mismatch")
            if int(row.get("receipt_generation", 0) or 0) != work_generation:
                reasons.append("derivation_receipt_generation_mismatch")
            if str(row.get("receipt_projection_hash") or "") != current_projection_hash:
                reasons.append("derivation_receipt_hash_mismatch")
            if str(row.get("receipt_derivation_id") or "") != (derivation_id or ""):
                reasons.append("derivation_receipt_id_mismatch")

        if reasons:
            return ProjectionFreshnessAssessment(
                state="stale",
                reason=",".join(reasons),
                **base,
            )
        return ProjectionFreshnessAssessment(
            state="fresh",
            reason="certified_current",
            **base,
        )

    @staticmethod
    def _assessment(
        *,
        state: str,
        reason: str,
        definition_id: str,
        target: ProjectionTarget,
        current_definition_version: int | None = None,
        current_projection_hash: str | None = None,
    ) -> ProjectionFreshnessAssessment:
        return ProjectionFreshnessAssessment(
            state=state,  # type: ignore[arg-type]
            reason=reason,
            definition_id=definition_id,
            target=target,
            current_definition_version=current_definition_version,
            target_present=None,
            work_generation=None,
            applied_generation=None,
            certified_definition_version=None,
            certified_generation=None,
            certified_projection_hash=None,
            current_projection_hash=current_projection_hash,
            derivation_id=None,
        )
