"""SQLite telemetry store and recorders — infrastructure layer.

Services, infrastructure, and MCP code all import telemetry from here.
The ``mcp.telemetry`` package re-exports these symbols alongside the
MCP-specific ``track_mcp_call`` helper so existing imports keep working.
"""

from .recorders import (
    current_traceback_text,
    record_destructive_op,
    record_episode_task_event,
    record_failure_event,
    record_lifecycle_action,
    record_lifecycle_event,
    record_llm_usage_event,
    record_mcp_event,
    record_memory_revision,
    record_merge,
)
from .store import (
    McpTelemetryStore,
    connect_telemetry_db,
    default_telemetry_db_path,
    telemetry_store,
)


def enable_llm_usage_telemetry() -> None:
    """Route non-episode LLM calls into the process telemetry sidecar."""

    from menhir.infrastructure.observability import set_default_llm_usage_callback

    set_default_llm_usage_callback(lambda event: record_llm_usage_event(event=event))

__all__ = [
    "McpTelemetryStore",
    "connect_telemetry_db",
    "default_telemetry_db_path",
    "telemetry_store",
    "current_traceback_text",
    "record_destructive_op",
    "record_episode_task_event",
    "record_failure_event",
    "record_lifecycle_action",
    "record_lifecycle_event",
    "record_llm_usage_event",
    "record_mcp_event",
    "record_memory_revision",
    "record_merge",
    "enable_llm_usage_telemetry",
]
