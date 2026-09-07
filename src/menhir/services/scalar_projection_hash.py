"""Pure canonical hashing for the typed-scalar current-state projection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import timezone
from typing import Any

from menhir.domain.projection import ProjectionDefinition, ProjectionTarget
from menhir.domain.temporal import parse_iso8601
from menhir.domain.typed_assertion import normalize_scalar

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


def _canonical_time(value: object) -> str:
    parsed = parse_iso8601(value)
    if parsed is not None:
        return parsed.astimezone(timezone.utc).isoformat()
    return str(value or "").strip()


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
            # ScalarStateKind persists the canonical scalar string, not the fold's raw Python type.
            # Hash that shared surface so numeric/bool/range values certify after graph read-back.
            "value": normalize_scalar(value),
            # Neo4j may render an equivalent instant with ``Z`` or a named zone. Identity is the
            # instant, not the driver's timestamp spelling.
            "valid_at": _canonical_time(valid_at),
            "scalar_contributors": sorted(str(item) for item in contributor_ids),
            "scalar_effective_tier": str(effective_tier or ""),
            "episode_uuids": sorted(str(item) for item in episode_uuids),
        }
    )
