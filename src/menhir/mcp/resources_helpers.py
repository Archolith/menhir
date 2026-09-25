"""Build identity, normalization, and input-validation helpers for the menhir MCP resources."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from uuid import UUID

from menhir.domain.models import NodeScope, ProcessingState
from menhir.domain.utils import excerpt

_TYPE_PATTERN = re.compile(r"^[A-Za-z0-9_\- ]{1,64}$")
_BUILD_ID_CACHE: str | None = None


def _resolve_build_id() -> str:
    global _BUILD_ID_CACHE
    if _BUILD_ID_CACHE is not None:
        return _BUILD_ID_CACHE
    explicit = os.getenv("MEMORY_BUILD_ID", "").strip()
    if explicit:
        _BUILD_ID_CACHE = explicit
        return _BUILD_ID_CACHE
    _here = Path(__file__).resolve()
    repo_dir = str(next((p for p in _here.parents if (p / "pyproject.toml").exists()), _here.parents[3]))
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=2.0,
            check=True,
        )
        build_id = result.stdout.strip()
        if build_id:
            _BUILD_ID_CACHE = build_id
            return _BUILD_ID_CACHE
    except (OSError, subprocess.SubprocessError):
        pass
    _BUILD_ID_CACHE = f"dev-{int(os.path.getmtime(__file__))}"
    return _BUILD_ID_CACHE


def _runtime_fingerprint(provider_config: dict[str, Any], session: Any) -> tuple[str, str]:
    payload = "|".join(
        [
            str(provider_config.get("neo4j_uri") or ""),
            str(provider_config.get("neo4j_database") or "neo4j"),
            str(provider_config.get("local_llm_base_url") or ""),
            str(provider_config.get("local_llm_embed_base_url") or ""),
            str(provider_config.get("chat_model") or ""),
            str(provider_config.get("embed_model") or ""),
            str(provider_config.get("chat_provider") or "local"),
            str(provider_config.get("graphiti_provider") or "local"),
            str(provider_config.get("graphiti_embed_provider") or ""),
            session.session_id,
        ]
    )
    config_fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    build_id = _resolve_build_id()
    return build_id, f"memory:{build_id}:{config_fingerprint}:{session.session_id[:8]}"


def _normalize_memory_row(row: dict[str, Any] | None, *, detail: bool = False) -> dict[str, Any] | None:
    if row is None:
        return None
    compact = {
        "uuid": row.get("uuid"),
        "name": row.get("name"),
        "type": row.get("type"),
        "scope": row.get("scope"),
        "summary": excerpt(row.get("summary") or row.get("content")),
        "freshness": row.get("freshness"),
        "user_flagged": bool(row.get("user_flagged", False)),
        "created_at": row.get("created_at"),
        "last_accessed": row.get("last_accessed"),
    }
    if not detail:
        return compact
    return {
        **compact,
        "labels": row.get("labels") or [],
        "content": row.get("content"),
        "source": row.get("source"),
        "source_confidence": row.get("source_confidence"),
        "session_id": row.get("session_id"),
        "user_id": row.get("user_id"),
        "processing_state": row.get("processing_state"),
        "processing_stage": row.get("processing_stage"),
        "processing_substage": row.get("processing_substage"),
        "processing_substage_started_at": row.get("processing_substage_started_at"),
        "processing_progress": row.get("processing_progress"),
        "processing_steps_total": row.get("processing_steps_total"),
        "processing_steps_completed": row.get("processing_steps_completed"),
        "processing_llm_tasks_attempt": row.get("processing_llm_tasks_attempt"),
        "processing_llm_tasks_total": row.get("processing_llm_tasks_total"),
        "processing_llm_last_task_at": row.get("processing_llm_last_task_at"),
        "processing_llm_active_task": row.get("processing_llm_active_task"),
        "processing_llm_active_kind": row.get("processing_llm_active_kind"),
        "processing_llm_active_model": row.get("processing_llm_active_model"),
        "processing_llm_active_endpoint": row.get("processing_llm_active_endpoint"),
        "processing_attempts": row.get("processing_attempts"),
        "queued_at": row.get("queued_at"),
        "processing_owner": row.get("processing_owner"),
        "processing_lease_expires_at": row.get("processing_lease_expires_at"),
        "processing_heartbeat_at": row.get("processing_heartbeat_at"),
        "processing_started_at": row.get("processing_started_at"),
        "processing_completed_at": row.get("processing_completed_at"),
        "processing_error": row.get("processing_error"),
        "resolved_episode_uuid": row.get("resolved_episode_uuid"),
        "enriched_nodes_touched": row.get("enriched_nodes_touched"),
        "enriched_edges_touched": row.get("enriched_edges_touched"),
    }


def _normalize_scored_result(scored: Any) -> dict[str, Any]:
    if isinstance(scored, dict):
        breakdown = scored.get("breakdown") or {}
        return {
            "uuid": scored.get("uuid"),
            "name": scored.get("name"),
            "type": scored.get("type") or scored.get("memory_type"),
            "scope": scored.get("scope"),
            "summary": excerpt(scored.get("summary") or scored.get("content")),
            "final_score": scored.get("final_score"),
            "breakdown": {
                "semantic_similarity": breakdown.get("semantic_similarity"),
                "adjacency_bonus": breakdown.get("adjacency_bonus"),
                "recency_bonus": breakdown.get("recency_bonus"),
                "prominence_bonus": breakdown.get("prominence_bonus"),
            },
        }
    breakdown = getattr(scored, "breakdown", None)
    return {
        "uuid": getattr(scored, "uuid", None),
        "name": getattr(scored, "name", None),
        "type": getattr(scored, "memory_type", None),
        "scope": getattr(scored, "scope", None),
        "summary": excerpt(getattr(scored, "summary", None) or getattr(scored, "content", None)),
        "final_score": getattr(scored, "final_score", None),
        "breakdown": {
            "semantic_similarity": getattr(breakdown, "semantic_similarity", None),
            "adjacency_bonus": getattr(breakdown, "adjacency_bonus", None),
            "recency_bonus": getattr(breakdown, "recency_bonus", None),
            "prominence_bonus": getattr(breakdown, "prominence_bonus", None),
        },
    }


def _normalize_processing_row(
    row: dict[str, Any],
    *,
    stale_episode_ids: set[str],
    max_attempts: int,
) -> dict[str, Any]:
    state = str(row.get("processing_state") or "")
    attempts = int(row.get("processing_attempts") or 0)
    stale_lease = str(row.get("uuid") or "") in stale_episode_ids
    exhausted_pending = state.upper() == ProcessingState.PENDING and attempts >= max_attempts
    if stale_lease:
        status_hint = "stale_lease"
    elif exhausted_pending:
        status_hint = "exhausted_pending"
    elif state.upper() == ProcessingState.ENRICHING:
        status_hint = "active"
    else:
        status_hint = "queued"
    return {
        "uuid": row.get("uuid"),
        "state": row.get("processing_state"),
        "stage": row.get("processing_stage"),
        "substage": row.get("processing_substage"),
        "progress": row.get("processing_progress"),
        "attempts": attempts,
        "owner": row.get("processing_owner"),
        "lease_expires_at": row.get("processing_lease_expires_at"),
        "heartbeat_at": row.get("processing_heartbeat_at"),
        "started_at": row.get("processing_started_at"),
        "llm_last_task_at": row.get("processing_llm_last_task_at"),
        "llm_active_task": row.get("processing_llm_active_task"),
        "error": row.get("processing_error"),
        "stale_lease": stale_lease,
        "exhausted_pending": exhausted_pending,
        "status_hint": status_hint,
    }


def _normalize_scheduler_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    lease = snapshot.get("lease") or {}
    return {
        "running": snapshot.get("running"),
        "lease_acquired": lease.get("acquired"),
        "lease_blocked_reason": lease.get("blocked_reason"),
    }


def _require_uuid(node_uuid: str) -> str:
    candidate = (node_uuid or "").strip()
    try:
        return str(UUID(candidate))
    except ValueError as exc:
        raise ValueError(f"Invalid memory UUID: {node_uuid}") from exc


def _require_scope(scope: str) -> str:
    try:
        return NodeScope((scope or "").strip().upper()).value
    except ValueError:
        raise ValueError(f"Invalid scope: {scope!r}. Use SESSION, PERSISTENT, or PROMOTED.")


def _require_term(term: str) -> str:
    normalized = (term or "").strip()
    if not normalized:
        raise ValueError("Search term must not be empty.")
    if len(normalized) > 200:
        raise ValueError("Search term must be 200 characters or fewer.")
    return normalized


def _require_type(memory_type: str) -> str:
    normalized = (memory_type or "").strip()
    if not normalized:
        raise ValueError("Memory type must not be empty.")
    if not _TYPE_PATTERN.fullmatch(normalized):
        raise ValueError("Memory type must be 1-64 characters of letters, digits, spaces, underscores, or hyphens.")
    return normalized
