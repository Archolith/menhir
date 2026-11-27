"""#871: one scan-one-node loop, a merge ends the scan, re-checks don't count or reverse edges.

Counterexamples against 4794b41f:
- ``check_correlation`` had no ``break`` after ``merged``: hits after the merge were classified
  for a node that no longer existed (phantom conflicts, a judge asked about an empty node).
- consolidation ran its own copy of the loop; ``check_correlation_batch`` was a third, test-only
  copy (deleted; its namespace/skip/error tests are carried here against ``scan_node``).
- ``create_related_to_edge`` returned True for an edge that already existed and MERGEd on the
  directed pattern, so the reverse check created a second edge.
- two 0.85 conflict-threshold literals.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from menhir.domain.namespace import namespace_to_group_ids
from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.services import correlation_service_checks as checks
from menhir.services.correlation_service import CorrelationService
from menhir.services.correlation_service_types import CORRELATION_CONFLICT_THRESHOLD
from menhir.services.lifecycle_consolidation import LifecycleConsolidationMixin

pytestmark = pytest.mark.unit


class _Classifier:
    """classify_pair stand-in: a fixed outcome per target uuid, recording every call."""

    def __init__(self, outcomes: dict[str, tuple[str, dict[str, Any] | None]]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, source: str, target: str, score: float):
        self.calls.append((source, target))
        return self.outcomes.get(target, ("none", None))


def _client(hits: list[tuple[str, str, float]]) -> Any:
    client = MagicMock()
    client.search_scored = AsyncMock(return_value=hits)
    return client


HITS = [("x", "X", 0.97), ("y", "Y", 0.90), ("z", "Z", 0.97)]


# --------------------------------------------------------------------------- merge ends the scan

@pytest.mark.asyncio
async def test_check_correlation_stops_after_the_node_is_merged_away() -> None:
    svc = CorrelationService(MagicMock(), _client(HITS))
    classify = _Classifier({
        "x": ("merged", {"merged": 1}),
        "y": ("conflict", {"reason": "strong_overlap_pending_review"}),
        "z": ("merged", {"merged": 1}),
    })
    svc.classify_pair = classify  # type: ignore[method-assign]

    result = await svc.check_correlation("src", "query", namespace="default")

    assert classify.calls == [("src", "x")], "no hit after the merge may be classified"
    assert (result.merged, result.conflicts) == (1, 0)
    assert [(r.action, r.target_uuid) for r in result.results or []] == [("merged", "x")]


@pytest.mark.asyncio
async def test_conflicts_before_the_merge_are_kept_and_later_hits_are_not() -> None:
    svc = CorrelationService(MagicMock(), _client(HITS))
    classify = _Classifier({
        "x": ("conflict", {"reason": "strong_overlap_pending_review"}),
        "y": ("merged", {"merged": 1}),
        "z": ("conflict", {"reason": "strong_overlap_pending_review"}),
    })
    svc.classify_pair = classify  # type: ignore[method-assign]

    result = await svc.check_correlation("src", "query")

    assert classify.calls == [("src", "x"), ("src", "y")]
    assert [r.action for r in result.results or []] == ["conflict", "merged"]


@pytest.mark.asyncio
async def test_without_a_handler_every_conflict_is_collected() -> None:
    """Enrichment queues the first conflict that writes, so later ones are fallbacks."""
    svc = CorrelationService(MagicMock(), _client(HITS))
    classify = _Classifier({t: ("conflict", {"reason": "r"}) for t in ("x", "y", "z")})
    svc.classify_pair = classify  # type: ignore[method-assign]

    result = await svc.check_correlation("src", "query")

    assert result.conflicts == 3 and len(classify.calls) == 3


# --------------------------------------------------------------------------- one loop

def test_the_batch_copy_is_gone() -> None:
    assert not hasattr(CorrelationService, "check_correlation_batch")


class _Host(LifecycleConsolidationMixin):
    def __init__(self, hits) -> None:
        self.graph_adapter = MagicMock()
        self.graphiti_client = _client(hits)
        self.llm = None
        self.settings = SimpleNamespace(conflict_cooldown_days=30)


@pytest.mark.asyncio
async def test_both_live_callers_run_the_shared_loop() -> None:
    seen: list[dict[str, Any]] = []
    real = checks.scan_node

    async def _spy(*args, **kwargs):
        seen.append(kwargs)
        return await real(*args, **kwargs)

    host = _Host([])
    with patch.object(checks, "scan_node", _spy):
        await CorrelationService(MagicMock(), _client([])).check_correlation(
            "n1", "q", namespace="tenant-a",
        )
        await host._check_contradictions_batch([{"uuid": "n2", "name": "q", "namespace": "tenant-b"}])

    assert [s["group_ids"] for s in seen] == [
        namespace_to_group_ids("tenant-a"), namespace_to_group_ids("tenant-b"),
    ]
    assert [s["stop_on_drop"] for s in seen] == [False, True]


@pytest.mark.asyncio
async def test_consolidation_stops_after_a_merge_too() -> None:
    host = _Host(HITS)
    classify = _Classifier({"x": ("merged", {"merged": 1}), "y": ("conflict", {"reason": "r"})})
    host._build_correlation_service = lambda: SimpleNamespace(classify_pair=classify)  # type: ignore[method-assign]

    assert await host._check_contradictions_batch([{"uuid": "src", "name": "q"}]) == 0
    assert classify.calls == [("src", "x")]
    host.graph_adapter.set_conflict.assert_not_called()


# --------------------------------------------------------------------------- stop rules

@pytest.mark.asyncio
@pytest.mark.parametrize("stop_on_drop, expected_calls", [(True, 1), (False, 3)])
async def test_a_drop_ends_the_scan_only_when_asked(stop_on_drop, expected_calls) -> None:
    """Carried from test_conflict_detection_signal.test_batch_check_stops_at_a_drop_too."""
    classify = _Classifier({"x": ("none", {"reason": "below_similarity_floor"})})
    result = await checks.scan_node(
        classify, _client(HITS), "src", "q",
        group_ids=None, num_results=5, stop_on_drop=stop_on_drop,
    )
    assert len(classify.calls) == expected_calls
    assert (result.conflicts, result.related, result.merged) == (0, 0, 0)


@pytest.mark.asyncio
async def test_conflict_handler_decides_whether_the_scan_goes_on() -> None:
    classify = _Classifier({t: ("conflict", {"reason": "r"}) for t in ("x", "y", "z")})
    answers = iter([False, True])

    async def _handler(cr) -> bool:
        return next(answers)

    await checks.scan_node(
        classify, _client(HITS), "src", "q",
        group_ids=None, num_results=5, stop_on_drop=True, on_conflict=_handler,
    )
    assert classify.calls == [("src", "x"), ("src", "y")]


@pytest.mark.asyncio
async def test_related_counts_only_a_created_edge() -> None:
    classify = _Classifier({"x": ("related", {"created": False}), "y": ("related", {"created": True})})
    result = await checks.scan_node(
        classify, _client(HITS), "src", "q", group_ids=None, num_results=5, stop_on_drop=False,
    )
    assert result.related == 1


# --------------------------------------------------------------------------- carried from CF-63

@pytest.mark.asyncio
async def test_group_ids_reach_the_search_as_given() -> None:
    client = _client([])
    await checks.scan_node(
        _Classifier({}), client, "u-1", "text",
        group_ids=namespace_to_group_ids("tenant-a"), num_results=5, stop_on_drop=True,
    )
    kwargs = client.search_scored.await_args.kwargs
    assert kwargs == {"num_results": 5, "group_ids": namespace_to_group_ids("tenant-a")}


@pytest.mark.asyncio
async def test_no_group_ids_searches_unscoped() -> None:
    """DOCUMENTED RESIDUAL (CF-63): an unresolvable namespace searches every group."""
    client = _client([])
    await CorrelationService(MagicMock(), client).check_correlation("u-1", "text")
    assert "group_ids" not in client.search_scored.await_args.kwargs


@pytest.mark.asyncio
async def test_empty_query_skips_without_searching() -> None:
    client = _client([])
    result = await checks.scan_node(
        _Classifier({}), client, "u-1", "  ", group_ids=None, num_results=5, stop_on_drop=True,
    )
    assert (result.checked, result.skipped) == (1, 1)
    client.search_scored.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failing_search_is_skipped_and_reported_to_the_error_list() -> None:
    client = MagicMock()
    client.search_scored = AsyncMock(side_effect=RuntimeError("search exploded"))
    errors: list[str] = []
    result = await checks.scan_node(
        _Classifier({}), client, "u-1", "text",
        group_ids=None, num_results=5, stop_on_drop=True, errors=errors,
    )
    assert result.skipped == 1 and errors == ["u-1"]


# --------------------------------------------------------------------------- RELATES_TO edge

def _edge_repo(rows):
    neo4j = MagicMock()
    neo4j.execute.return_value = rows
    return CorrelationRepository(neo4j), neo4j


@pytest.mark.parametrize("pair", [("uuid-a", "uuid-b"), ("uuid-b", "uuid-a")])
def test_the_pair_is_ordered_so_a_reverse_check_matches_the_same_edge(pair) -> None:
    repo, neo4j = _edge_repo([{"edge_type": "correlation"}])
    repo.create_related_to_edge(*pair, similarity=0.75)
    params = neo4j.execute.call_args.kwargs["params"]
    assert (params["low_uuid"], params["high_uuid"]) == ("uuid-a", "uuid-b")


def test_the_edge_query_matches_either_direction_and_creates_only_when_absent() -> None:
    repo, neo4j = _edge_repo([])
    assert repo.create_related_to_edge("uuid-1", "uuid-2", similarity=0.75) is False
    cypher = neo4j.execute.call_args.args[0]
    assert "[(a)-[e:RELATES_TO]-(b) WHERE e.type = 'correlation' | e]" in cypher, (
        "an existing correlation edge either way round must match"
    )
    # The directed MERGE is reached only when no edge exists either way round.
    assert cypher.index("WHERE size(existing) = 0") < cypher.index("MERGE (a)-[r:RELATES_TO {type: 'correlation'}]->(b)")


@pytest.mark.asyncio
async def test_classify_pair_reports_a_matched_edge_as_not_created() -> None:
    repo = MagicMock()
    repo.create_related_to_edge.return_value = False
    svc = CorrelationService(repo, _client([]))
    action, details = await svc.classify_pair("s", "t", 0.75)
    assert (action, details) == ("related", {"created": False})


# --------------------------------------------------------------------------- one threshold

def test_one_conflict_threshold() -> None:
    from menhir.services import lifecycle_models

    assert lifecycle_models.SIMILARITY_CONFLICT_THRESHOLD is CORRELATION_CONFLICT_THRESHOLD
