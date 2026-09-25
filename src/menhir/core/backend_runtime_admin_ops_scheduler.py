"""Maintenance-scheduler takeover operations for the in-process backend adapter."""

from __future__ import annotations

from typing import Any

from .backend_shared import _to_jsonable


class SchedulerStartBlockedInBenchmarkMode(RuntimeError):
    """Force-takeover tried to start the maintenance scheduler under benchmark isolation."""


class RuntimeSchedulerOpsMixin:
    """Maintenance-scheduler takeover operations for the in-process backend adapter."""

    async def scheduler_force_takeover(self, reason: str) -> bool:
        scheduler = getattr(self.built, "scheduler", None)
        if scheduler is None:
            # Only the START is gated. Benchmark isolation forbids creating a maintenance
            # scheduler mid-measurement (same guarantee startup enforces in core/runtime.py);
            # takeover of a scheduler that already exists is not what it forbids -- and under
            # benchmark mode startup never created one, so this branch is the only reachable
            # path here. Refusing is visible: raise rather than return a bare False that would
            # be indistinguishable from "takeover attempted and lost".
            settings = getattr(self.built, "settings", None)
            if getattr(settings, "benchmark_mode", False):
                raise SchedulerStartBlockedInBenchmarkMode(
                    "cannot start the maintenance scheduler while benchmark_mode is set "
                    "(MENHIR_BENCHMARK_MODE=1); starting it would void the benchmark isolation "
                    "guarantee. Takeover of an already-running scheduler is not affected."
                )
            from menhir.core.runtime import _start_scheduler

            scheduler = await _start_scheduler(self.built)
        return bool(await scheduler.force_takeover(reason=reason))

    async def scheduler_status_snapshot(self) -> dict[str, Any] | None:
        scheduler = getattr(self.built, "scheduler", None)
        if scheduler is None:
            return None
        return _to_jsonable(await self._off_loop(scheduler.status_snapshot))

    async def scheduler_pause(self) -> bool:
        scheduler = getattr(self.built, "scheduler", None)
        if scheduler is None:
            return False
        was_running = await self._off_loop(scheduler.is_running)
        await scheduler.stop()
        return was_running

    async def scheduler_resume(self) -> bool:
        scheduler = getattr(self.built, "scheduler", None)
        if scheduler is None:
            return False
        return bool(await scheduler.start())
