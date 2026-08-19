from __future__ import annotations

import json
import logging
import os
import traceback
from typing import Any

from .helpers import (
    _json_default,
    _redact_telemetry_value,
    _safe_preview_of,
    _size_of,
    _utc_now_iso,
)
from .store import McpTelemetryStore, telemetry_store

logger = logging.getLogger(__name__)


_NON_EPISODE_SUBJECT_KEY = "__non_episode__"


def record_llm_usage_event(
    *,
    event: Any,
    episode_uuid: str | None = None,
    store: McpTelemetryStore = telemetry_store,
) -> bool:
    """Persist one terminal provider invocation without affecting the caller.

    Provider ``usage`` objects are not trusted to contain counters only: compatible providers may
    attach arbitrary strings. Persist a recursively minimized copy, and give process-wide/non-episode
    calls an explicit sentinel key instead of NULL so current operational rows can never masquerade as
    pre-lineage unaddressable content during a CF-165 completeness census.
    """

    if event.phase not in {"completed", "failed"} or not event.call_id:
        return False
    try:
        provider_usage_json = None
        if event.provider_usage is not None:
            provider_usage_json = json.dumps(
                _redact_telemetry_value(event.provider_usage),
                default=_json_default,
                sort_keys=True,
            )
        return store.record_llm_usage_event(
            call_id=event.call_id,
            recorded_at=_utc_now_iso(),
            run_id=os.getenv("MENHIR_BENCH_ACTIVE_RUN_ID") or None,
            episode_uuid=episode_uuid or _NON_EPISODE_SUBJECT_KEY,
            operation=event.operation,
            kind=event.kind,
            model=event.model,
            endpoint=event.endpoint,
            status=event.phase,
            duration_ms=event.duration_ms,
            input_tokens=event.input_tokens,
            output_tokens=event.output_tokens,
            total_tokens=event.total_tokens,
            cached_input_tokens=event.cached_input_tokens,
            reasoning_output_tokens=event.reasoning_output_tokens,
            provider_usage_json=provider_usage_json,
            error="[redacted]" if event.error else None,
        )
    except Exception as exc:  # pragma: no cover - telemetry must never break the caller
        _log_telemetry_persist_failure(f"record_llm_usage_event:{event.call_id}", exc)
        return False


_TELEMETRY_FAILURE_COUNTS: dict[str, int] = {}


def _log_telemetry_persist_failure(operation: str, exc: Exception) -> None:
    """Log telemetry persistence failures without flooding service logs."""

    count = _TELEMETRY_FAILURE_COUNTS.get(operation, 0) + 1
    _TELEMETRY_FAILURE_COUNTS[operation] = count
    should_warn = count == 1 or count in {5, 10} or count % 25 == 0
    message = "%s failed (%s): %s"
    if should_warn:
        logger.warning(message, operation, type(exc).__name__, exc)
    else:
        logger.debug(message, operation, type(exc).__name__, exc)


def _mcp_event_lineage(
    payload: Any, *, namespace: str | None, node_uuid: str | None
) -> tuple[str, str | None]:
    """Return non-NULL namespace lineage plus an optional structural node id."""
    from menhir.domain.namespace import DEFAULT_NAMESPACE

    resolved_namespace = str(namespace or "").strip()
    resolved_uuid = str(node_uuid or "").strip() or None
    if isinstance(payload, dict):
        resolved_namespace = resolved_namespace or str(payload.get("namespace") or "").strip()
        if resolved_uuid is None:
            for key in ("node_uuid", "memory_uuid", "uuid"):
                candidate = str(payload.get(key) or "").strip()
                if candidate:
                    resolved_uuid = candidate
                    break
    return resolved_namespace or DEFAULT_NAMESPACE, resolved_uuid


def record_mcp_event(
    *,
    kind: str,
    operation: str,
    payload: Any = None,
    result: Any = None,
    duration_ms: int = 0,
    success: bool = True,
    error: str | None = None,
    namespace: str | None = None,
    node_uuid: str | None = None,
    store: McpTelemetryStore = telemetry_store,
) -> None:
    """Persist a non-wrapper telemetry event such as background work or recall waits.

    Free-text payload values are redacted before persistence. New rows also receive explicit
    namespace lineage so they cannot be confused with historical pre-lineage residue during
    explicit-erasure completeness checks.
    """

    timestamp = _utc_now_iso()
    input_size = _size_of(payload) if payload is not None else None
    result_size = _size_of(result) if result is not None else None
    preview_source = payload if payload is not None else result
    payload_preview = _safe_preview_of(preview_source) if preview_source is not None else None
    effective_namespace, effective_uuid = _mcp_event_lineage(
        payload, namespace=namespace, node_uuid=node_uuid
    )
    try:
        store.record(
            kind=kind,
            operation=operation,
            started_at=timestamp,
            completed_at=timestamp,
            duration_ms=max(0, duration_ms),
            success=success,
            error="[redacted]" if error else None,
            input_size=input_size,
            result_size=result_size,
            payload_preview=payload_preview,
            namespace=effective_namespace,
            node_uuid=effective_uuid,
        )
    except Exception as exc:  # pragma: no cover - telemetry must never break the caller
        _log_telemetry_persist_failure(f"record_mcp_event:{kind}:{operation}", exc)


def record_failure_event(
    *,
    operation: str,
    error: str,
    episode_uuid: str | None = None,
    failure_stage: str | None = None,
    classification: str | None = None,
    retryable: bool | None = None,
    processing_attempt: int | None = None,
    queue_depth: int | None = None,
    worker_id: str | None = None,
    error_type: str | None = None,
    traceback_text: str | None = None,
    details: Any = None,
    store: McpTelemetryStore = telemetry_store,
) -> bool:
    """Persist a structured failure record for enrichment and scheduler diagnostics.

    Episode failures retain their addressable diagnostics. A non-episode failure has no memory
    subject to erase, so it gets an explicit operational sentinel and a minimized payload rather
    than a NULL key plus arbitrary exception prose.
    """

    scoped = bool(str(episode_uuid or "").strip())
    details_payload = details
    if traceback_text:
        base_details = (
            dict(details)
            if isinstance(details, dict)
            else {"details": details}
            if details is not None
            else {}
        )
        base_details["traceback"] = traceback_text
        base_details["traceback_preview"] = traceback_text[:500]
        details_payload = base_details
    if details_payload is not None and not scoped:
        details_payload = _redact_telemetry_value(details_payload)
    details_json = None
    if details_payload is not None:
        details_json = json.dumps(details_payload, default=_json_default, sort_keys=True)
    try:
        return store.record_failure(
            recorded_at=_utc_now_iso(),
            operation=operation,
            episode_uuid=episode_uuid if scoped else _NON_EPISODE_SUBJECT_KEY,
            failure_stage=failure_stage,
            classification=classification,
            retryable=retryable,
            processing_attempt=processing_attempt,
            queue_depth=queue_depth,
            worker_id=worker_id,
            error_type=error_type,
            error=error if scoped else "[redacted]",
            details_json=details_json,
        )
    except Exception as exc:  # pragma: no cover - telemetry must never break the caller
        _log_telemetry_persist_failure(f"record_failure_event:{operation}", exc)
        return False


def current_traceback_text() -> str:
    """Return the active exception traceback as text, if any."""

    rendered = traceback.format_exc()
    if not rendered or rendered.strip() == "NoneType: None":
        return ""
    return rendered


def record_episode_task_event(
    *,
    episode_uuid: str,
    parent_task: str | None,
    child_task: str | None,
    phase: str,
    kind: str | None = None,
    model: str | None = None,
    endpoint: str | None = None,
    scheduler_task: str | None = None,
    details: Any = None,
    store: McpTelemetryStore = telemetry_store,
) -> None:
    details_json = None
    if details is not None:
        details_json = json.dumps(details, default=_json_default, sort_keys=True)
    try:
        store.record_episode_task_event(
            recorded_at=_utc_now_iso(),
            episode_uuid=episode_uuid,
            parent_task=parent_task,
            child_task=child_task,
            phase=phase,
            kind=kind,
            model=model,
            endpoint=endpoint,
            scheduler_task=scheduler_task,
            details_json=details_json,
        )
    except Exception as exc:  # pragma: no cover - telemetry must never break the caller
        _log_telemetry_persist_failure(
            f"record_episode_task_event:{phase}:{episode_uuid or 'unknown'}",
            exc,
        )


def record_lifecycle_event(
    *,
    component: str,
    event: str,
    state: str,
    episode_uuid: str | None = None,
    details: Any = None,
    store: McpTelemetryStore = telemetry_store,
) -> None:
    """Persist a lifecycle event with addressable or explicitly operational lineage."""
    scoped = bool(str(episode_uuid or "").strip())
    details_payload = details if scoped else _redact_telemetry_value(details)
    details_json = None
    if details_payload is not None:
        details_json = json.dumps(details_payload, default=_json_default, sort_keys=True)
    try:
        store.record_lifecycle_event(
            recorded_at=_utc_now_iso(),
            component=component,
            event=event,
            state=state,
            episode_uuid=episode_uuid if scoped else _NON_EPISODE_SUBJECT_KEY,
            details_json=details_json,
        )
    except Exception as exc:  # pragma: no cover - telemetry must never break the caller
        _log_telemetry_persist_failure(f"record_lifecycle_event:{component}:{event}:{state}", exc)


def record_lifecycle_action(
    *,
    action: str,
    node_uuid: str,
    session_id: str | None = None,
    trigger: str,
    before_freshness: str | None = None,
    after_freshness: str | None = None,
    llm_used: bool = False,
    duration_ms: int | None = None,
    notes: str | None = None,
    store: McpTelemetryStore = telemetry_store,
) -> None:
    """Module-level convenience wrapper around ``telemetry_store.record_lifecycle_action``."""

    try:
        store.record_lifecycle_action(
            action=action,
            node_uuid=node_uuid,
            session_id=session_id,
            trigger=trigger,
            before_freshness=before_freshness,
            after_freshness=after_freshness,
            llm_used=llm_used,
            duration_ms=duration_ms,
            notes=notes,
        )
    except Exception as exc:  # pragma: no cover
        _log_telemetry_persist_failure(f"record_lifecycle_action:{action}:{node_uuid}", exc)


def record_merge(
    *,
    survivor_uuid: str,
    absorbed_uuid: str,
    similarity: float | None,
    snapshot_json: str,
    survivor_namespace: str | None = None,
    absorbed_namespace: str | None = None,
    store: McpTelemetryStore = telemetry_store,
) -> None:
    """Persist the durable merge audit with namespace lineage when derivable."""

    try:
        inferred_namespace: str | None = None
        try:
            payload = json.loads(snapshot_json)
            properties = payload.get("properties") if isinstance(payload, dict) else None
            if isinstance(properties, dict):
                inferred_namespace = str(properties.get("namespace") or "").strip() or None
        except (TypeError, ValueError, json.JSONDecodeError):
            inferred_namespace = None
        survivor_ns = str(survivor_namespace or "").strip() or inferred_namespace
        absorbed_ns = str(absorbed_namespace or "").strip() or inferred_namespace
        store.record_merge(
            survivor_uuid=survivor_uuid,
            absorbed_uuid=absorbed_uuid,
            similarity=similarity,
            snapshot_json=snapshot_json,
            survivor_namespace=survivor_ns,
            absorbed_namespace=absorbed_ns,
        )
    except Exception as exc:  # pragma: no cover - telemetry must never break the caller
        _log_telemetry_persist_failure(
            f"record_merge:{survivor_uuid}:{absorbed_uuid}", exc
        )


def record_memory_revision(
    *,
    node_uuid: str,
    field: str,
    old_value: str | None,
    new_value: str | None,
    changed_by: str,
    episode_uuid: str | None = None,
    store: McpTelemetryStore = telemetry_store,
) -> None:
    """Module-level convenience wrapper around ``telemetry_store.record_memory_revision``."""

    try:
        store.record_memory_revision(
            node_uuid=node_uuid,
            field=field,
            old_value=old_value,
            new_value=new_value,
            changed_by=changed_by,
            episode_uuid=episode_uuid,
        )
    except Exception as exc:  # pragma: no cover
        _log_telemetry_persist_failure(f"record_memory_revision:{node_uuid}:{field}", exc)


def record_destructive_op(
    *,
    surface: str,
    name: str,
    tier: str,
    session_id: str,
    user_id: str,
) -> None:
    """Record a destructive operation (operator-tier action) for audit purposes."""
    try:
        record_mcp_event(
            kind="background",
            operation="destructive_op",
            payload={
                "surface": surface,
                "name": name,
                "tier": tier,
                "session_id": session_id,
                "user_id": user_id,
            },
            success=True,
        )
    except Exception as exc:  # pragma: no cover - telemetry must never break the caller
        _log_telemetry_persist_failure("record_destructive_op", exc)
