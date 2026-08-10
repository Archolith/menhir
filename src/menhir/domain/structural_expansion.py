"""Bounded structural expansion (R3 rung F / belief-layer.md "Structural expansion").

A semantic/vector hit is often NOT the bug-relevant memory — the real cause is a
structural neighbor (the caller that changed, the test covering the file, a recent git
neighbor, a superseded-belief neighbor). menhir already injects file-context candidates;
this generalizes that into a bounded expansion:

    seed hits -> structural neighbor expansion -> bounded candidate pool

The whole risk is unbounded graph blow-up, so the guards are the point (belief-layer.md):
    max_clones_per_hit, max_total_clones, blast-radius depth limit,
    utility-symbol suppression (skip high-centrality hub nodes a la a logger),
    centrality penalty.

This is a pure, deterministic candidate-generation step over a neighbor provider; it does
NOT score or assert (that is the belief/oracle layers). It only decides WHICH structural
neighbors are allowed into the candidate pool, with provenance for each.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class StructuralNeighbor:
    """A depth-1 structural neighbor of a node, with the relation that links them."""

    id: str
    relation: str  # caller | callee | imports | imported_by | test_of | git_neighbor | same_error | superseded_neighbor | todo


@dataclass(frozen=True)
class ExpansionConfig:
    """Guards that keep structural expansion bounded (belief-layer.md required guards)."""

    max_clones_per_hit: int = 5     # each seed may contribute at most this many neighbors
    max_total_clones: int = 20      # hard cap on the whole expansion pool
    max_depth: int = 2              # blast-radius depth limit
    utility_degree_threshold: int = 25  # skip hub/utility nodes whose degree exceeds this

    def __post_init__(self) -> None:
        for name in ("max_clones_per_hit", "max_total_clones", "max_depth"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")


@dataclass(frozen=True)
class ExpandedCandidate:
    """One structural neighbor admitted to the pool, with provenance."""

    id: str
    source_hit: str  # the seed (or nearer node) it expanded from
    relation: str
    depth: int


@dataclass
class ExpansionResult:
    candidates: list[ExpandedCandidate] = field(default_factory=list)
    skipped_utility: list[str] = field(default_factory=list)   # suppressed hub nodes
    truncated: bool = False                                     # a cap was hit

    @property
    def candidate_ids(self) -> list[str]:
        return [c.id for c in self.candidates]


def expand_structural(
    seed_hits: list[str],
    neighbor_fn: Callable[[str], list[StructuralNeighbor]],
    *,
    config: ExpansionConfig | None = None,
    degree_fn: Callable[[str], int] | None = None,
) -> ExpansionResult:
    """Bounded breadth-first structural expansion from the seed hits.

    Returns neighbors NOT already among the seeds, deterministically ordered (depth, then
    discovery order). Guards applied: per-hit cap, total cap, depth limit, and utility
    suppression (a neighbor whose degree exceeds ``utility_degree_threshold`` is skipped —
    hub nodes like a shared logger would otherwise pull in the whole graph).
    """
    cfg = config or ExpansionConfig()
    seeds = set(seed_hits)
    result = ExpansionResult()
    admitted: set[str] = set()

    for seed in seed_hits:
        if len(result.candidates) >= cfg.max_total_clones:
            result.truncated = True
            break
        per_hit = 0
        # BFS from this seed; attribute every admitted node to this seed.
        queue: deque[tuple[str, int]] = deque([(seed, 0)])
        visited: set[str] = {seed}
        while queue:
            node, depth = queue.popleft()
            if depth >= cfg.max_depth:
                continue
            for nb in neighbor_fn(node):
                if nb.id in visited:
                    continue
                visited.add(nb.id)
                queue.append((nb.id, depth + 1))
                if nb.id in seeds or nb.id in admitted:
                    continue
                if degree_fn is not None and degree_fn(nb.id) > cfg.utility_degree_threshold:
                    result.skipped_utility.append(nb.id)
                    continue
                if per_hit >= cfg.max_clones_per_hit:
                    result.truncated = True
                    break
                if len(result.candidates) >= cfg.max_total_clones:
                    result.truncated = True
                    break
                result.candidates.append(
                    ExpandedCandidate(id=nb.id, source_hit=seed, relation=nb.relation, depth=depth + 1)
                )
                admitted.add(nb.id)
                per_hit += 1
            else:
                continue
            break  # inner break propagated: a cap was hit for this seed
    return result
