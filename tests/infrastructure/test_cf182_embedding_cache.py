"""CF-182 regression and concurrency tests for EmbeddingCache.

Covers the LRU guard, the MENHIR_EMBEDDING_CACHE_MAX_SIZE env override (resolved
via the _default_max_size() helper, not module reload), the positive control,
thread safety, and the stats() contract.
"""

from __future__ import annotations

import concurrent.futures
from collections import OrderedDict

import pytest

from menhir.infrastructure.embedding_cache import (
    EmbeddingCache,
    _default_max_size,
    get_embedding_cache,
)


# ---------------------------------------------------------------------------
# 1. LRU regression guard
# ---------------------------------------------------------------------------

def test_lru_evicts_least_recently_used_and_get_promotes():
    cache = EmbeddingCache(max_size=2)
    cache.set("a", [1.0])
    cache.set("b", [2.0])

    # Touch "a" so it becomes most-recently-used.
    cache.get("a")

    # Adding "c" should evict "b" (the least recently used), not "a".
    cache.set("c", [3.0])

    assert cache.get("a") == [1.0], "promoted key should survive eviction"
    assert cache.get("b") is None, "least-recently-used key should be evicted"
    assert cache.get("c") == [3.0]


def test_reset_existing_key_does_not_grow_cache():
    cache = EmbeddingCache(max_size=2)
    cache.set("a", [1.0])
    cache.set("a", [1.5])

    assert cache.stats()["size"] == 1, "re-setting an existing key must not grow the cache"

    # The key should still be the only entry and retrievable.
    assert cache.get("a") == [1.5]


# ---------------------------------------------------------------------------
# 2. Env override via the capacity helper
# ---------------------------------------------------------------------------

def test_default_max_size_env_override(monkeypatch):
    monkeypatch.setenv("MENHIR_EMBEDDING_CACHE_MAX_SIZE", "64")
    assert _default_max_size() == 64


# ---------------------------------------------------------------------------
# 3. Bad env values fall back to 512
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "abc", "0", "-5"])
def test_default_max_size_bad_values_fall_back_to_default(monkeypatch, bad):
    monkeypatch.setenv("MENHIR_EMBEDDING_CACHE_MAX_SIZE", bad)
    assert _default_max_size() == 512


def test_default_max_size_missing_falls_back_to_default(monkeypatch):
    monkeypatch.delenv("MENHIR_EMBEDDING_CACHE_MAX_SIZE", raising=False)
    assert _default_max_size() == 512


# ---------------------------------------------------------------------------
# 4. Positive control: default capacity still caches with no env var set
# ---------------------------------------------------------------------------

def test_positive_control_default_capacity_still_caches(monkeypatch):
    monkeypatch.delenv("MENHIR_EMBEDDING_CACHE_MAX_SIZE", raising=False)
    cache = EmbeddingCache(max_size=_default_max_size())
    assert cache._max_size == 512
    cache.set("hello", [0.1, 0.2, 0.3])
    assert cache.get("hello") == [0.1, 0.2, 0.3]


# ---------------------------------------------------------------------------
# 5. Thread safety
#
# NOTE ON WHAT IS AND IS NOT PROVABLE HERE. The concurrency smoke test below
# passes on the UNLOCKED implementation too -- measured, 5/5 runs -- and so does
# a dedicated lost-update probe on the `_hits += 1` counter, at 640k increments
# across 32 threads with sys.setswitchinterval(1e-6). Under CPython's GIL this
# race is not empirically reproducible, so a concurrency test CANNOT stand as
# proof that the lock is present.
#
# The lock is still the correct change -- it is what makes the invariant hold
# under a free-threaded build, and correctness should not rest on an interpreter
# implementation detail -- so it is verified STRUCTURALLY instead: the test below
# proves every mutation happens while the lock is held, and it fails immediately
# if the lock is removed.
# ---------------------------------------------------------------------------

def test_every_mutation_happens_while_the_lock_is_held():
    """The real assertion: `get` WRITES (LRU reorder plus counters), and no write
    to the shared OrderedDict may occur outside the lock."""
    cache = EmbeddingCache(max_size=4)

    state = {"depth": 0}
    real_lock = cache._lock
    violations: list[str] = []

    class _TrackingLock:
        def __enter__(self):
            real_lock.acquire()
            state["depth"] += 1
            return self

        def __exit__(self, *exc):
            state["depth"] -= 1
            real_lock.release()
            return False

    class _GuardedDict(OrderedDict):
        def _check(self, op: str) -> None:
            if state["depth"] <= 0:
                violations.append(op)

        def __setitem__(self, key, value):
            self._check("__setitem__")
            super().__setitem__(key, value)

        def move_to_end(self, key, last=True):
            self._check("move_to_end")
            super().move_to_end(key, last=last)

        def popitem(self, last=True):
            self._check("popitem")
            return super().popitem(last=last)

    cache._lock = _TrackingLock()
    guarded = _GuardedDict(cache._cache)
    cache._cache = guarded

    cache.set("a", [1.0])
    cache.get("a")            # the read path that writes -- the point of the finding
    cache.get("missing")
    for i in range(6):        # force eviction via popitem
        cache.set(f"k{i}", [float(i)])
    cache.stats()

    assert violations == [], f"mutations outside the lock: {violations}"
    assert state["depth"] == 0, "lock was not released"


def test_concurrency_smoke_no_exception_and_capacity_respected():
    """SMOKE ONLY -- see the note above. This passes without the lock as well; it
    guards against a deadlock or an outright crash under contention, not the race."""
    max_size = 16
    cache = EmbeddingCache(max_size=max_size)

    def worker(i: int) -> None:
        for j in range(200):
            key = f"k{i}-{j % 40}"
            cache.set(key, [float(j)])
            cache.get(key)

    workers = 8
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(worker, range(workers)))

    assert cache.stats()["size"] <= max_size


# ---------------------------------------------------------------------------
# 6. stats() contract and hit/miss tracking
# ---------------------------------------------------------------------------

def test_stats_exact_keys_and_counters():
    cache = EmbeddingCache()
    assert set(cache.stats().keys()) == {"hits", "misses", "size"}

    cache.set("x", [1.0])
    cache.get("x")   # hit
    cache.get("y")   # miss

    stats = cache.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["size"] == 1


def test_the_singleton_actually_uses_the_resolved_capacity() -> None:
    """CALLER BOUNDARY: `_default_max_size()` can be perfectly correct and still be wired to
    nothing. Unwiring it from the module-level singleton leaves every other test in this file
    passing, so this asserts the construction site itself.

    Checked by parsing the module rather than by `importlib.reload`. Reloading rebinds
    `EmbeddingCache` to a NEW class object while other test modules still hold the old one, so
    `isinstance(get_embedding_cache(), EmbeddingCache)` in `tests/test_embedding_cache.py` starts
    failing depending on which xdist worker runs first. That is a real cross-test break, not a
    flake -- so this test must have no import-time side effects at all.
    """
    import ast
    import inspect

    from menhir.infrastructure import embedding_cache as module

    tree = ast.parse(inspect.getsource(module))

    construction = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_default_embedding_cache" for t in node.targets
        ):
            construction = node.value

    assert construction is not None, "module-level singleton assignment not found"
    assert isinstance(construction, ast.Call), "singleton is not constructed by a call"

    called = {
        n.func.id
        for n in ast.walk(construction)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "_default_max_size" in called, (
        "the singleton does not call _default_max_size(); the env override is wired to nothing"
    )
