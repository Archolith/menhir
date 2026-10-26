"""Pure signal helpers for conflict detection (plan: menhir-conflict-detection-signal-2026-08-09).

Two things live here, both free of I/O so they are cheap to test:

* ``pair_cosine`` -- a real cosine similarity on the documented [-1, 1] scale (in practice [0, 1]
  for the embedders Menhir uses). ``CorrelationService`` thresholds are written as cosine values
  but the score ``search_scored`` hands it is a graphiti RRF rank-fusion score on ~[0, 2]; this is
  the separate accessor the plan asks for, so ``search_scored`` itself is left untouched.
* ``attributes_overlap`` -- the scoping rule for the shared-episode veto. Two entities extracted
  from one episode cannot contradict each other *unless* they speak about the same attribute (a
  turn can say "retry limit was 5, actually 7"). Overlap is decided from the entity names only:
  they overlap when they share a non-numeric word. Disjoint names (two different fonts) do not.
  The rule errs toward "overlap" (no veto), so a doubtful pair still reaches the queue exactly as
  it does today.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence

_TOKEN = re.compile(r"[a-z0-9]+")


def pair_cosine(a: Sequence[float] | None, b: Sequence[float] | None) -> float | None:
    """Cosine similarity of two embedding vectors, or ``None`` when it is not defined.

    Undefined (``None``) for a missing vector, a length mismatch, or a zero-norm vector. The
    caller must treat ``None`` as "no signal", never as 0.0 (which would read as dissimilar).
    """
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


def attribute_tokens(name: str | None) -> frozenset[str]:
    """Lower-cased alphanumeric words of ``name`` that carry no digit.

    Words containing a digit (``400``, ``v2``, ``5s``) are values, not attribute identity, so
    they are dropped: ``timeout 5s`` and ``timeout 7s`` both reduce to ``{timeout}``.
    """
    return frozenset(
        token for token in _TOKEN.findall(str(name or "").lower())
        if not any(ch.isdigit() for ch in token)
    )


def attributes_overlap(name_a: str | None, name_b: str | None) -> bool:
    """True when the two names could be about the same attribute (veto must NOT fire).

    Conservative: an entity with no attribute words at all (empty / purely numeric name) cannot
    be shown disjoint from anything, so it counts as overlapping.
    """
    tokens_a = attribute_tokens(name_a)
    tokens_b = attribute_tokens(name_b)
    if not tokens_a or not tokens_b:
        return True
    return bool(tokens_a & tokens_b)
