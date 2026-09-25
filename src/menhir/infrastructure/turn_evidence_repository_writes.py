"""Write side of selective `:TurnEvidence` capture.

Split out of ``turn_evidence_repository.py`` (file-size refactor). ``TurnEvidenceRepository``
composes ``TurnEvidenceWritesMixin``; methods run against the facade's ``self._neo4j``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from menhir.domain.namespace import normalize_namespace
from menhir.infrastructure.turn_evidence_repository_keys import (
    EVIDENCE_ROLES,
    _namespace_scoped_key,
    derive_prompt_hash,
    derive_turn_key,
)


def _normalize_occurred_at(value: str | None) -> str | None:
    """Validate an optional ISO-8601 source time and return a timezone-aware string."""
    if value is None or not str(value).strip():
        return None
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"occurred_at must be ISO-8601, got {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


class TurnEvidenceWritesMixin:
    """The ``record_turn_evidence`` capture half of ``TurnEvidenceRepository``."""

    def record_turn_evidence(
        self,
        *,
        text: str,
        role: str = "user",
        declarant: str | None = None,
        session_id: str | None = None,
        occurred_at: str | None = None,
        namespace: str | None = None,
        source_kind: str = "unknown",
        source_id: str | None = None,
        source_client: str | None = None,
        hook_version: str | None = None,
        cwd: str | None = None,
        transcript_path: str | None = None,
        triage_reason: list[str] | None = None,
        triage_version: str | None = None,
        metadata: dict[str, Any] | None = None,
        turn_key: str | None = None,
        prompt_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist one candidate evidence record (idempotent on turn_key). Returns
        {turn_id, created, recorded_at, occurred_at}. `role` and non-empty `text` are required.

        `source_client`/`hook_version` are additive PROVENANCE labels (which producer captured this,
        at what version) and are optional — older callers that omit them store nulls. `prompt_hash`
        is derived server-side from `text` so it is always present and client-independent. Free-form
        `metadata` (project_root/git_branch/git_commit/...) is stored verbatim as JSON.

        `recorded_at` is always the server receive time and remains the monotonic processing cursor.
        `occurred_at` is optional world time supplied by replay/import producers; live host hooks omit
        it and therefore retain receive-time semantics."""
        text = (text or "").strip()
        if not text:
            raise ValueError("record_turn_evidence requires non-empty text")
        if role not in EVIDENCE_ROLES:
            raise ValueError(f"role must be one of {EVIDENCE_ROLES}, got {role!r}")
        declarant = declarant or role
        normalized_occurred_at = _normalize_occurred_at(occurred_at)
        namespace_key = normalize_namespace(namespace)
        key = (
            _namespace_scoped_key(turn_key, namespace)
            if turn_key
            else derive_turn_key(
                source_kind=source_kind, session_id=session_id, text=text, cwd=cwd,
                prompt_id=prompt_id, namespace=namespace,
            )
        )
        rows = self._neo4j.execute(
            """
            MERGE (f:EvidenceNamespaceFence {namespace_key: $namespace_key})
            ON CREATE SET f.generation = 0, f.created_at = datetime()
            SET f.lock_nonce = $operation_id, f.locked_at = datetime()
            WITH f
            MERGE (t:TurnEvidence {turn_key: $turn_key})
            ON CREATE SET
                t.turn_id = randomUuid(),
                t.recorded_at = datetime(),
                t.occurred_at = CASE
                    WHEN $occurred_at IS NULL THEN null
                    ELSE datetime($occurred_at)
                END,
                t.role = $role,
                t.declarant = $declarant,
                t.text = $text,
                t.session_id = $session_id,
                t.namespace = $namespace,
                t.source_kind = $source_kind,
                t.source_id = $source_id,
                t.source_client = $source_client,
                t.hook_version = $hook_version,
                t.cwd = $cwd,
                t.transcript_path = $transcript_path,
                t.triage_reason = $triage_reason,
                t.triage_version = $triage_version,
                t.prompt_length = $prompt_length,
                t.prompt_hash = $prompt_hash,
                t.prompt_id = $prompt_id,
                t.metadata = $metadata_json,
                t.evidence_finalized = true,
                t.evidence_generation = f.generation,
                t._created = true
            ON MATCH SET
                t._created = false,
                t.occurred_at = coalesce(
                    t.occurred_at,
                    CASE WHEN $occurred_at IS NULL THEN null ELSE datetime($occurred_at) END
                ),
                t.evidence_finalized = true,
                t.evidence_generation = f.generation
            RETURN t.turn_id AS turn_id, coalesce(t._created, false) AS created,
                   toString(t.recorded_at) AS recorded_at,
                   toString(t.occurred_at) AS occurred_at
            """,
            params={
                "turn_key": key,
                "operation_id": f"turn-evidence:{key}",
                "namespace_key": namespace_key,
                "role": role,
                "declarant": declarant,
                "text": text[:8000],
                "session_id": session_id,
                "occurred_at": normalized_occurred_at,
                "namespace": namespace,
                "source_kind": source_kind,
                "source_id": source_id,
                "source_client": source_client,
                "hook_version": hook_version,
                "cwd": cwd,
                "transcript_path": transcript_path,
                "triage_reason": list(triage_reason or []),
                "triage_version": triage_version,
                "prompt_length": len(text),
                "prompt_hash": derive_prompt_hash(text),
                "prompt_id": (prompt_id or None),
                "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),
            },
        )
        row = rows[0] if rows else {}
        return {
            "turn_id": str(row.get("turn_id") or ""),
            "created": bool(row.get("created")),
            "recorded_at": str(row.get("recorded_at") or ""),
            "occurred_at": str(row.get("occurred_at")) if row.get("occurred_at") else None,
        }
