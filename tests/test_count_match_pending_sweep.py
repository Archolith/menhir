"""4R: a namespace with no new turns still gets its undecided and re-decide rows swept. Before, the
scheduler only listed dirty namespaces, so those rows waited for the next user turn."""

from __future__ import annotations

import pytest

from menhir.domain.count_decide import (
    DECIDE_K,
    MAX_DECIDE_ATTEMPTS,
    REDECIDE_COOLDOWN_S,
)
from menhir.services.counting_consolidation import (
    CountingConsolidationConfig,
    run_counting_consolidation,
)
from tests.test_count_match_runner import _build_episodes, _Graph, _Llm, _Perception


@pytest.fixture
def perception(monkeypatch):
    import menhir.services.typed_scalar_perception as tsp

    _Perception.instances = []
    _Perception.results = []
    monkeypatch.setattr(tsp, "TypedScalarPerceptionService", _Perception)
    return _Perception


class _PendingGraph(_Graph):
    def __init__(self, *, dirty=(), pending=(), **kw):
        super().__init__(**kw)
        self.dirty = list(dirty)
        self.pending = list(pending)

    def list_counting_dirty_namespaces(self, **kw):
        self._log("list_counting_dirty_namespaces", **kw)
        return list(self.dirty)

    def pending_decision_namespaces(self, **kw):
        self._log("pending_decision_namespaces", **kw)
        return list(self.pending)


def _run(graph, *, match=True, namespaces=None, max_namespaces=50, call_budget=None):
    return run_counting_consolidation(
        graph, namespaces=namespaces, counting_llm=_Llm(), build_episodes=_build_episodes,
        k=1, threshold=1.0, call_budget=call_budget, embed=None,
        config=CountingConsolidationConfig(
            perceiver_version="cv1", scalar_perceiver_version="sv1", batch_size=500,
            match=match, max_namespaces=max_namespaces))


def _claimed(graph):
    return [c[1][0] for c in graph.of("claim_counting_namespace")]


@pytest.mark.unit
def test_quiet_namespace_with_pending_rows_is_swept_without_reading_pages(perception):
    graph = _PendingGraph(pending=["ns-quiet"])
    out = _run(graph)
    assert _claimed(graph) == ["ns-quiet"]
    assert [c[1][0] for c in graph.of("undecided_count_occurrences")] == ["ns-quiet"]
    assert [c[1][0] for c in graph.of("redecide_count_occurrences")] == ["ns-quiet"]
    assert graph.of("advance_counting_cursor") == []
    assert perception.instances[0].persisted == []
    assert out["counting_namespaces_processed"] == 0
    call, = graph.of("pending_decision_namespaces")
    assert call[2] == {"perceiver_version": "cv1+m", "max_attempts": MAX_DECIDE_ATTEMPTS,
                       "limit": 50, "cooldown_s": REDECIDE_COOLDOWN_S}


@pytest.mark.unit
def test_dirty_namespaces_come_first_deduplicated_and_capped(perception):
    graph = _PendingGraph(dirty=["a", "b"], pending=["b", "c", "d"])
    _run(graph, max_namespaces=3)
    assert _claimed(graph) == ["a", "b", "c"]


@pytest.mark.unit
def test_full_dirty_list_does_not_query_pending(perception):
    graph = _PendingGraph(dirty=["a", "b"], pending=["c"])
    _run(graph, max_namespaces=2)
    assert graph.of("pending_decision_namespaces") == []
    assert _claimed(graph) == ["a", "b"]


def _redecide_kwargs(graph):
    return {c[1][0]: c[2] for c in graph.of("redecide_count_occurrences")}


@pytest.mark.unit
def test_only_a_pending_only_namespace_re_decides_under_the_cooldown(perception):
    # Fails if a quiet namespace re-decides every tick (each tick spends one of the row's
    # MAX_DECIDE_ATTEMPTS on unchanged input), or if a dirty namespace waits out the cooldown.
    graph = _PendingGraph(dirty=["d"], pending=["d", "q"])
    _run(graph)
    got = _redecide_kwargs(graph)
    assert got["q"]["cooldown_s"] == REDECIDE_COOLDOWN_S
    assert "cooldown_s" not in got["d"]


@pytest.mark.unit
def test_explicit_namespaces_re_decide_without_the_cooldown(perception):
    graph = _PendingGraph(pending=["ns-1"])
    _run(graph, namespaces=["ns-1"])
    assert "cooldown_s" not in _redecide_kwargs(graph)["ns-1"]


class _FailingPendingGraph(_PendingGraph):
    def pending_decision_namespaces(self, **kw):
        self._log("pending_decision_namespaces", **kw)
        raise RuntimeError("listing timed out")


@pytest.mark.unit
def test_failed_pending_listing_still_runs_the_dirty_namespaces(perception):
    # Fails if the optional listing's error escapes and stops the whole counting lane.
    graph = _FailingPendingGraph(dirty=["d"])
    out = _run(graph)
    assert _claimed(graph) == ["d"]
    assert out["counting_namespaces_failed"] == 0


@pytest.mark.unit
def test_pending_only_namespace_is_skipped_when_budget_cannot_cover_one_decide(perception):
    # Fails if a quiet namespace is claimed (and every candidate embedded) with fewer than DECIDE_K
    # calls left, which can decide nothing and repeats every tick. A dirty one still runs.
    graph = _PendingGraph(dirty=["d"], pending=["q"])
    _run(graph, call_budget=DECIDE_K - 1)
    assert _claimed(graph) == ["d"]
    graph = _PendingGraph(dirty=["d"], pending=["q"])
    _run(graph, call_budget=DECIDE_K)
    assert _claimed(graph) == ["d", "q"]


@pytest.mark.unit
@pytest.mark.parametrize("kw", [{"match": False}, {"namespaces": ["ns-1"]}])
def test_match_off_or_explicit_namespaces_never_list_pending(perception, kw):
    graph = _PendingGraph(pending=["ns-quiet"])
    _run(graph, **kw)
    assert graph.of("pending_decision_namespaces") == []
    assert "ns-quiet" not in _claimed(graph)
