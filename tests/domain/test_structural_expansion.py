"""Tests for bounded structural expansion (R3 rung F).

The guards are the point: per-hit cap, total cap, depth limit, utility suppression,
seed exclusion, dedup, determinism."""

from __future__ import annotations

from menhir.domain.structural_expansion import (
    ExpansionConfig,
    StructuralNeighbor,
    expand_structural,
)


def _graph(adj: dict[str, list[tuple[str, str]]]):
    """Build a neighbor_fn from {node: [(neighbor_id, relation), ...]}."""
    def neighbor_fn(node: str) -> list[StructuralNeighbor]:
        return [StructuralNeighbor(id=n, relation=r) for n, r in adj.get(node, [])]
    return neighbor_fn


def test_basic_expansion_surfaces_neighbor_with_provenance() -> None:
    nf = _graph({"seed": [("caller", "caller"), ("test", "test_of")]})
    res = expand_structural(["seed"], nf, config=ExpansionConfig(max_depth=1))
    ids = res.candidate_ids
    assert set(ids) == {"caller", "test"}
    c = next(c for c in res.candidates if c.id == "caller")
    assert c.source_hit == "seed" and c.relation == "caller" and c.depth == 1


def test_seeds_are_excluded_from_results() -> None:
    nf = _graph({"a": [("b", "caller")], "b": [("a", "callee")]})
    res = expand_structural(["a", "b"], nf, config=ExpansionConfig())
    assert "a" not in res.candidate_ids and "b" not in res.candidate_ids


def test_depth_limit_bounds_blast_radius() -> None:
    nf = _graph({"seed": [("d1", "caller")], "d1": [("d2", "caller")], "d2": [("d3", "caller")]})
    res = expand_structural(["seed"], nf, config=ExpansionConfig(max_depth=1, max_clones_per_hit=99))
    assert res.candidate_ids == ["d1"]  # depth 2+ not reached
    res2 = expand_structural(["seed"], nf, config=ExpansionConfig(max_depth=2, max_clones_per_hit=99))
    assert set(res2.candidate_ids) == {"d1", "d2"}


def test_per_hit_cap() -> None:
    nf = _graph({"seed": [(f"n{i}", "caller") for i in range(10)]})
    res = expand_structural(["seed"], nf, config=ExpansionConfig(max_clones_per_hit=3, max_depth=1))
    assert len(res.candidate_ids) == 3
    assert res.truncated is True


def test_total_cap_across_hits() -> None:
    nf = _graph({"s1": [(f"a{i}", "caller") for i in range(5)],
                 "s2": [(f"b{i}", "caller") for i in range(5)]})
    res = expand_structural(["s1", "s2"], nf, config=ExpansionConfig(max_clones_per_hit=5, max_total_clones=4, max_depth=1))
    assert len(res.candidate_ids) == 4
    assert res.truncated is True


def test_utility_suppression_skips_hub_nodes() -> None:
    nf = _graph({"seed": [("logger", "callee"), ("real", "caller")]})
    degree = {"logger": 500, "real": 3}
    res = expand_structural(
        ["seed"], nf, config=ExpansionConfig(max_depth=1, utility_degree_threshold=25),
        degree_fn=lambda n: degree.get(n, 0),
    )
    assert "logger" not in res.candidate_ids
    assert "real" in res.candidate_ids
    assert "logger" in res.skipped_utility


def test_dedup_neighbor_reached_from_two_paths() -> None:
    nf = _graph({"seed": [("a", "caller"), ("b", "caller")], "a": [("shared", "callee")], "b": [("shared", "callee")]})
    res = expand_structural(["seed"], nf, config=ExpansionConfig(max_depth=2, max_clones_per_hit=99))
    assert res.candidate_ids.count("shared") == 1


def test_deterministic() -> None:
    nf = _graph({"seed": [("x", "caller"), ("y", "test_of"), ("z", "imports")]})
    cfg = ExpansionConfig(max_depth=1)
    assert expand_structural(["seed"], nf, config=cfg).candidate_ids == expand_structural(["seed"], nf, config=cfg).candidate_ids
