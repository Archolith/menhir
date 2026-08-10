"""Concurrency gate for Graphiti ingestion.

A single global ``asyncio.Lock`` used to serialize every ``add_episode`` call. That is
necessary when extraction runs on a memory-sensitive LOCAL model (concurrent runs OOM
it), but it is a hard throughput cap for cloud providers — where episodes in DIFFERENT
namespaces (``group_id``s) touch disjoint subgraphs and therefore cannot race on entity
dedup.

``IngestGate`` keeps the correctness guarantee while allowing parallelism:

- at most ``concurrency`` episodes extract at once, globally (a semaphore), and
- at most ONE per ``group_id`` (namespace), so same-subgraph dedup never races.

``concurrency=1`` reproduces the old single-flight behavior exactly, so the default is a
no-op against the previous global lock.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

# Episodes with no namespace share one serialization key (they all land in the default
# group), preserving the old behavior for unscoped ingestion.
_DEFAULT_KEY = "__default__"


class IngestGate:
    """Bound Graphiti ingestion concurrency, serialized per namespace.

    Acquisition order is per-group-lock THEN semaphore: an episode blocked behind a
    same-namespace peer must not occupy a global concurrency slot while it waits.
    """

    def __init__(self, concurrency: int = 1) -> None:
        if concurrency < 1:
            raise ValueError(f"concurrency must be >= 1, got {concurrency}")
        self._concurrency = concurrency
        self._semaphore = asyncio.Semaphore(concurrency)
        self._group_locks: dict[str, asyncio.Lock] = {}
        self._group_refcounts: dict[str, int] = {}
        self._map_lock = asyncio.Lock()

    @property
    def concurrency(self) -> int:
        return self._concurrency

    @asynccontextmanager
    async def acquire(self, group_id: str | None) -> AsyncIterator[None]:
        """Hold the per-namespace lock + a global concurrency slot for the body."""
        key = group_id or _DEFAULT_KEY
        lock = await self._checkout_group_lock(key)
        try:
            async with lock:
                async with self._semaphore:
                    yield
        finally:
            await self._return_group_lock(key)

    async def _checkout_group_lock(self, key: str) -> asyncio.Lock:
        # Refcount under _map_lock so concurrent checkouts for the same key share ONE
        # lock object and an idle key can be pruned without losing a live waiter.
        async with self._map_lock:
            lock = self._group_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._group_locks[key] = lock
            self._group_refcounts[key] = self._group_refcounts.get(key, 0) + 1
            return lock

    async def _return_group_lock(self, key: str) -> None:
        async with self._map_lock:
            count = self._group_refcounts.get(key, 0) - 1
            if count <= 0:
                self._group_refcounts.pop(key, None)
                self._group_locks.pop(key, None)
            else:
                self._group_refcounts[key] = count

    def active_group_count(self) -> int:
        """Number of namespaces with an in-flight or waiting acquirer (diagnostics/tests)."""
        return len(self._group_locks)
