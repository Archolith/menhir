"""In-memory LRU embedding cache.

M6 Phase 4 — SHA256-keyed cache for OpenAI-compatible embedding responses.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from collections import OrderedDict

logger = logging.getLogger(__name__)

_ENV_MAX_SIZE = "MENHIR_EMBEDDING_CACHE_MAX_SIZE"
_DEFAULT_MAX_SIZE = 512


def _default_max_size() -> int:
    """Resolve the module-level singleton's capacity from the environment.

    Missing, empty, non-integer, zero, or negative values fall back to the
    default of 512 and log a warning naming the bad value. A cache with
    max_size <= 0 would evict on every set, which is worse than the default.
    """
    raw = os.getenv(_ENV_MAX_SIZE)
    if raw is None or raw.strip() == "":
        return _DEFAULT_MAX_SIZE
    try:
        parsed = int(raw.strip())
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; falling back to %d",
            _ENV_MAX_SIZE,
            raw,
            _DEFAULT_MAX_SIZE,
        )
        return _DEFAULT_MAX_SIZE
    if parsed <= 0:
        logger.warning(
            "%s=%r must be positive; falling back to %d",
            _ENV_MAX_SIZE,
            raw,
            _DEFAULT_MAX_SIZE,
        )
        return _DEFAULT_MAX_SIZE
    return parsed


class EmbeddingCache:
    """In-process LRU cache keyed by SHA256(model + text). Resets on process restart."""

    def __init__(self, max_size: int = 512) -> None:
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._max_size = max_size
        self._hits = 0
        self._misses = 0
        # get() writes (LRU reorder plus hit/miss counters) and this cache is
        # reachable from worker threads, so mutations must be serialized.
        self._lock = threading.Lock()

    def _key(self, text: str, model: str = "") -> str:
        return hashlib.sha256(f"{model}\x00{text}".encode()).hexdigest()

    def get(self, text: str, model: str = "") -> list[float] | None:
        key = self._key(text, model)
        with self._lock:
            result = self._cache.get(key)
            if result is not None:
                self._hits += 1
                self._cache.move_to_end(key)
            else:
                self._misses += 1
        return result

    def set(self, text: str, embedding: list[float], model: str = "") -> None:
        key = self._key(text, model)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = embedding
            if len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"hits": self._hits, "misses": self._misses, "size": len(self._cache)}


# Module-level singleton — one per MCP server process.
_default_embedding_cache = EmbeddingCache(max_size=_default_max_size())


def get_embedding_cache() -> EmbeddingCache:
    """Return the process-wide embedding cache singleton."""
    return _default_embedding_cache
