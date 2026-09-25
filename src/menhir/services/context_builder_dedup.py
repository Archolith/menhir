"""Redundancy filter for context building.

Normalization, Jaccard overlap scoring, and near-duplicate collapse over recalled
memories. Moved verbatim from ``context_builder.py``, which re-exports ``_deduplicate``.
"""

from __future__ import annotations

import re

from menhir.domain.recall import ScoredMemory

# ---------------------------------------------------------------------------
# Redundancy filter
# ---------------------------------------------------------------------------

_PUNCTUATION_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = text.lower()
    text = _PUNCTUATION_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _deduplicate(memories: list[ScoredMemory]) -> list[ScoredMemory]:
    """Remove near-duplicate memories, keeping the highest scorer.

    Never collapses memories with different claim shapes (view_kind values) —
    a View may only dedup against another View with the same view_kind.
    """
    if not memories:
        return memories

    # Pre-compute normalized text and word sets
    norm_map: dict[str, str] = {}
    words_map: dict[str, set[str]] = {}
    for m in memories:
        raw = m.content or m.name
        norm = _normalize(raw)
        norm_map[m.uuid] = norm
        words_map[m.uuid] = set(norm.split())

    # Exact-match collapse (skip if different claim shapes)
    seen_norms: dict[tuple[str, str | None], ScoredMemory] = {}
    unique: list[ScoredMemory] = []
    for m in memories:
        norm = norm_map[m.uuid]
        # Use (normalized_text, view_kind) as dedup key to prevent collapsing across shapes
        dedup_key = (norm, m.view_kind)
        existing = seen_norms.get(dedup_key)
        if existing is not None:
            if m.final_score > existing.final_score:
                unique = [u if u.uuid != existing.uuid else m for u in unique]
                seen_norms[dedup_key] = m
        else:
            seen_norms[dedup_key] = m
            unique.append(m)

    # Jaccard overlap collapse (skip if different claim shapes)
    kept: list[ScoredMemory] = []
    for m in unique:
        words_m = words_map[m.uuid]
        redundant = False
        for k in kept:
            # Only dedup if both have same view_kind (or both are None)
            if m.view_kind == k.view_kind and _jaccard(words_m, words_map[k.uuid]) > 0.8:
                redundant = True
                break
        if not redundant:
            kept.append(m)

    return kept
