"""Relevance-ranked attached facts (MENHIR_FRONTIER_FACT_RELEVANCE): ranking, query, wiring."""

from __future__ import annotations

import pytest

from menhir.config.feature_flags import FEATURES
from menhir.config.settings_model import MemorySettings
from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.services.recall_policies import _build_temporal_facts
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService
from tests.test_scalar_history_recall import _setup_search_and_metadata


def _rows(similarities, node="n1"):
    """Eight rows; index 0 is the oldest. similarities maps index -> fact_similarity."""
    out = []
    for i in range(8):
        row = {
            "node_uuid": node, "fact": f"fact-{i}", "valid_at": None, "invalid_at": None,
            "created_at": f"2025-01-{i + 1:02d}T00:00:00Z", "expired_at": None,
        }
        if i in similarities:
            row["fact_similarity"] = similarities[i]
        out.append(row)
    return out


def _facts(result, node="n1"):
    return [f.fact for f in result[node]]


@pytest.mark.unit
def test_relevance_k_zero_is_newest_five():
    result = _build_temporal_facts(_rows({0: 0.99, 1: 0.98, 2: 0.97}))
    assert _facts(result) == ["fact-7", "fact-6", "fact-5", "fact-4", "fact-3"]


@pytest.mark.unit
def test_relevance_picks_oldest_high_similarity_plus_newest_ordered_newest_first():
    result = _build_temporal_facts(
        _rows({0: 0.99, 1: 0.98, 2: 0.97, 5: 0.1}), relevance_k=3,
    )
    assert _facts(result) == ["fact-7", "fact-6", "fact-2", "fact-1", "fact-0"]
    assert len(result["n1"]) == 5


@pytest.mark.unit
def test_none_similarity_never_takes_a_relevance_slot():
    result = _build_temporal_facts(_rows({0: 0.9}), relevance_k=3)
    assert _facts(result) == ["fact-7", "fact-6", "fact-5", "fact-4", "fact-0"]
    result = _build_temporal_facts(
        _rows({0: 0.9, 1: None, 2: float("nan"), 3: float("inf")}), relevance_k=3,
    )
    assert "fact-1" not in _facts(result) and "fact-2" not in _facts(result)
    assert "fact-3" not in _facts(result) and "fact-0" in _facts(result)


@pytest.mark.unit
def test_similarity_tie_breaks_to_newest():
    result = _build_temporal_facts(_rows({0: 0.5, 1: 0.5, 2: 0.5, 3: 0.5}), relevance_k=1)
    assert "fact-3" in _facts(result) and "fact-0" not in _facts(result)


class _FakeNeo4j:
    def __init__(self):
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((query, params))
        return []


@pytest.mark.unit
def test_fetch_temporal_facts_query_vector_adds_similarity_field_and_param():
    neo = _FakeNeo4j()
    repo = MemoryQueryRepository(neo)
    repo.fetch_temporal_facts(["a"])
    query, params = neo.calls[-1]
    assert "fact_similarity" not in query
    assert "query_vector" not in params
    repo.fetch_temporal_facts(["a"], query_vector=[0.1, 0.2])
    query, params = neo.calls[-1]
    assert "fact_similarity" in query
    assert "size(r.fact_embedding) = size($query_vector)" in query
    assert params["query_vector"] == [0.1, 0.2]


@pytest.mark.unit
def test_fact_relevance_flag_registered_and_reaches_tuning(monkeypatch):
    feature = FEATURES["frontier_fact_relevance"]
    assert feature.env_var == "MENHIR_FRONTIER_FACT_RELEVANCE"
    assert feature.default is False
    assert RetrievalTuningConfig().enable_fact_relevance is False
    monkeypatch.setenv("MENHIR_FRONTIER_FACT_RELEVANCE", "1")
    settings = MemorySettings.from_env()
    assert settings.frontier_fact_relevance is True
    assert settings.retrieval_tuning().enable_fact_relevance is True


def _fact_rows():
    return [
        {"node_uuid": "entity-1", "fact": f"fact-{i}", "valid_at": None, "invalid_at": None,
         "created_at": f"2025-01-{i + 1:02d}T00:00:00Z", "expired_at": None,
         "fact_similarity": 0.95 if i == 0 else 0.1}
        for i in range(8)
    ]


def _svc(client, adapter):
    return RecallService(graphiti_client=client, graph_adapter=adapter,
                         scoring_service=ScoringService())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pipeline_flag_on_passes_query_vector_and_keeps_old_relevant_fact(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    seen = []

    def fake(node_uuids, *, query_vector=None):
        seen.append(query_vector)
        return _fact_rows()

    stub_memory_graph_adapter.fetch_temporal_facts = fake
    res = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall(
        "q", tuning=RetrievalTuningConfig(enable_fact_relevance=True))
    assert seen and seen[-1] == stub_graphiti_client.embed_query_result
    facts = {f.fact for r in res.results if r.uuid == "entity-1" for f in r.temporal_facts}
    assert "fact-0" in facts


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pipeline_flag_off_passes_no_query_vector(
    stub_graphiti_client, stub_memory_graph_adapter,
):
    _setup_search_and_metadata(stub_graphiti_client, stub_memory_graph_adapter)
    kwargs_seen = []

    def fake(node_uuids, **kwargs):
        kwargs_seen.append(kwargs)
        return _fact_rows()

    stub_memory_graph_adapter.fetch_temporal_facts = fake
    res = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall("q")
    assert kwargs_seen and all(k == {} for k in kwargs_seen)
    facts = {f.fact for r in res.results if r.uuid == "entity-1" for f in r.temporal_facts}
    assert "fact-0" not in facts
