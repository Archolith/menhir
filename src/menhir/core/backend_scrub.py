"""Scrubbing proxy for MemoryBackend results (#577).

``ScrubbingBackend`` wraps a backend so every awaited operation result passes through
``scrub_payload`` exactly once. Arguments are never scrubbed. Sync callables, async
generators and non-callable attributes pass through untouched. A scan failure raises
``PayloadScrubFailed`` (fail closed).
"""
from __future__ import annotations

import functools
import inspect
from typing import Any

from menhir.domain.payload_scrub import scrub_payload


class ScrubbingBackend:
    """Forwards every attribute to the wrapped backend, scrubbing async results."""

    def __init__(self, inner: Any) -> None:
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(object.__getattribute__(self, "_inner"), name)
        if not callable(attr):
            return attr

        # Decide per call, not per function: a non-async decorator hides `async def` from
        # iscoroutinefunction and would silently skip the scrub.
        @functools.wraps(attr)
        def _scrubbed(*args: Any, **kwargs: Any) -> Any:
            result = attr(*args, **kwargs)
            if not inspect.isawaitable(result):
                return result

            async def _await_and_scrub() -> Any:
                return scrub_payload(await result)

            return _await_and_scrub()

        return _scrubbed

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_inner"), name, value)


def wrap_backend(backend: Any) -> Any:
    """Return ``backend`` wrapped in a ``ScrubbingBackend`` (idempotent)."""
    if isinstance(backend, ScrubbingBackend):
        return backend
    return ScrubbingBackend(backend)
