"""Pure canonical hashing for the typed-scalar current-state projection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from menhir.domain.projection import ProjectionDefinition, ProjectionTarget

__all__ = [
    "scalar_projection_absent_hash",
    "scalar_projection_present_hash",
]


def _stable_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _base(definition: ProjectionDefinition, target: ProjectionTarget) -> dict[str, object]:
    return {
        "definition_id": definition.definition_id,
        "target": {
            "namespace": target.namespace,
            "subject_id": target.subject_id,
            "key": list(target.key),
        },
    }


def scalar_projection_absent_hash(
    definition: ProjectionDefinition,
    target: ProjectionTarget,
) -> str:
    """Hash canonical physical absence for one scalar projection target."""
    return _stable_hash({**_base(definition, target), "state": "absent"})


def scalar_projection_present_hash(
    definition: ProjectionDefinition,
    target: ProjectionTarget,
    *,
    value: Any,
    valid_at: object,
    contributor_ids: Iterable[object],
    effective_tier: object,
    episode_uuids: Iterable[object],
) -> str:
    """Hash the parity-bearing scalar-state surface shared by writer and read-side audit."""
    return _stable_hash(
        {
            **_base(definition, target),
            "state": "present",
            "value": value,
            "valid_at": str(valid_at or ""),
            "scalar_contributors": sorted(str(item) for item in contributor_ids),
            "scalar_effective_tier": str(effective_tier or ""),
            "episode_uuids": sorted(str(item) for item in episode_uuids),
        }
    )
