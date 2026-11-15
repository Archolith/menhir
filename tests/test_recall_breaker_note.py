"""#632: when the embed breaker is open, recall's note says why and that results are keyword-only."""

from __future__ import annotations

import httpx
import openai
import pytest

from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.infrastructure import circuit_breaker as cb
from menhir.infrastructure.circuit_breaker import CircuitBreaker
from menhir.services.recall_pipeline import embed_breaker_note
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService
from tests.test_source_memory_lane import _meta

pytestmark = pytest.mark.unit

_REQ = httpx.Request("POST", "https://api.openai.com/v1/embeddings")


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    now = [1000.0]
    monkeypatch.setattr(cb, "monotonic", lambda: now[0])
    return now


async def _billing_open_breaker() -> CircuitBreaker:
    breaker = CircuitBreaker(name="embed:x")

    async def refuse():
        raise openai.RateLimitError(
            "insufficient_quota",
            response=httpx.Response(429, request=_REQ),
            body={"code": "insufficient_quota", "type": "insufficient_quota", "message": "x"},
        )

    with pytest.raises(Exception):
        await breaker.call(refuse)
    return breaker


def _wire(stub, breaker: CircuitBreaker) -> None:
    async def embed_query(text):
        return await breaker.call(lambda: None)  # breaker is open: raises CircuitOpenError

    stub.embed_query = embed_query
    stub.circuit_breaker_snapshots = lambda: {"embed": breaker.state_snapshot()}


def _svc(stub, adapter) -> RecallService:
    return RecallService(graphiti_client=stub, graph_adapter=adapter, scoring_service=ScoringService())


async def test_note_names_billing_and_keyword_only_with_results(
    clock, stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    breaker = await _billing_open_breaker()
    clock[0] += 120
    _wire(stub_graphiti_client, breaker)
    stub_graphiti_client.search_scored_results = [("entity-1", "Entity One", 0.9)]
    stub_memory_graph_adapter.candidate_metadata = [_meta("entity-1", "Entity One")]

    result = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall(
        "query", tuning=RetrievalTuningConfig(enable_source_memories=True), include_session=True
    )

    assert result.results
    remaining_m = -(-int(cb.QUOTA_COOLDOWN_SECONDS - 120) // 60)
    assert (
        f"semantic search off: embedding provider refused for billing (next probe in {remaining_m}m); "
        "results are keyword-only"
    ) in (result.note or "")


async def test_note_present_on_the_empty_result_path(
    clock, stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    _wire(stub_graphiti_client, await _billing_open_breaker())
    stub_graphiti_client.search_scored_results = []
    stub_memory_graph_adapter.candidate_metadata = []

    result = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall(
        "query", tuning=RetrievalTuningConfig(), include_session=True
    )

    assert result.results == []
    assert "refused for billing" in (result.note or "") and "keyword-only" in (result.note or "")


async def test_no_note_when_breaker_closed(stub_graphiti_client, stub_memory_graph_adapter) -> None:
    breaker = CircuitBreaker(name="embed:x")
    stub_graphiti_client.circuit_breaker_snapshots = lambda: {"embed": breaker.state_snapshot()}
    stub_graphiti_client.search_scored_results = [("entity-1", "Entity One", 0.9)]
    stub_memory_graph_adapter.candidate_metadata = [_meta("entity-1", "Entity One")]

    result = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall(
        "query", tuning=RetrievalTuningConfig(), include_session=True
    )

    assert "semantic search off" not in (result.note or "")


@pytest.mark.parametrize(
    ("snap", "expected"),
    [
        ({"state": "closed"}, None),
        (
            {"state": "open", "cooldown_remaining_s": 25.0},
            "semantic search off: embedding provider unavailable (next probe in 1m); results are keyword-only",
        ),
        (
            {"state": "half_open", "reason": "provider_quota_exhausted", "cooldown_remaining_s": 0.0},
            "semantic search off: embedding provider refused for billing (probe due now); results are keyword-only",
        ),
    ],
)
def test_note_wording(snap, expected) -> None:
    class _Client:
        def circuit_breaker_snapshots(self):
            return {"embed": snap}

    assert embed_breaker_note(_Client()) == expected


def test_unreadable_snapshot_gives_no_note() -> None:
    class _Broken:
        def circuit_breaker_snapshots(self):
            raise RuntimeError("x")

    assert embed_breaker_note(_Broken()) is None
    assert embed_breaker_note(object()) is None
