"""Retrieval diversity gate (R8 / retrieval-control-rails.md Guard 4).

Set-level anti-spiral rail: when the top-of-list collapses onto one evidence family
(top_memory_dominance high or retrieval_entropy low), interleave families so one semantic
cluster cannot monopolize the context window. Unlike the per-candidate Wardens this reasons
over the whole ranked SET, so it lives here as a pure list->list transform and is called from
recall after the combiner, not inside the warden chain.

Determinism (control-rails non-goal: rails must not reorder nondeterministically): when NOT
collapsed the input is returned unchanged; when collapsed the reorder is a stable round-robin
by family, families ordered by first appearance, within-family order preserved. Same input ->
same output, always.
"""

from __future__ import annotations

import math
from collections import Counter, OrderedDict
from collections.abc import Callable, Sequence
from typing import TypeVar

T = TypeVar("T")

# Trigger defaults (transparent starting point; the bench tunes them).
_DEFAULT_DOMINANCE_MAX = 0.6   # one family may hold up to 60% of the window before we act
_DEFAULT_ENTROPY_MIN = 0.5     # normalized family entropy below this is "collapsed"
_DEFAULT_MAX_PER_FAMILY = 3    # cap any single family's run in the interleave


def family_distribution(families: Sequence[str]) -> dict[str, int]:
    """Count candidates per evidence family (insertion order preserved)."""
    counts: OrderedDict[str, int] = OrderedDict()
    for fam in families:
        counts[fam] = counts.get(fam, 0) + 1
    return dict(counts)


def dominance(families: Sequence[str]) -> float:
    """Fraction of the set held by the single most-represented family (0 when empty)."""
    if not families:
        return 0.0
    counts = Counter(families)
    return max(counts.values()) / len(families)


def normalized_entropy(families: Sequence[str]) -> float:
    """Shannon entropy of the family distribution, normalized to [0, 1].

    1.0 = perfectly uniform across the families present; 0.0 = one family. A single distinct
    family (or empty) returns 0.0 (no diversity)."""
    if not families:
        return 0.0
    counts = Counter(families)
    if len(counts) <= 1:
        return 0.0
    total = len(families)
    h = -sum((c / total) * math.log2(c / total) for c in counts.values())
    return h / math.log2(len(counts))


def is_collapsed(
    families: Sequence[str],
    *,
    dominance_max: float = _DEFAULT_DOMINANCE_MAX,
    entropy_min: float = _DEFAULT_ENTROPY_MIN,
) -> bool:
    """Guard 4 trigger: dominance over the cap OR entropy under the floor."""
    if not families:
        return False
    return dominance(families) > dominance_max or normalized_entropy(families) < entropy_min


def diversify(
    items: Sequence[T],
    family_of: Callable[[T], str],
    *,
    max_per_family: int = _DEFAULT_MAX_PER_FAMILY,
    dominance_max: float = _DEFAULT_DOMINANCE_MAX,
    entropy_min: float = _DEFAULT_ENTROPY_MIN,
) -> list[T]:
    """Return ``items`` reordered for family diversity, or unchanged if not collapsed.

    Deterministic, stable: families are queued in first-appearance order; the result is a
    round-robin draw across those queues (at most ``max_per_family`` consecutive from one
    family), preserving each family's internal order. A pure permutation of the input."""
    max_per_family = max(1, max_per_family)
    items = list(items)
    families = [family_of(it) for it in items]
    if not is_collapsed(families, dominance_max=dominance_max, entropy_min=entropy_min):
        return items

    queues: OrderedDict[str, list[T]] = OrderedDict()
    for it, fam in zip(items, families):
        queues.setdefault(fam, []).append(it)

    out: list[T] = []
    run_family: str | None = None
    run_len = 0
    while queues:
        progressed = False
        for fam in list(queues.keys()):
            if fam == run_family and run_len >= max_per_family:
                continue
            q = queues[fam]
            out.append(q.pop(0))
            if not q:
                del queues[fam]
            run_len = run_len + 1 if fam == run_family else 1
            run_family = fam
            progressed = True
        if not progressed:  # only remaining family is over its run cap -> drain it
            fam, q = next(iter(queues.items()))
            out.extend(q)
            del queues[fam]
    return out
