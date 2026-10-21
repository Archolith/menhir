"""Which embedding model produced the source-memory vectors (`content_embedding_model`).

Ingest stamps every content embedding with the embedder's model name; recall, the ingest
idempotency check and the backfill compare against the CURRENT embedder so vectors from a
different model (same dimension, different space) are never ranked together and are re-embedded.
When the model cannot be resolved the check is skipped (``None``) rather than excluding every
stored vector.
"""

from __future__ import annotations

from typing import Any

UNKNOWN_EMBEDDING_MODEL = "unknown"


def embedder_model_name(graphiti_client: Any) -> str:
    """Best-effort embedder model name; ``'unknown'`` when it cannot be resolved."""
    embedder = getattr(graphiti_client, "embedder_ref", None)
    if embedder is not None:
        for attr in ("model", "model_name", "embed_model"):
            value = getattr(embedder, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return UNKNOWN_EMBEDDING_MODEL


def comparable_model(model: str | None) -> str | None:
    """The model to filter/compare on, or None when it is blank or unresolved."""
    value = (model or "").strip()
    return None if not value or value == UNKNOWN_EMBEDDING_MODEL else value
