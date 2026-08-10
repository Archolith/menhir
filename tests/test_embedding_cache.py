"""Unit tests for EmbeddingCache (M6 Phase 4)."""

from __future__ import annotations

import hashlib

from menhir.infrastructure.embedding_cache import (
    EmbeddingCache,
    get_embedding_cache,
)


# ---------------------------------------------------------------------------
# 1. Cache miss returns None
# ---------------------------------------------------------------------------

def test_cache_miss_returns_none():
    cache = EmbeddingCache()
    assert cache.get("never stored") is None


# ---------------------------------------------------------------------------
# 2. Cache hit returns stored embedding after set()
# ---------------------------------------------------------------------------

def test_cache_hit_returns_stored_embedding():
    cache = EmbeddingCache()
    embedding = [0.1, 0.2, 0.3]
    cache.set("hello", embedding)
    assert cache.get("hello") == [0.1, 0.2, 0.3]


# ---------------------------------------------------------------------------
# 3. LRU eviction: oldest entry is evicted when max_size is exceeded
# ---------------------------------------------------------------------------

def test_lru_eviction_removes_oldest_entry():
    cache = EmbeddingCache(max_size=2)
    cache.set("a", [1.0])
    cache.set("b", [2.0])
    # Cache is now full (size=2). Adding a third should evict "a".
    cache.set("c", [3.0])

    assert cache.get("a") is None, "oldest entry 'a' should have been evicted"
    assert cache.get("b") == [2.0]
    assert cache.get("c") == [3.0]


def test_lru_eviction_respects_access_order():
    """Accessing an entry via get() should refresh it, so it survives eviction."""
    cache = EmbeddingCache(max_size=2)
    cache.set("a", [1.0])
    cache.set("b", [2.0])

    # Touch "a" so it becomes most-recently-used
    cache.get("a")

    # Adding "c" should now evict "b" (the least recently used)
    cache.set("c", [3.0])

    assert cache.get("a") == [1.0], "'a' was accessed recently and should survive"
    assert cache.get("b") is None, "'b' should have been evicted"
    assert cache.get("c") == [3.0]


# ---------------------------------------------------------------------------
# 4. stats() returns correct hit/miss/size counts
# ---------------------------------------------------------------------------

def test_stats_initial():
    cache = EmbeddingCache()
    assert cache.stats() == {"hits": 0, "misses": 0, "size": 0}


def test_stats_after_misses():
    cache = EmbeddingCache()
    cache.get("x")
    cache.get("y")
    assert cache.stats() == {"hits": 0, "misses": 2, "size": 0}


def test_stats_after_hits_and_misses():
    cache = EmbeddingCache()
    cache.set("x", [1.0])
    cache.get("x")       # hit
    cache.get("y")       # miss
    cache.get("x")       # hit
    assert cache.stats() == {"hits": 2, "misses": 1, "size": 1}


def test_stats_size_tracks_entries():
    cache = EmbeddingCache(max_size=5)
    for i in range(3):
        cache.set(f"item-{i}", [float(i)])
    assert cache.stats()["size"] == 3


def test_stats_size_respects_max():
    cache = EmbeddingCache(max_size=2)
    for i in range(5):
        cache.set(f"item-{i}", [float(i)])
    assert cache.stats()["size"] == 2


# ---------------------------------------------------------------------------
# 5. get_embedding_cache() returns a module-level singleton
# ---------------------------------------------------------------------------

def test_get_embedding_cache_returns_singleton():
    a = get_embedding_cache()
    b = get_embedding_cache()
    assert a is b


def test_get_embedding_cache_returns_embedding_cache_instance():
    assert isinstance(get_embedding_cache(), EmbeddingCache)


# ---------------------------------------------------------------------------
# 6. SHA256 key is deterministic (same text -> same key)
# ---------------------------------------------------------------------------

def test_key_is_deterministic():
    cache = EmbeddingCache()
    key1 = cache._key("deterministic input")
    key2 = cache._key("deterministic input")
    assert key1 == key2


def test_key_matches_sha256_hexdigest():
    cache = EmbeddingCache()
    text = "verify against hashlib"
    expected = hashlib.sha256(f"\x00{text}".encode()).hexdigest()
    assert cache._key(text) == expected


def test_different_texts_produce_different_keys():
    cache = EmbeddingCache()
    assert cache._key("alpha") != cache._key("beta")
