"""Definition publication and generation-advancing writes for the projection lifecycle repository.

Mixin of :class:`menhir.infrastructure.projection_lifecycle_repository.ProjectionLifecycleRepository`;
moved verbatim from that module, which still defines and re-exports the composed class.
"""

from __future__ import annotations

from collections.abc import Sequence

from menhir.domain.projection import ProjectionDefinition, ProjectionTarget
from menhir.domain.projection_lifecycle import (
    ProjectionDefinitionNotPublishedError,
    ProjectionDefinitionPublication,
    ProjectionLifecycleCorruptionError,
    ProjectionWorkToken,
    StaleProjectionDefinitionError,
)
from menhir.infrastructure.neo4j import Neo4jTransaction
from menhir.infrastructure.projection_lifecycle_repository_encoding import (
    _require_nonblank,
    _target_json,
    _unique_targets,
    _work_key,
    _work_token_from_row,
)
from menhir.infrastructure.projection_lifecycle_repository_schema import (
    PROJECTION_LIFECYCLE_SCHEMA_QUERIES,
)


class _ProjectionLifecycleWritesMixin:
    """Publishing, dirtying, retiring, and reconciling projection work generations."""

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
