"""Real query-to-name cosine for recall candidates (plan: menhir-recall-real-similarity-test).

``search_scored`` returns a rank-fusion score, so a candidate's real similarity is otherwise
lost. This reads it back from the stored ``name_embedding``. It runs only for an explicit
experiment tuning, and it assumes the query embedder built the stored vectors.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Sequence
from dataclasses import replace
from functools import partial
from typing import Any

from menhir.domain.recall import CandidateData
from menhir.domain.retrieval_trace_models import NameCosineRecord

logger = logging.getLogger(__name__)


def name_cosine(a: Sequence[float] | None, b: Sequence[float] | None) -> float | None:
    """Cosine on the raw [-1, 1] scale, or ``None`` when undefined (never 0.0, which reads as
    dissimilar): a missing vector, a length mismatch or a zero vector."""
    if not a or not b or len(a) != len(b):
        return None
    dot = norm_a = norm_b = 0.0
    for x, y in zip(a, b):
        fx, fy = float(x), float(y)
        dot += fx * fy
        norm_a += fx * fx
        norm_b += fy * fy
    if norm_a <= 0.0 or norm_b <= 0.0:
        return None
    return max(-1.0, min(1.0, dot / math.sqrt(norm_a * norm_b)))


async def attach_name_cosines(
    service: Any,
    query: str,
    candidates: list[CandidateData],
    namespace: str | None,
) -> tuple[list[CandidateData], NameCosineRecord]:
    """Return candidates with ``name_cosine`` set, and the record of what was read.

    Must never break recall. On failure the candidates come back unchanged, so a floor arm
    behaves like its baseline; the record's warning says so.
    """
    if not candidates:
        return candidates, NameCosineRecord(cosines={})
    try:
        query_vector = await service.graphiti_client.embed_query(query)
        vectors = await asyncio.to_thread(
            partial(
                service.graph_adapter.fetch_name_embeddings,
                [c.uuid for c in candidates],
                namespace=namespace,
            )
        )
    except Exception as exc:
        logger.error("Name cosine unavailable: %s: %s", exc.__class__.__name__, exc, exc_info=True)
        return candidates, NameCosineRecord(
            cosines={}, warning=f"name_cosine_unavailable:{exc.__class__.__name__}"
        )
    updated = [
        replace(c, name_cosine=name_cosine(query_vector, vectors.get(c.uuid)))
        for c in candidates
    ]
    return updated, NameCosineRecord(cosines={c.uuid: c.name_cosine for c in updated})


__all__ = ["attach_name_cosines", "name_cosine"]
