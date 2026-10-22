"""Typed results for memory ingestion."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum


class IngestStatus(str, Enum):
    """Stable status values for the ingestion pipeline."""

    QUEUED = "queued"
    INGESTED = "ingested"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class IngestResult:
    """Lean v1 ingestion result returned by the ingest service."""

    episode_id: str
    status: IngestStatus
    nodes_touched: int
    edges_touched: int
    idempotent_replay: bool = False
    processing_state: str | None = None


class IdempotencyKeyConflict(ValueError):
    """An idempotency key was reused with different episode content."""


_INGEST_KEY_PREFIX = b"menhir-episode-ingest-key-v1\x00"


def episode_ingest_key(idempotency_key: str, namespace: str) -> str:
    """Namespace-scoped digest of a client idempotency key (pass normalize_namespace(ns))."""

    material = (
        _INGEST_KEY_PREFIX
        + namespace.encode("utf-8")
        + b"\x00"
        + idempotency_key.encode("utf-8")
    )
    return hashlib.sha256(material).hexdigest()


def episode_request_digest(
    *,
    content: str,
    diff: str | None,
    source: str,
    flagged: bool,
    bootstrap_scope: str | None,
    occurred_at: object,
    role: str | None,
    turn_evidence_uuid: str | None,
    user_statement: str | None,
) -> str:
    """sha256 hex of the caller-supplied submit fields (canonical JSON).

    Session and user are left out: the route mints a fresh session when the caller names none,
    so including them would turn every honest retry into a conflict.
    """

    payload = {
        "content": content,
        "diff": diff,
        "source": source,
        "flagged": flagged,
        "bootstrap_scope": bootstrap_scope,
        "occurred_at": str(occurred_at) if occurred_at is not None else None,
        "role": role,
        "turn_evidence_uuid": turn_evidence_uuid,
        "user_statement": user_statement,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_constraint_violation(exc: BaseException) -> bool:
    """Neo4j uniqueness-constraint failure (same test as project_identity_binding)."""

    return "ConstraintValidationFailed" in str(getattr(exc, "code", "") or "") or (
        "already exists with label" in str(exc)
    )
