"""MCP tool: get_episode_trace."""

from __future__ import annotations

from menhir.domain.utils import decode_json_value
from menhir.mcp.formatters import _coerce_iso, _require_episode_uuid
from menhir.mcp.tools.base import BaseJsonTool
from menhir.mcp.contracts import ToolScope


async def get_episode_trace(episode_uuid: str, limit: int = 20) -> str:
    """Return a compact debug trace for one episode using queue state plus telemetry sidecar rows."""

    return await GetEpisodeTraceTool().execute(episode_uuid=episode_uuid, limit=limit)


class GetEpisodeTraceTool(BaseJsonTool):
    name = "get_episode_trace"
    scope = ToolScope.OBJECT
    required_tier = "readonly"
    description = "Return a compact debug trace for one episode."

    async def endpoint(self, episode_uuid: str, limit: int = 20) -> str:
        backend = self.get_backend()
        normalized_uuid = _require_episode_uuid(episode_uuid)
        row = await backend.fetch_episode_processing(normalized_uuid)
        task_events = await backend.fetch_episode_task_events(
            episode_uuid=normalized_uuid,
            limit=max(1, min(limit, 50)),
        )
        failure_events = await backend.fetch_recent_failures(
            limit=max(1, min(limit, 50)),
            episode_uuid=normalized_uuid,
        )
        lifecycle_events = await backend.fetch_recent_lifecycle_events(
            limit=max(1, min(limit, 50)),
            episode_uuid=normalized_uuid,
        )
        return self.render_json(
            {
                "episode_uuid": normalized_uuid,
                "current": None
                if row is None
                else {
                    "state": row.get("processing_state"),
                    "stage": row.get("processing_stage"),
                    "substage": row.get("processing_substage"),
                    "progress": row.get("processing_progress"),
                    "attempts": row.get("processing_attempts"),
                    "owner": row.get("processing_owner"),
                    "lease_expires_at": _coerce_iso(row.get("processing_lease_expires_at")),
                    "heartbeat_at": _coerce_iso(row.get("processing_heartbeat_at")),
                    "llm_last_task_at": _coerce_iso(row.get("processing_llm_last_task_at")),
                    "llm_active_task": row.get("processing_llm_active_task"),
                    "llm_active_kind": row.get("processing_llm_active_kind"),
                    "llm_active_model": row.get("processing_llm_active_model"),
                    "llm_active_endpoint": row.get("processing_llm_active_endpoint"),
                    "error": row.get("processing_error"),
                },
                "task_events": [
                    {
                        "recorded_at": item.get("recorded_at"),
                        "phase": item.get("phase"),
                        "kind": item.get("kind"),
                        "model": item.get("model"),
                        "endpoint": item.get("endpoint"),
                        "scheduler_task": item.get("scheduler_task"),
                        "details": decode_json_value(item.get("details_json")),
                    }
                    for item in task_events
                ],
                "failure_events": [
                    {
                        "recorded_at": item.get("recorded_at"),
                        "operation": item.get("operation"),
                        "failure_stage": item.get("failure_stage"),
                        "classification": item.get("classification"),
                        "processing_attempt": item.get("processing_attempt"),
                        "error_type": item.get("error_type"),
                        "error": item.get("error"),
                        "details": decode_json_value(item.get("details_json")),
                        "traceback_preview": (
                            (decode_json_value(item.get("details_json")) or {}).get("traceback_preview")
                            if isinstance(decode_json_value(item.get("details_json")), dict)
                            else None
                        ),
                    }
                    for item in failure_events
                ],
                "lifecycle_events": [
                    {
                        "recorded_at": item.get("recorded_at"),
                        "component": item.get("component"),
                        "event": item.get("event"),
                        "state": item.get("state"),
                        "details": decode_json_value(item.get("details_json")),
                    }
                    for item in lifecycle_events
                ],
            }
        )
