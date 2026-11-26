"""Input bounds for probe_view_reachability (issue #966).

max_views must be 1..200 and top_k must be 1..50. Out-of-range values raise ValueError
before any View listing or recall runs, so a rejected call performs no recall.
"""

from __future__ import annotations

import pytest

from menhir.services.view_entropy import probe_view_reachability


class _RecordingRecall:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def recall(self, query: str, **kwargs: object) -> object:
        self.calls.append({"query": query, **kwargs})
        raise AssertionError("recall must not be called for rejected input")


class _RecordingViews:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def list_views(self, *, kind=None, namespace=None, limit=100):
        self.calls.append({"limit": limit})
        return [{"uuid": "view-1", "name": "surface", "namespace": ""}]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_views": 0},
        {"max_views": -1},
        {"top_k": 0},
        {"top_k": -5},
    ],
)
async def test_below_minimum_is_rejected_without_recall(kwargs: dict[str, int]) -> None:
    recall = _RecordingRecall()
    views = _RecordingViews()

    with pytest.raises(ValueError):
        await probe_view_reachability(recall_service=recall, graph_adapter=views, **kwargs)

    assert recall.calls == []
    assert views.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_views": 201},
        {"max_views": 10_000},
        {"top_k": 51},
        {"top_k": 1_000},
    ],
)
async def test_above_maximum_is_rejected_without_recall(kwargs: dict[str, int]) -> None:
    recall = _RecordingRecall()
    views = _RecordingViews()

    with pytest.raises(ValueError):
        await probe_view_reachability(recall_service=recall, graph_adapter=views, **kwargs)

    assert recall.calls == []
    assert views.calls == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_views": 1, "top_k": 1},
        {"max_views": 200, "top_k": 50},
    ],
)
async def test_bounds_are_inclusive(kwargs: dict[str, int]) -> None:
    class _Recall:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def recall(self, query: str, **kw: object) -> object:
            self.calls.append(kw)
            from menhir.domain.recall import RecallResult

            return RecallResult(
                query=query, preset="knowledge", results=[],
                candidates_evaluated=0, nodes_touched=0,
            )

    recall = _Recall()
    views = _RecordingViews()

    out = await probe_view_reachability(recall_service=recall, graph_adapter=views, **kwargs)

    assert views.calls[0]["limit"] == kwargs["max_views"]
    assert recall.calls[0]["limit"] == kwargs["top_k"]
    assert out["summary"]["views_probed"] == 1


async def test_tool_endpoint_maps_out_of_range_to_invalid_argument():
    from menhir.mcp.telemetry.tracker import McpToolRefusal
    from menhir.mcp.tools.ops.view_entropy import ViewEntropyTool

    class _Backend:
        async def view_entropy(self, **kwargs):
            raise ValueError("max_views must be between 1 and 200 (got 0)")

    tool = ViewEntropyTool()
    tool.get_backend = lambda: _Backend()
    with pytest.raises(McpToolRefusal) as excinfo:
        await tool.endpoint(max_views=0)
    assert getattr(excinfo.value, "code", None) == "invalid_argument"


@pytest.mark.parametrize("bad", ["5", 10.5, True, None])
async def test_non_integer_bounds_are_rejected(bad):
    with pytest.raises(ValueError, match="must be an integer"):
        await probe_view_reachability(
            graph_adapter=object(), recall_service=_RecordingRecall(), max_views=bad, top_k=5,
        )
