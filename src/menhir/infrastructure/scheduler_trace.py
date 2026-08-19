"""Generic task-trace registration for the external model scheduler service."""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any

import httpx

from menhir.domain.utils import excerpt
from menhir.infrastructure.llama_endpoint import scheduler_url_from_env
from menhir.infrastructure.telemetry import record_lifecycle_event

logger = logging.getLogger(__name__)

_MEMORY_SOURCE_PAYLOAD = {
    "source": "memory",
    "display_name": "menhir",
    "description": "Graph memory enrichment and recall jobs",
    "resource_uri_template": "memory://node/{node_uuid}",
}
_registered = False
_register_lock = asyncio.Lock()


async def register_scheduler_task_source() -> None:
    global _registered
    async with _register_lock:
        if _registered:
            await asyncio.to_thread(
                partial(
                    record_lifecycle_event,
                    component="scheduler_trace",
                    event="register_task_source",
                    state="skipped",
                    details={"reason": "already_registered", "source": "memory"},
                )
            )
            return
        scheduler_url = scheduler_url_from_env().rstrip("/")
        await asyncio.to_thread(
            partial(
                record_lifecycle_event,
                component="scheduler_trace",
                event="register_task_source",
                state="started",
                details={"source": "memory", "scheduler_url": scheduler_url},
            )
        )
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.post(f"{scheduler_url}/task-sources/register", json=_MEMORY_SOURCE_PAYLOAD)
                response.raise_for_status()
            _registered = True
            await asyncio.to_thread(
                partial(
                    record_lifecycle_event,
                    component="scheduler_trace",
                    event="register_task_source",
                    state="completed",
                    details={"source": "memory", "scheduler_url": scheduler_url},
                )
            )
        except (httpx.HTTPError, OSError) as exc:
            await asyncio.to_thread(
                partial(
                    record_lifecycle_event,
                    component="scheduler_trace",
                    event="register_task_source",
                    state="failed",
                    details={"source": "memory", "scheduler_url": scheduler_url, "error": str(exc)},
                )
            )
            logger.warning("scheduler task source registration failed: %s", exc)


async def emit_scheduler_task_event(
    *,
    parent_job_id: str,
    parent_label: str,
    parent_state: str,
    parent_heartbeat_at: str | None = None,
    parent_error: str | None = None,
    parent_metadata: dict[str, Any] | None = None,
    child: dict[str, Any] | None = None,
) -> None:
    scheduler_url = scheduler_url_from_env().rstrip("/")
    payload = {
        "source": "memory",
        "parent_job_id": parent_job_id,
        "parent_label": parent_label,
        "parent_state": parent_state,
        "parent_heartbeat_at": parent_heartbeat_at,
        "parent_error": parent_error,
        "parent_metadata": parent_metadata or {},
    }
    if child is not None:
        payload["child"] = child
    await asyncio.to_thread(
        partial(
            record_lifecycle_event,
            component="scheduler_trace",
            event="emit_task_event",
            state="started",
            episode_uuid=parent_job_id,
            details={
                "scheduler_url": scheduler_url,
                "parent_state": parent_state,
                "has_child": child is not None,
            },
        )
    )
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.post(f"{scheduler_url}/task-events", json=payload)
            response.raise_for_status()
        await asyncio.to_thread(
            partial(
                record_lifecycle_event,
                component="scheduler_trace",
                event="emit_task_event",
                state="completed",
                episode_uuid=parent_job_id,
                details={
                    "scheduler_url": scheduler_url,
                    "parent_state": parent_state,
                    "has_child": child is not None,
                },
            )
        )
    except (httpx.HTTPError, OSError) as exc:
        await asyncio.to_thread(
            partial(
                record_lifecycle_event,
                component="scheduler_trace",
                event="emit_task_event",
                state="failed",
                episode_uuid=parent_job_id,
                details={
                    "scheduler_url": scheduler_url,
                    "parent_state": parent_state,
                    "has_child": child is not None,
                    "error": str(exc),
                },
            )
        )
        logger.warning("scheduler task event failed parent_job_id=%s: %s", parent_job_id, exc)


def build_episode_scheduler_task(*, episode_uuid: str, provider: str, action: str) -> str:
    normalized_episode = "".join(ch for ch in (episode_uuid or "unknown") if ch.isalnum()) or "unknown"
    normalized_provider = "".join(ch if ch.isalnum() else "-" for ch in provider).strip("-") or "unknown"
    normalized_action = "".join(ch if ch.isalnum() else "-" for ch in action).strip("-") or "unknown"
    return f"memory-{normalized_episode}--{normalized_provider}-{normalized_action}"


def build_episode_parent_metadata(
    *,
    attempts: int,
    source: str | None = None,
    content: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "attempts": int(attempts),
    }
    if source:
        metadata["source"] = source
    preview = excerpt(content)
    if preview:
        metadata["summary"] = preview
    elif name:
        metadata["summary"] = str(name)
    return metadata


def build_episode_child_details(
    *,
    attempt: int,
    step_key: str,
    step_label: str,
    source: str | None = None,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "attempt": int(attempt),
        "step_key": step_key,
        "step_label": step_label,
    }
    if source:
        details["source"] = source
    return details
