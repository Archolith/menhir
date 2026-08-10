"""Tests for IngestGate: bounded global concurrency + per-namespace serialization."""

from __future__ import annotations

import asyncio

import pytest

from menhir.services.ingest_gate import IngestGate


async def _run_peak(gate: IngestGate, group_for: "callable", n: int, hold_s: float = 0.02) -> int:
    """Run n bodies through the gate concurrently; return the peak simultaneous occupancy."""
    state = {"inside": 0, "peak": 0}

    async def worker(i: int) -> None:
        async with gate.acquire(group_for(i)):
            state["inside"] += 1
            state["peak"] = max(state["peak"], state["inside"])
            await asyncio.sleep(hold_s)
            state["inside"] -= 1

    await asyncio.gather(*[worker(i) for i in range(n)])
    return state["peak"]


def test_rejects_non_positive_concurrency() -> None:
    with pytest.raises(ValueError):
        IngestGate(0)
    with pytest.raises(ValueError):
        IngestGate(-3)


def test_default_concurrency_is_single_flight() -> None:
    # concurrency=1 reproduces the old global lock: distinct namespaces never overlap.
    gate = IngestGate(1)
    peak = asyncio.run(_run_peak(gate, lambda i: f"ns-{i}", n=4))
    assert peak == 1
    assert gate.active_group_count() == 0  # locks pruned after release


def test_bounds_global_concurrency_across_namespaces() -> None:
    # 6 distinct namespaces, concurrency=2 -> at most 2 extract at once.
    gate = IngestGate(2)
    peak = asyncio.run(_run_peak(gate, lambda i: f"ns-{i}", n=6))
    assert peak == 2
    assert gate.active_group_count() == 0


def test_serializes_same_namespace_despite_high_concurrency() -> None:
    # Same group_id must never overlap (dedup safety), even with headroom in the semaphore.
    gate = IngestGate(4)
    peak = asyncio.run(_run_peak(gate, lambda i: "same", n=5))
    assert peak == 1
    assert gate.active_group_count() == 0


def test_distinct_namespaces_run_in_parallel_up_to_bound() -> None:
    # With concurrency >= n distinct namespaces, all run together (peak == n).
    gate = IngestGate(4)
    peak = asyncio.run(_run_peak(gate, lambda i: f"ns-{i}", n=4))
    assert peak == 4


def test_none_group_uses_a_shared_default_key() -> None:
    # Unscoped episodes share one serialization key (old default-group behavior).
    gate = IngestGate(4)
    peak = asyncio.run(_run_peak(gate, lambda i: None, n=3))
    assert peak == 1
    assert gate.active_group_count() == 0


def test_group_locks_are_pruned_when_idle() -> None:
    async def _go() -> int:
        gate = IngestGate(2)
        async with gate.acquire("a"):
            async with gate.acquire("b"):
                peak = gate.active_group_count()
        # both released
        assert gate.active_group_count() == 0
        return peak

    assert asyncio.run(_go()) == 2
