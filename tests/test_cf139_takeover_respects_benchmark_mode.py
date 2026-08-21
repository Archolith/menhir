"""CF-139: `scheduler_force_takeover` must not START the scheduler under benchmark mode.

Benchmark isolation (`MENHIR_BENCHMARK_MODE=1`) is read in exactly one place in all of
`src/menhir` (startup, in `core/runtime.py`) with the guarantee that no scheduler and no
orphan recovery run, so the store is never mutated mid-measurement. This path called
`_start_scheduler` without consulting that flag, silently voiding the guarantee. The fix gates
only the START: takeover of a scheduler that already exists is untouched (and, since benchmark
startup never creates one, that branch is unreachable under benchmark mode anyway).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from menhir.core.backend_runtime_admin_ops import (
    RuntimeProviderAdminOpsMixin,
    SchedulerStartBlockedInBenchmarkMode,
)


class _FakeScheduler:
    def __init__(self, takeover_result: bool = True) -> None:
        self.takeover_result = takeover_result
        self.takeover_calls = 0

    async def force_takeover(self, *, reason: str) -> bool:
        self.takeover_calls += 1
        return self.takeover_result


class _Settings:
    def __init__(self, benchmark_mode: bool = False) -> None:
        self.benchmark_mode = benchmark_mode


class _Built:
    def __init__(self, *, benchmark_mode: bool = False, scheduler: object | None = None) -> None:
        self.settings = _Settings(benchmark_mode=benchmark_mode)
        self.scheduler = scheduler


class _Ops(RuntimeProviderAdminOpsMixin):
    def __init__(self, built: _Built) -> None:
        self.built = built


def _run(coro) -> object:
    return asyncio.run(coro)


# 1. THE FINDING: with benchmark_mode on and no existing scheduler, a scheduler is NOT started.
@pytest.mark.unit
def test_benchmark_mode_blocks_scheduler_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = []

    async def fake_start_scheduler(built):
        called.append(built)
        return _FakeScheduler()

    monkeypatch.setattr("menhir.core.runtime._start_scheduler", fake_start_scheduler)

    ops = _Ops(_Built(benchmark_mode=True, scheduler=None))

    with pytest.raises(SchedulerStartBlockedInBenchmarkMode):
        _run(ops.scheduler_force_takeover(reason="benchmark-check"))

    assert called == [], "a scheduler must not be constructed under benchmark mode"


# 2. The refusal is distinguishable from a lost takeover (specific exception, not a bare False).
@pytest.mark.unit
def test_refusal_is_a_visible_distinct_signal() -> None:
    ops = _Ops(_Built(benchmark_mode=True, scheduler=None))

    with pytest.raises(SchedulerStartBlockedInBenchmarkMode) as excinfo:
        _run(ops.scheduler_force_takeover(reason="why"))

    assert "benchmark" in str(excinfo.value).lower()


# 3. POSITIVE CONTROL: benchmark OFF and no scheduler -> _start_scheduler IS called, takeover proceeds.
@pytest.mark.unit
def test_start_scheduler_and_takeover_proceed_without_benchmark_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_scheduler = _FakeScheduler(takeover_result=True)
    started_with = []

    async def fake_start_scheduler(built):
        started_with.append(built)
        return fake_scheduler

    monkeypatch.setattr("menhir.core.runtime._start_scheduler", fake_start_scheduler)

    built = _Built(benchmark_mode=False, scheduler=None)
    ops = _Ops(built)

    result = _run(ops.scheduler_force_takeover(reason="normal"))

    assert started_with == [built], "scheduler should be started when benchmark mode is off"
    assert fake_scheduler.takeover_calls == 1
    assert result is True


# 4. POSITIVE CONTROL: an ALREADY-RUNNING scheduler proceeds regardless of benchmark_mode.
#    We gated construction, not takeover.
@pytest.mark.unit
def test_takeover_of_running_scheduler_ignores_benchmark_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = []

    async def fake_start_scheduler(built):
        called.append(built)
        return _FakeScheduler()

    monkeypatch.setattr("menhir.core.runtime._start_scheduler", fake_start_scheduler)

    running = _FakeScheduler(takeover_result=True)
    ops = _Ops(_Built(benchmark_mode=True, scheduler=running))

    result = _run(ops.scheduler_force_takeover(reason="running"))

    assert called == [], "no scheduler should be constructed when one already exists"
    assert running.takeover_calls == 1
    assert result is True


# 5. Structural: `benchmark_mode` is now read in more than one place in src/menhir.
@pytest.mark.unit
def test_benchmark_mode_is_read_in_more_than_one_place() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "menhir"
    hits = 0
    for py in root.rglob("*.py"):
        hits += py.read_text(encoding="utf-8").count("benchmark_mode")

    assert hits > 1, (
        "`benchmark_mode` is still consulted at exactly one place in src/menhir; the isolation "
        "gate added for CF-139 must also read it"
    )
