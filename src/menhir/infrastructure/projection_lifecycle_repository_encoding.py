"""Encoding, key-derivation, and row-parsing helpers for the projection lifecycle repository.

Moved verbatim from :mod:`menhir.infrastructure.projection_lifecycle_repository`, which
still re-exports the public ``MaterializationCallback`` alias.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from typing import Any

from menhir.domain.projection import ProjectionTarget
from menhir.domain.projection_lifecycle import (
    ProjectionLifecycleCorruptionError,
    ProjectionWorkToken,
)
from menhir.infrastructure.neo4j import Neo4jTransaction


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
