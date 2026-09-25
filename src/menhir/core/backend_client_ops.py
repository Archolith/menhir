"""Operation methods for the HTTP-backed backend adapter."""

from __future__ import annotations

from .backend_client_ops_memory import BackendClientMemoryOpsMixin
from .backend_client_ops_projects import BackendClientProjectsOpsMixin
from .backend_client_ops_pipeline import BackendClientPipelineOpsMixin
from .backend_client_ops_todos import BackendClientTodosOpsMixin
from .backend_client_ops_artifacts import BackendClientArtifactsOpsMixin


class BackendClientOpsMixin(
    BackendClientMemoryOpsMixin,
    BackendClientProjectsOpsMixin,
    BackendClientPipelineOpsMixin,
    BackendClientTodosOpsMixin,
    BackendClientArtifactsOpsMixin,
):
    """Behavior mixin holding the large HTTP operation surface."""
