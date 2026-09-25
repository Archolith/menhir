"""Conflict, scheduler, telemetry, and mutation operations for the in-process backend adapter.

The operation groups live in sibling modules (``backend_runtime_admin_ops_<aspect>.py``); this
module composes them into the public ``RuntimeProviderAdminOpsMixin`` mixin and re-exports the
benchmark-mode scheduler exception that ``scheduler_force_takeover`` raises.
"""

from __future__ import annotations

from .backend_runtime_admin_ops_conflicts import RuntimeConflictOpsMixin
from .backend_runtime_admin_ops_scheduler import (
    RuntimeSchedulerOpsMixin,
    SchedulerStartBlockedInBenchmarkMode,
)
from .backend_runtime_admin_ops_telemetry import RuntimeTelemetryOpsMixin
from .backend_runtime_admin_ops_diagnostics import RuntimeDiagnosticsOpsMixin
from .backend_runtime_admin_ops_todos import RuntimeTodoOpsMixin
from .backend_runtime_admin_ops_artifacts import RuntimeArtifactOpsMixin
from .backend_runtime_admin_ops_memory import RuntimeMemoryAdminOpsMixin


class RuntimeProviderAdminOpsMixin(
    RuntimeConflictOpsMixin,
    RuntimeSchedulerOpsMixin,
    RuntimeTelemetryOpsMixin,
    RuntimeDiagnosticsOpsMixin,
    RuntimeTodoOpsMixin,
    RuntimeArtifactOpsMixin,
    RuntimeMemoryAdminOpsMixin,
):
    """Conflict, scheduler, telemetry, and mutation operations for the in-process backend adapter."""
