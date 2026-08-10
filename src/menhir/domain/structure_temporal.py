"""Time-aware blast radius (StructureTemporalOracle, step 1 — the windowed change filter).

The killer debugging query: "Test C failed. What does it depend on (structure), and which of
those changed between when it last passed and now (git)?" = blast-radius x time.

This is pure composition of the built domain modules — it adds orchestration, not new
staleness/identity logic:
    structural_expansion.expand_structural  -> the bounded blast radius from the anchor
    git_staleness.GitChange (.matches, rename-aware) + a [start, end] window filter
                                             -> which of those dependencies changed in-window

It OBSERVES and RANKS (Oracle altitude); it does not decide (Warden) or write (Mutator).
Output feeds the wardens downstream.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from menhir.domain.git_staleness import GitChange
from menhir.domain.structural_expansion import (
    ExpansionConfig,
    StructuralNeighbor,
    expand_structural,
)


def _parse(ts: str | None) -> datetime | None:
    if ts is None:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _in_window(changed_at: str, start: str | None, end: str | None) -> bool:
    """Inclusive [start, end] window. A missing bound is open on that side."""
    when = _parse(changed_at)
    if when is None:
        return False
    lo, hi = _parse(start), _parse(end)
    if lo is not None and when < lo:
        return False
    if hi is not None and when > hi:
        return False
    return True


@dataclass(frozen=True)
class ChangedDependency:
    """A structural dependency of the anchor that changed within the time window."""

    id: str
    relation: str                       # how it relates to the anchor (caller/callee/test/...)
    depth: int                          # blast-radius depth from the anchor
    changes: tuple[GitChange, ...]      # the in-window changes to it (provenance)

    @property
    def change_count(self) -> int:
        return len(self.changes)


def changed_in_window(
    anchor: str,
    neighbor_fn: Callable[[str], list[StructuralNeighbor]],
    changes: list[GitChange],
    *,
    window_start: str | None,
    window_end: str | None,
    config: ExpansionConfig | None = None,
    degree_fn: Callable[[str], int] | None = None,
) -> list[ChangedDependency]:
    """Blast-radius x time: the anchor's structural dependencies that changed in-window.

    Step 1: bounded structural expansion from the anchor (guards apply — depth/per-hit/total
    caps, utility suppression). Step 2: keep the dependencies with >=1 git change whose
    timestamp falls in [window_start, window_end] (rename-aware via GitChange.matches).
    Deterministic order: shallower depth first, then id. The anchor itself is excluded
    (it is the seed); we report what it depends on, not the anchor's own change."""
    radius = expand_structural([anchor], neighbor_fn, config=config, degree_fn=degree_fn)
    out: list[ChangedDependency] = []
    for cand in radius.candidates:
        in_window = [
            c for c in changes
            if c.matches({cand.id}) and _in_window(c.changed_at, window_start, window_end)
        ]
        if in_window:
            out.append(
                ChangedDependency(
                    id=cand.id, relation=cand.relation, depth=cand.depth,
                    changes=tuple(in_window),
                )
            )
    out.sort(key=lambda d: (d.depth, d.id))
    return out


@dataclass(frozen=True)
class StructureTemporalQuery:
    """Input to the oracle: a failing anchor + the time window to look back over."""

    anchor: str
    window_start: str | None   # e.g. when the test last passed
    window_end: str | None     # e.g. now / when it failed


@dataclass(frozen=True)
class RankedDependency:
    """A changed dependency with the oracle's transparent suspicion score + rationale."""

    dependency: ChangedDependency
    score: float
    rationale: str


@dataclass
class StructureTemporalOracle:
    """Oracle: rank the anchor's in-window-changed structural dependencies by suspicion.

    Read-only. Composes ``changed_in_window`` (blast-radius x time) then scores each changed
    dependency by a transparent, inspectable signal:

        score = depth_proximity (closer to the anchor = more suspect)
              + change_recency  (changed later in the window = more suspect)
              + change_density  (more changes in-window = more churn = more suspect)

    It OBSERVES and RANKS; it does not decide admission (Warden) or write (Mutator). The
    output feeds the wardens. No new staleness/identity logic — pure orchestration."""

    neighbor_fn: Callable[[str], list[StructuralNeighbor]]
    degree_fn: Callable[[str], int] | None = None
    config: ExpansionConfig | None = None
    name: str = "structure_temporal"

    def evaluate(self, query: StructureTemporalQuery, changes: list[GitChange]) -> list[RankedDependency]:
        deps = changed_in_window(
            query.anchor, self.neighbor_fn, changes,
            window_start=query.window_start, window_end=query.window_end,
            config=self.config, degree_fn=self.degree_fn,
        )
        ranked = [self._score(d, query) for d in deps]
        ranked.sort(key=lambda r: (-r.score, r.dependency.depth, r.dependency.id))
        return ranked

    def _score(self, dep: ChangedDependency, query: StructureTemporalQuery) -> RankedDependency:
        # depth proximity: 1.0 at depth 1, decaying with distance from the anchor.
        proximity = 1.0 / float(dep.depth) if dep.depth > 0 else 1.0
        # recency within the window: latest in-window change, normalized to [0, 1].
        recency = _window_recency(dep, query.window_start, query.window_end)
        # density: log-damped change count so churn helps but doesn't dominate.
        from math import log
        density = log(1 + dep.change_count)
        score = round(proximity + recency + density, 6)
        rationale = (
            f"depth={dep.depth}(prox {proximity:.2f}) + recency {recency:.2f} + "
            f"{dep.change_count} change(s)(density {density:.2f}) via {dep.relation}"
        )
        return RankedDependency(dependency=dep, score=score, rationale=rationale)


def _window_recency(dep: ChangedDependency, start: str | None, end: str | None) -> float:
    """How late in [start, end] the dependency's latest change is, in [0, 1].

    Later changes are more suspect for a just-broke test. Falls back to 0.5 (neutral) when
    the window is open-ended or unparseable — no false precision."""
    lo, hi = _parse(start), _parse(end)
    latest = max((_parse(c.changed_at) for c in dep.changes if _parse(c.changed_at)), default=None)
    if latest is None or lo is None or hi is None or hi <= lo:
        return 0.5
    frac = (latest - lo).total_seconds() / (hi - lo).total_seconds()
    return max(0.0, min(1.0, frac))
