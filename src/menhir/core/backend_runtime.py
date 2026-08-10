"""In-process backend adapter over BuildArtifacts + telemetry."""

from __future__ import annotations

import asyncio
from typing import Any

from menhir.core.backend_protocol import MemoryBackend
from menhir.infrastructure.telemetry import telemetry_store

from .backend_runtime_ops import RuntimeProviderOpsMixin


class RuntimeProvider(RuntimeProviderOpsMixin, MemoryBackend):
    """In-process backend adapter over BuildArtifacts + telemetry."""

    def __init__(
        self,
        built: Any,
        process_session: Any,
        *,
        caller_session: Any | None = None,
        telemetry: Any = telemetry_store,
    ) -> None:
        self.built = built
        self.process_session = process_session
        self.caller_session = caller_session
        self.telemetry = telemetry

    async def _off_loop(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a blocking function in a thread so the event loop stays free."""
        if kwargs:
            import functools

            call = functools.partial(fn, *args, **kwargs)
            return await asyncio.to_thread(call)
        return await asyncio.to_thread(fn, *args)

    def _effective_session_id(self) -> str | None:
        session = self.caller_session or self.process_session
        return getattr(session, "session_id", None)
