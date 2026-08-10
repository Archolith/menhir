"""Tests for time-aware blast radius (StructureTemporalOracle step 1)."""

from __future__ import annotations

from menhir.domain.git_staleness import GitChange
from menhir.domain.structural_expansion import ExpansionConfig, StructuralNeighbor
from menhir.domain.structure_temporal import (
    StructureTemporalOracle,
    StructureTemporalQuery,
    changed_in_window,
)


def _graph(adj: dict[str, list[tuple[str, str]]]):
    def fn(node: str) -> list[StructuralNeighbor]:
        return [StructuralNeighbor(id=n, relation=r) for n, r in adj.get(node, [])]
    return fn


def test_surfaces_only_in_window_changed_dependency() -> None:
    """The killer query: test depends on {dep_changed, dep_unchanged, dep_changed_old};
    only the dependency changed inside [last_passed, now] is surfaced."""
    nf = _graph({"testC": [("dep_changed", "callee"), ("dep_unchanged", "callee"), ("dep_old", "callee")]})
    changes = [
        GitChange("dep_changed", "2026-06-15", commit="c1"),   # in window
        GitChange("dep_old", "2026-01-01", commit="c0"),       # before window
        # dep_unchanged: no change at all
    ]
    out = changed_in_window(
        "testC", nf, changes,
        window_start="2026-06-01", window_end="2026-06-28",
        config=ExpansionConfig(max_depth=1),
    )
    ids = [d.id for d in out]
    assert ids == ["dep_changed"]
    assert out[0].relation == "callee" and out[0].change_count == 1


def test_window_bounds_are_inclusive_and_open_ended() -> None:
    nf = _graph({"a": [("d", "callee")]})
    # exactly on the start bound -> included
    assert changed_in_window("a", nf, [GitChange("d", "2026-06-01")],
                             window_start="2026-06-01", window_end=None,
                             config=ExpansionConfig(max_depth=1))
    # open start, bounded end: a later change is excluded
    assert not changed_in_window("a", nf, [GitChange("d", "2026-07-01")],
                                 window_start=None, window_end="2026-06-28",
                                 config=ExpansionConfig(max_depth=1))


def test_anchor_itself_is_not_reported() -> None:
    nf = _graph({"anchor": [("dep", "callee")]})
    out = changed_in_window("anchor", nf, [GitChange("anchor", "2026-06-15"), GitChange("dep", "2026-06-15")],
                            window_start="2026-06-01", window_end="2026-06-28",
                            config=ExpansionConfig(max_depth=1))
    assert [d.id for d in out] == ["dep"]   # the anchor (seed) is excluded


def test_rename_aware_change_matches_dependency() -> None:
    """A change that renamed a dependency still matches the old-path anchor (durable id)."""
    nf = _graph({"t": [("old/Dep.java", "callee")]})
    out = changed_in_window(
        "t", nf,
        [GitChange("new/Dep.java", "2026-06-15", renamed_from="old/Dep.java")],
        window_start="2026-06-01", window_end="2026-06-28",
        config=ExpansionConfig(max_depth=1),
    )
    assert [d.id for d in out] == ["old/Dep.java"]


def test_ordered_by_depth_then_id() -> None:
    nf = _graph({"a": [("z1", "callee"), ("m1", "callee")], "z1": [("deep", "callee")]})
    changes = [GitChange(x, "2026-06-15") for x in ("z1", "m1", "deep")]
    out = changed_in_window("a", nf, changes, window_start="2026-06-01", window_end="2026-06-28",
                            config=ExpansionConfig(max_depth=2, max_clones_per_hit=9))
    # depth 1 (m1, z1 sorted by id) before depth 2 (deep)
    assert [d.id for d in out] == ["m1", "z1", "deep"]


def test_no_changes_returns_empty() -> None:
    nf = _graph({"a": [("d", "callee")]})
    assert changed_in_window("a", nf, [], window_start="2026-06-01", window_end="2026-06-28",
                             config=ExpansionConfig(max_depth=1)) == []


# --- StructureTemporalOracle (step 2) --------------------------------------

def test_oracle_ranks_closest_recent_churned_dependency_first() -> None:
    """A direct dependency changed late with churn outranks a distant single early change."""
    nf = _graph({"testC": [("near_hot", "callee"), ("far", "callee")], "near_hot": [("deep_old", "callee")]})
    changes = [
        GitChange("near_hot", "2026-06-25", commit="a"),   # depth 1, late
        GitChange("near_hot", "2026-06-20", commit="b"),   # + churn
        GitChange("deep_old", "2026-06-26", commit="c"),   # depth 2, even later but distant
    ]
    oracle = StructureTemporalOracle(neighbor_fn=nf, config=ExpansionConfig(max_depth=2, max_clones_per_hit=9))
    ranked = oracle.evaluate(
        StructureTemporalQuery(anchor="testC", window_start="2026-06-01", window_end="2026-06-28"),
        changes,
    )
    assert ranked[0].dependency.id == "near_hot"   # proximity + density win
    assert ranked[0].score > ranked[1].score
    assert "change(s)" in ranked[0].rationale


def test_oracle_is_readonly_no_write_methods() -> None:
    oracle = StructureTemporalOracle(neighbor_fn=_graph({}))
    assert not any(hasattr(oracle, m) for m in ("create", "promote", "supersede", "write", "mutate"))


def test_oracle_empty_when_nothing_changed_in_window() -> None:
    nf = _graph({"a": [("d", "callee")]})
    oracle = StructureTemporalOracle(neighbor_fn=nf, config=ExpansionConfig(max_depth=1))
    out = oracle.evaluate(
        StructureTemporalQuery(anchor="a", window_start="2026-06-01", window_end="2026-06-28"),
        [GitChange("d", "2025-01-01")],   # out of window
    )
    assert out == []
