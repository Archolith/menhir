"""Compatibility mixin that composes the in-process backend operation groups."""

from __future__ import annotations

from .backend_runtime_admin_ops import RuntimeProviderAdminOpsMixin
from .backend_runtime_data_ops import RuntimeProviderDataOpsMixin


class RuntimeProviderOpsMixin(RuntimeProviderDataOpsMixin, RuntimeProviderAdminOpsMixin):
    """Aggregate all in-process backend operations for RuntimeProvider."""

    pass
