"""CF-175: the BM25 and cosine lanes of `search_ranked_by_method` run concurrently.

Each lane is stubbed with a fixed delay; two lanes finishing in roughly one delay proves overlap,
and the failure-isolation tests pin that one failing lane leaves the other's hits intact.
"""

from __future__ import annotations

import asyncio

import pytest

from menhir.infrastructure.graphiti_client import GraphitiClient


class _LaneClient:
    """A GraphitiClient stub whose two lanes each sleep, so serialization is visible in the clock."""

    def __init__(self, delay: float = 0.1) -> None:
        self.delay = delay
        self.driver = object()


def _lane_wrapper(monkeypatch: pytest.MonkeyPatch, *, delay: float = 0.1) -> GraphitiClient:
    wrapper = GraphitiClient.__new__(GraphitiClient)
    wrapper.client = type("C", (), {"driver": object()})()

    async def _no_wake(*, task: str) -> None:
        return None

    async def _embed(query: str) -> list[float]:
        await asyncio.sleep(delay / 2)
        return [0.1, 0.2]

    monkeypatch.setattr(wrapper, "embed_query", _embed)
    return wrapper


def _stub_lanes(monkeypatch: pytest.MonkeyPatch, *, delay: float = 0.1) -> dict[str, object]:
    """Patch graphiti_core's two search helpers, which the method imports at call time."""
    from graphiti_core.search import search_utils

    seen: dict[str, object] = {"calls": []}

    class _Node:
        def __init__(self, uuid: str, name: str) -> None:
            self.uuid = uuid
            self.name = name
            self.labels = ["Entity"]

    async def _bm25(driver, query, filters, group_ids, limit):
        seen["calls"].append("bm25")
        await asyncio.sleep(delay)
        return [_Node("b1", "bm25-hit")]

    async def _cosine(driver, vector, filters, group_ids, limit):
        seen["calls"].append("cosine")
        await asyncio.sleep(delay / 2)
        return [_Node("c1", "cosine-hit")]

    monkeypatch.setattr(search_utils, "node_fulltext_search", _bm25)
    monkeypatch.setattr(search_utils, "node_similarity_search", _cosine)
    return seen


@pytest.mark.asyncio
async def test_bm25_and_cosine_lanes_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE FINDING. The lanes ran in series. Each lane now waits, while running, for the other
    to start; in series the first lane would time out. No wall-clock budget (#605)."""
    from graphiti_core.search import search_utils

    _stub_lanes(monkeypatch)
    wrapper = _lane_wrapper(monkeypatch)
    started = {"bm25": asyncio.Event(), "cosine": asyncio.Event()}
    saw_other: dict[str, bool] = {}
    bm25, cosine = search_utils.node_fulltext_search, search_utils.node_similarity_search

    def _meeting(name: str, other: str, inner):
        async def lane(*args, **kwargs):
            started[name].set()
            try:
                await asyncio.wait_for(started[other].wait(), timeout=10)
                saw_other[name] = True
            except asyncio.TimeoutError:
                saw_other[name] = False
            return await inner(*args, **kwargs)
        return lane

    monkeypatch.setattr(search_utils, "node_fulltext_search", _meeting("bm25", "cosine", bm25))
    monkeypatch.setattr(
        search_utils, "node_similarity_search", _meeting("cosine", "bm25", cosine)
    )

    ranked = await wrapper.search_ranked_by_method(
        "q", methods=["bm25", "cosine_similarity"], num_results=5
    )

    assert set(ranked) == {"bm25", "cosine_similarity"}
    assert saw_other == {"bm25": True, "cosine": True}, f"lanes ran in series: {saw_other}"


@pytest.mark.asyncio
async def test_both_lanes_still_return_their_own_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    """POSITIVE CONTROL. A version that dropped a lane, or let one lane's result overwrite the
    other's key, would pass the timing test above."""
    _stub_lanes(monkeypatch)
    wrapper = _lane_wrapper(monkeypatch)

    ranked = await wrapper.search_ranked_by_method(
        "q", methods=["bm25", "cosine_similarity"], num_results=5
    )

    assert ranked["bm25"] == [("b1", "bm25-hit")]
    assert ranked["cosine_similarity"] == [("c1", "cosine-hit")]


@pytest.mark.asyncio
async def test_one_failing_lane_leaves_the_other_intact(monkeypatch: pytest.MonkeyPatch) -> None:
    """The docstring promises "other retrieval lanes will continue". Under `gather` that needs the
    handler INSIDE the lane; a bare `gather` cancels siblings on the first exception."""
    from graphiti_core.search import search_utils

    class _Node:
        def __init__(self) -> None:
            self.uuid = "b1"
            self.name = "bm25-hit"
            self.labels = ["Entity"]

    async def _bm25(driver, query, filters, group_ids, limit):
        await asyncio.sleep(0.02)
        return [_Node()]

    async def _cosine(driver, vector, filters, group_ids, limit):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(search_utils, "node_fulltext_search", _bm25)
    monkeypatch.setattr(search_utils, "node_similarity_search", _cosine)

    wrapper = _lane_wrapper(monkeypatch)
    ranked = await wrapper.search_ranked_by_method(
        "q", methods=["bm25", "cosine_similarity"], num_results=5
    )

    assert ranked["bm25"] == [("b1", "bm25-hit")]
    assert ranked["cosine_similarity"] == []


@pytest.mark.asyncio
async def test_all_lanes_failing_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """The method's contract distinguishes "one lane degraded" from "retrieval is down"; swallowing
    both into an empty dict would make a total outage look like zero results."""
    from graphiti_core.search import search_utils

    async def _boom(*a, **kw):
        raise RuntimeError("down")

    monkeypatch.setattr(search_utils, "node_fulltext_search", _boom)
    monkeypatch.setattr(search_utils, "node_similarity_search", _boom)

    wrapper = _lane_wrapper(monkeypatch)
    with pytest.raises(RuntimeError, match="all requested Graphiti search lanes failed"):
        await wrapper.search_ranked_by_method(
            "q", methods=["bm25", "cosine_similarity"], num_results=5
        )


@pytest.mark.asyncio
async def test_an_unsupported_method_is_rejected_before_any_lane_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DELIBERATE, DOCUMENTED CHANGE. Serially the ValueError landed only after every earlier
    lane had already queried Neo4j. Concurrently there is no "earlier", so the check has to be
    hoisted -- and hoisting it is the stricter behaviour: no work is done for a request that was
    always going to be rejected."""
    seen = _stub_lanes(monkeypatch)
    wrapper = _lane_wrapper(monkeypatch)

    with pytest.raises(ValueError, match="Unsupported search method"):
        await wrapper.search_ranked_by_method(
            "q", methods=["bm25", "nonsense"], num_results=5
        )

    assert seen["calls"] == [], "bm25 ran before the unsupported method was rejected"
