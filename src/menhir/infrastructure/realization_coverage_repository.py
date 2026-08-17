"""Read-only infrastructure adapters for Realization Coverage.

This module exposes the complete durable T5 target snapshot and the scalar projection's canonical
installed-state hash.  It never mutates projection lifecycle state or Views.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from menhir.domain.projection import ProjectionDefinition, ProjectionTarget
from menhir.domain.projection_lifecycle import (
    ProjectionFreshnessAssessment,
    ProjectionLifecycleCorruptionError,
    ProjectionWorkToken,
)
from menhir.infrastructure.projection_lifecycle_repository import ProjectionLifecycleRepository

__all__ = ["RealizationLifecycleRepository", "ScalarStateProjectionHashSource"]


def _target_from_json(raw: object) -> ProjectionTarget:
    try:
        payload = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ProjectionLifecycleCorruptionError(
            "persisted realization target is not valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ProjectionLifecycleCorruptionError("persisted realization target is not an object")
    namespace = payload.get("namespace")
    subject_id = payload.get("subject_id")
    key = payload.get("key", [])
    if namespace is not None and not isinstance(namespace, str):
        raise ProjectionLifecycleCorruptionError("persisted realization namespace is invalid")
    if not isinstance(subject_id, str) or not subject_id.strip():
        raise ProjectionLifecycleCorruptionError("persisted realization subject_id is invalid")
    if not isinstance(key, list) or any(not isinstance(part, str) for part in key):
        raise ProjectionLifecycleCorruptionError("persisted realization target key is invalid")
    return ProjectionTarget(namespace=namespace, subject_id=subject_id, key=tuple(key))


class RealizationLifecycleRepository:
    """Read-only T5 lifecycle source for Realization Coverage."""

    def __init__(self, neo4j: Any) -> None:
        self._neo4j = neo4j
        self._lifecycle = ProjectionLifecycleRepository(neo4j)

    def targets_for_definition(self, definition_id: str) -> tuple[ProjectionWorkToken, ...]:
        definition_id = str(definition_id or "").strip()
        if not definition_id:
            raise ValueError("definition_id must be a non-blank string")
        rows = self._neo4j.execute(
            """
            MATCH (w:ProjectionWorkState {definition_id:$definition_id})
            OPTIONAL MATCH (d:ProjectionDefinitionState {definition_id:$definition_id})
            RETURN w.work_key AS work_key,
                   w.definition_id AS definition_id,
                   w.definition_version AS definition_version,
                   d.current_version AS current_definition_version,
                   w.target_json AS target_json,
                   w.generation AS generation,
                   w.target_present AS target_present,
                   w.reason AS reason
            ORDER BY w.work_key
            """,
            {"definition_id": definition_id},
        )
        tokens: list[ProjectionWorkToken] = []
        for raw in rows:
            row = dict(raw)
            if row.get("current_definition_version") is None:
                raise ProjectionLifecycleCorruptionError(
                    "projection target exists without its shared-current definition"
                )
            target = _target_from_json(row.get("target_json"))
            work_key = str(row.get("work_key") or "")
            if work_key != self._lifecycle.work_key(definition_id, target):
                raise ProjectionLifecycleCorruptionError(
                    "projection target work_key does not match definition/target identity"
                )
            try:
                token = ProjectionWorkToken(
                    work_key=work_key,
                    definition_id=str(row.get("definition_id") or ""),
                    definition_version=int(row["definition_version"]),
                    target=target,
                    generation=int(row["generation"]),
                    target_present=row.get("target_present"),
                    reason=str(row.get("reason") or ""),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ProjectionLifecycleCorruptionError(
                    "persisted projection target snapshot is invalid"
                ) from exc
            if token.definition_id != definition_id:
                raise ProjectionLifecycleCorruptionError(
                    "projection target snapshot resolved to a different definition"
                )
            if token.definition_version != int(row["current_definition_version"]):
                raise ProjectionLifecycleCorruptionError(
                    "projection target version disagrees with shared-current definition"
                )
            tokens.append(token)
        return tuple(sorted(tokens, key=lambda token: token.target.sort_key))

    def assess_freshness(
        self,
        *,
        definition_id: str,
        target: ProjectionTarget,
        current_projection_hash: str | None,
    ) -> ProjectionFreshnessAssessment:
        return self._lifecycle.assess_freshness(
            definition_id=definition_id,
            target=target,
            current_projection_hash=current_projection_hash,
        )


def _stable_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _target_payload(target: ProjectionTarget) -> dict[str, object]:
    return {
        "namespace": target.namespace,
        "subject_id": target.subject_id,
        "key": list(target.key),
    }


class ScalarStateProjectionHashSource:
    """Canonical installed-state hash for ``typed_scalar.current_state``.

    The hash covers the parity-bearing persisted scalar-state surface, not incidental node identity.
    Absence has its own canonical hash.  ``target_present`` is lifecycle membership and therefore does
    not decide physical View presence; the graph read always hashes what is actually installed.
    """

    DEFINITION_ID = "typed_scalar.current_state"

    def __init__(self, neo4j: Any) -> None:
        self._neo4j = neo4j

    def current_projection_hash(
        self,
        *,
        definition: ProjectionDefinition,
        target: ProjectionTarget,
        target_present: bool,
    ) -> str | None:
        if definition.definition_id != self.DEFINITION_ID:
            raise ValueError(
                "ScalarStateProjectionHashSource only supports typed_scalar.current_state"
            )
        if len(target.key) != 4:
            raise ValueError("scalar-state projection target key must contain four slot parts")
        attribute, scope, value_kind, unit = target.key
        rows = self._neo4j.execute(
            """
            MATCH (n:Entity {view_kind:'scalar_state', view_subject_uuid:$subject_uuid,
                             ss_attribute:$attribute, ss_kind:$value_kind})
            WHERE coalesce(n.view_current, true)
              AND coalesce(n.ss_scope, '') = $scope
              AND coalesce(n.ss_unit, '') = $unit
              AND ((n.group_id IS NULL AND $namespace IS NULL) OR n.group_id = $namespace)
            RETURN n.ss_value AS value,
                   toString(n.valid_at) AS valid_at,
                   coalesce(n.scalar_contributors, []) AS scalar_contributors,
                   coalesce(n.scalar_effective_tier, '') AS scalar_effective_tier,
                   coalesce(n.episode_uuids, []) AS episode_uuids
            """,
            {
                "subject_uuid": target.subject_id,
                "attribute": attribute,
                "scope": scope,
                "value_kind": value_kind,
                "unit": unit,
                "namespace": target.namespace,
            },
        )
        if len(rows) > 1:
            raise ProjectionLifecycleCorruptionError(
                "multiple current ScalarStateViews exist for one realization target"
            )
        base = {
            "definition_id": definition.definition_id,
            "target": _target_payload(target),
        }
        if not rows:
            return _stable_hash({**base, "state": "absent"})

        row = dict(rows[0])
        return _stable_hash(
            {
                **base,
                "state": "present",
                "value": row.get("value"),
                "valid_at": str(row.get("valid_at") or ""),
                "scalar_contributors": sorted(
                    str(value) for value in (row.get("scalar_contributors") or [])
                ),
                "scalar_effective_tier": str(row.get("scalar_effective_tier") or ""),
                "episode_uuids": sorted(str(value) for value in (row.get("episode_uuids") or [])),
            }
        )
