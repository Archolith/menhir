"""Pending-work reads, fenced commits, and freshness assessment for the projection lifecycle repository.

Mixin of :class:`menhir.infrastructure.projection_lifecycle_repository.ProjectionLifecycleRepository`;
moved verbatim from that module, which still defines and re-exports the composed class.
"""

from __future__ import annotations

from menhir.domain.projection import ProjectionTarget
from menhir.domain.projection_lifecycle import (
    ProjectionFreshnessAssessment,
    ProjectionFreshnessCertificate,
    ProjectionLifecycleCorruptionError,
    ProjectionWorkAlreadyCompletedError,
    ProjectionWorkToken,
    StaleProjectionWorkError,
)
from menhir.infrastructure.neo4j import Neo4jTransaction
from menhir.infrastructure.projection_lifecycle_repository_encoding import (
    MaterializationCallback,
    _receipt_key,
    _require_nonblank,
    _target_from_json,
    _target_json,
    _work_key,
    _work_token_from_row,
)


class _ProjectionLifecycleReadsMixin:
    """Pending queue reads, atomic fence/materialize/receipt/certify commits, freshness."""

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
