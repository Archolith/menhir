"""In-memory LRU embedding cache.

M6 Phase 4 — SHA256-keyed cache for OpenAI-compatible embedding responses.
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict

logger = logging.getLogger(__name__)


class EmbeddingCache:
    """In-process LRU cache keyed by SHA256(model + text). Resets on process restart."""

    def __init__(self, max_size: int = 512) -> None:
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._max_size = max_size
        self._hits = 0
        self._misses = 0

    def _key(self, text: str, model: str = "") -> str:
        return hashlib.sha256(f"{model}\x00{text}".encode()).hexdigest()

    def get(self, text: str, model: str = "") -> list[float] | None:
        key = self._key(text, model)
        result = self._cache.get(key)
        if result is not None:
            self._hits += 1
            self._cache.move_to_end(key)
        else:
            self._misses += 1
        return result

    def set(self, text: str, embedding: list[float], model: str = "") -> None:
        key = self._key(text, model)
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = embedding
        if len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def stats(self) -> dict[str, int]:
        return {"hits": self._hits, "misses": self._misses, "size": len(self._cache)}


# Module-level singleton — one per MCP server process.
_default_embedding_cache = EmbeddingCache()


def get_embedding_cache() -> EmbeddingCache:
    """Return the process-wide embedding cache singleton."""
    return _default_embedding_cache
