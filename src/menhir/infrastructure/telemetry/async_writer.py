"""Ordered off-loop writer for append-only observability telemetry (#527).

A telemetry write called on an event-loop thread is handed to one FIFO writer thread instead
of opening SQLite on the loop. Readers drain the queue first (``McpTelemetryStore._connect``),
and erasure drains strictly before it purges, so neither can miss a write submitted earlier.
A full queue makes the caller write synchronously: back-pressure, never a dropped event.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import queue
import threading
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

_QUEUE_MAX = 10_000
_SHUTDOWN_DRAIN_TIMEOUT_S = 5.0


class TelemetryDrainTimeout(RuntimeError):
    """Earlier telemetry writes were still queued when a strict drain gave up."""


def on_event_loop_thread() -> bool:
    """True when the calling thread is currently running an asyncio event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class TelemetryWriter:
    """One daemon thread applying queued writes in submission order."""

    def __init__(self, maxsize: int = _QUEUE_MAX) -> None:
        self._queue: queue.Queue[tuple[str, Callable[[], object]]] = queue.Queue(maxsize)
        self._start_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def submit(self, label: str, write: Callable[[], object]) -> bool:
        """Queue ``write``; False when the queue is full and the caller must write itself."""
        self._ensure_thread()
        try:
            self._queue.put_nowait((label, write))
        except queue.Full:
            return False
        return True

    def pending(self) -> int:
        with self._queue.mutex:
            return self._queue.unfinished_tasks

    def is_writer_thread(self) -> bool:
        thread = self._thread
        return thread is not None and threading.current_thread() is thread

    def drain(self, timeout_s: float) -> bool:
        """Wait until every write submitted before this call has been applied.

        Returns False on timeout. Called from the writer thread itself it returns True at once:
        the writer applies items in order, so anything it reads was submitted before the item
        it is running.
        """
        if self.is_writer_thread() or self.pending() == 0:
            return True
        done = threading.Event()
        deadline = time.monotonic() + max(0.0, timeout_s)
        try:
            self._queue.put(("drain-marker", done.set), timeout=max(0.0, timeout_s))
        except queue.Full:
            return False
        self._ensure_thread()
        return done.wait(max(0.0, deadline - time.monotonic()))

    def drain_strict(self, timeout_s: float) -> None:
        if not self.drain(timeout_s):
            raise TelemetryDrainTimeout(
                f"{self.pending()} telemetry write(s) still queued after {timeout_s:.1f}s"
            )

    def _ensure_thread(self) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            return
        with self._start_lock:
            thread = self._thread
            if thread is not None and thread.is_alive():
                return
            thread = threading.Thread(
                target=self._run, name="menhir-telemetry-writer", daemon=True
            )
            thread.start()
            self._thread = thread

    def _run(self) -> None:
        while True:
            label, write = self._queue.get()
            try:
                write()
            except Exception as exc:  # noqa: BLE001 - telemetry must never break anything
                logger.warning(
                    "Off-loop telemetry write %s failed (%s): %s", label, type(exc).__name__, exc
                )
            finally:
                self._queue.task_done()


TELEMETRY_WRITER = TelemetryWriter()


class OffLoopObservabilityWritesMixin:
    """Route append-only observability writes off the event loop.

    Only rows nothing reads back to decide a transition belong here. ``record_memory_revision``,
    ``record_merge`` and ``record_destructive_op`` hold recovery and history data that recall and
    unmerge read, so they stay synchronous. An offloaded bool-returning write reports True
    (accepted); no production caller reads that value.
    """

    def _offloaded(self, label: str, write: Callable[[], object]) -> bool:
        return on_event_loop_thread() and TELEMETRY_WRITER.submit(label, write)

    def record(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        parent = super().record  # type: ignore[misc]
        if self._offloaded("mcp_event", lambda: parent(*args, **kwargs)):
            return None
        return parent(*args, **kwargs)

    def record_failure(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        parent = super().record_failure  # type: ignore[misc]
        if self._offloaded("failure_event", lambda: parent(*args, **kwargs)):
            return True
        return parent(*args, **kwargs)

    def record_episode_task_event(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        parent = super().record_episode_task_event  # type: ignore[misc]
        if self._offloaded("episode_task_event", lambda: parent(*args, **kwargs)):
            return None
        return parent(*args, **kwargs)

    def record_lifecycle_event(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        parent = super().record_lifecycle_event  # type: ignore[misc]
        if self._offloaded("lifecycle_event", lambda: parent(*args, **kwargs)):
            return None
        return parent(*args, **kwargs)

    def record_lifecycle_action(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        parent = super().record_lifecycle_action  # type: ignore[misc]
        if self._offloaded("lifecycle_action", lambda: parent(*args, **kwargs)):
            return None
        return parent(*args, **kwargs)

    def record_llm_usage_event(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        parent = super().record_llm_usage_event  # type: ignore[misc]
        if self._offloaded("llm_usage_event", lambda: parent(*args, **kwargs)):
            return True
        return parent(*args, **kwargs)


def _drain_at_exit() -> None:
    if not TELEMETRY_WRITER.drain(_SHUTDOWN_DRAIN_TIMEOUT_S):
        logger.warning(
            "Exiting with %d telemetry write(s) unapplied", TELEMETRY_WRITER.pending()
        )


atexit.register(_drain_at_exit)


__all__ = [
    "OffLoopObservabilityWritesMixin",
    "TELEMETRY_WRITER",
    "TelemetryDrainTimeout",
    "TelemetryWriter",
    "on_event_loop_thread",
]
