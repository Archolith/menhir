"""The "already in progress, skipping" guards drop concurrent callers rather than queueing them.

CF-242 WAS REFUTED, and this file is what remains of it. The finding (carried from the July 2026
audit as "consolidation-lock TOCTOU STILL PRESENT") reads the shape

    if self._consolidation_lock.locked():
        return <zero result>
    async with self._consolidation_lock:
        ...

as check-then-act: two callers both observe the lock free, both proceed, and the second blocks and
runs a full pass instead of skipping. That window is NOT REACHABLE here.
`asyncio.Lock.acquire()` on an uncontended lock takes the lock and returns without yielding, and
there is no `await` between the check and the acquire -- so on a single-threaded event loop no
other task can be scheduled in between. Measured: 2,000 randomized trials of 2-8 concurrent
callers, maximum simultaneously inside the body = 1, callers queueing after passing the check = 0.

WHAT WOULD MAKE IT REAL, and why these tests are kept: inserting any await between the check and
the `async with` -- an awaited log call, a metric emit, a permission check -- opens the window for
real. The shape is correct today and fragile to that one edit, so the invariant is pinned
behaviourally here rather than left to a reader of the guard.

These tests are NOT a regression test for a fix; they pass against both the original shape and any
rewrite. They assert the property: while one pass is running, a second caller returns the zero
result immediately and never enters the body. Call A is held inside the critical section by a run
body that signals `started` and then waits on a `release` event the test controls, so any queueing
by B would hang it rather than pass.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

import pytest

from menhir.services.lifecycle_consolidation import ConsolidationResult, LifecycleConsolidationMixin
from menhir.services.lifecycle_decay import DecayResult, LifecycleDecayMixin

# `record_mcp_event` is imported at module scope in both lifecycle modules; the run bodies here
# never actually run, but the skip path returns before that call. Patching it to a no-op keeps the
# services constructible with only the attributes the method under test touches.
_LC_CONSOLIDATION = "menhir.services.lifecycle_consolidation"
_LC_DECAY = "menhir.services.lifecycle_decay"

_REAL_CONSOLIDATION = ConsolidationResult(
    promoted=3, deleted=1, conflicts_detected=0, skipped_pending=0,
    orphan_episodes_cleaned=2, demoted=0,
)
_REAL_DECAY = DecayResult(
    edge_counts_synced=5, sharpness_recalculated=4, compressed=2,
    deleted=0, edges_bridged=1, orphan_subgraphs_cleaned=0,
)
_ZERO_CONSOLIDATION = ConsolidationResult(0, 0, 0, 0, 0)
_ZERO_DECAY = DecayResult(0, 0, 0, 0, 0, 0)


def _make_slow_run(
    counter: list[int],
    started: asyncio.Event,
    release: asyncio.Event,
    result: object,
) -> Callable[..., Awaitable[object]]:
    async def slow_run(*_args: object, **_kwargs: object) -> object:
        counter.append(1)
        started.set()
        await release.wait()
        return result

    return slow_run


def _build_consolidation_service(slow_run: Callable[..., Awaitable[object]]) -> LifecycleConsolidationMixin:
    service = LifecycleConsolidationMixin.__new__(LifecycleConsolidationMixin)
    service._consolidation_lock = asyncio.Lock()
    service._consolidation_running = False
    service._run_consolidation = slow_run
    return service


def _build_decay_service(slow_run: Callable[..., Awaitable[object]]) -> LifecycleDecayMixin:
    service = LifecycleDecayMixin.__new__(LifecycleDecayMixin)
    service._decay_lock = asyncio.Lock()
    service._decay_running = False
    service._run_decay = slow_run
    return service


# ---------------------------------------------------------------------------
# Consolidation
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_consolidation_second_caller_skips_instead_of_queuing() -> None:
    counter: list[int] = []
    started = asyncio.Event()
    release = asyncio.Event()
    service = _build_consolidation_service(_make_slow_run(counter, started, release, _REAL_CONSOLIDATION))

    call_a = asyncio.create_task(service.consolidate_session())
    await started.wait()
    assert len(counter) == 1  # A is inside _run_consolidation (inside the lock)

    # B must return the zero result IMMEDIATELY and must not run the body. If B queued behind A,
    # this await would hang until release is set.
    result_b = await asyncio.wait_for(service.consolidate_session(), timeout=1.0)
    assert result_b == _ZERO_CONSOLIDATION
    assert len(counter) == 1  # B skipped; it did not queue and did not run

    release.set()
    result_a = await call_a
    assert result_a == _REAL_CONSOLIDATION
    assert len(counter) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_consolidation_runs_when_nothing_in_progress() -> None:
    # Positive control: a call with nothing running must actually run -- otherwise a fix that
    # always returns the zero result would pass every skip assertion above.
    counter: list[int] = []
    started = asyncio.Event()
    release = asyncio.Event()
    service = _build_consolidation_service(_make_slow_run(counter, started, release, _REAL_CONSOLIDATION))

    call_a = asyncio.create_task(service.consolidate_session())
    await started.wait()
    release.set()
    result = await call_a
    assert result == _REAL_CONSOLIDATION
    assert len(counter) == 1


# ---------------------------------------------------------------------------
# Decay
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_decay_second_caller_skips_instead_of_queuing() -> None:
    counter: list[int] = []
    started = asyncio.Event()
    release = asyncio.Event()
    service = _build_decay_service(_make_slow_run(counter, started, release, _REAL_DECAY))

    call_a = asyncio.create_task(service.apply_decay())
    await started.wait()
    assert len(counter) == 1  # A is inside _run_decay (inside the lock)

    result_b = await asyncio.wait_for(service.apply_decay(), timeout=1.0)
    assert result_b == _ZERO_DECAY
    assert len(counter) == 1  # B skipped; it did not queue and did not run

    release.set()
    result_a = await call_a
    assert result_a == _REAL_DECAY
    assert len(counter) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_decay_runs_when_nothing_in_progress() -> None:
    # Positive control: a call with nothing running must actually run.
    counter: list[int] = []
    started = asyncio.Event()
    release = asyncio.Event()
    service = _build_decay_service(_make_slow_run(counter, started, release, _REAL_DECAY))

    call_a = asyncio.create_task(service.apply_decay())
    await started.wait()
    release.set()
    result = await call_a
    assert result == _REAL_DECAY
    assert len(counter) == 1
