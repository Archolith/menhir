"""Unit tests for top-1 source-memory session-window expansion (#1057)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from menhir.config.settings_model import MemorySettings
from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.services.recall_pipeline import _build_source_memories


def test_tuning_config_defaults_and_bounds():
    cfg = RetrievalTuningConfig()
    assert cfg.source_memory_session_window == 0
    assert cfg.source_memory_neighbor_max_chars == 800

    valid = RetrievalTuningConfig(source_memory_session_window=2, source_memory_neighbor_max_chars=600)
    assert valid.source_memory_session_window == 2
    assert valid.source_memory_neighbor_max_chars == 600

    with pytest.raises(ValueError, match="source_memory_session_window"):
        RetrievalTuningConfig(source_memory_session_window=-1)
    with pytest.raises(ValueError, match="source_memory_session_window"):
        RetrievalTuningConfig(source_memory_session_window=6)
    with pytest.raises(ValueError, match="source_memory_neighbor_max_chars"):
        RetrievalTuningConfig(source_memory_neighbor_max_chars=50)


def test_settings_env_parsing_and_tuning_wiring(monkeypatch):
    monkeypatch.setenv("MENHIR_FRONTIER_SOURCE_MEMORY_SESSION_WINDOW", "2")
    monkeypatch.setenv("MENHIR_FRONTIER_SOURCE_MEMORY_NEIGHBOR_MAX_CHARS", "650")
    s = MemorySettings.from_env()
    assert s.frontier_source_memory_session_window == 2
    assert s.frontier_source_memory_neighbor_max_chars == 650
    t = s.retrieval_tuning()
    assert t.source_memory_session_window == 2
    assert t.source_memory_neighbor_max_chars == 650


def test_get_session_episode_neighbors_cypher_is_bounded_and_scoped():
    neo4j = MagicMock()
    neo4j.execute.return_value = [{"uuid": "nb-1", "content": "Target app", "session_id": "s43", "reference_time": "2023-05-29T13:28:00Z", "cosine": 0.0, "source": "user"}]
    q = MemoryQueryRepository(neo4j=neo4j)

    rows = q.get_session_episode_neighbors(
        "s43", "anchor-1", window=2, namespace="lme-51a45a95", sources=("user",)
    )
    assert len(rows) == 1
    assert neo4j.execute.call_count == 1
    cypher, kwargs = neo4j.execute.call_args[0][0], neo4j.execute.call_args[1]
    assert cypher.count("LIMIT $window") == 2
    assert "n.processing_state <> 'FAILED'" in cypher
    assert "n.session_id = $session_id" in cypher
    assert "n.source IN $sources" in cypher
    assert kwargs["params"]["window"] == 2
    assert kwargs["params"]["session_id"] == "s43"
    assert kwargs["params"]["anchor_uuid"] == "anchor-1"
    assert kwargs["params"]["sources"] == ["user"]

    # Guard: window=0 or empty session_id short-circuits without querying Neo4j.
    neo4j.reset_mock()
    assert q.get_session_episode_neighbors("s43", "anchor-1", window=0) == []
    assert q.get_session_episode_neighbors("", "anchor-1", window=2) == []
    assert neo4j.execute.call_count == 0


@pytest.mark.asyncio
async def test_build_source_memories_expands_top1_when_starved_and_caps_neighbors():
    # Two distinct sessions filling effective_limit=2 -> spread starves rest.
    search_rows = [
        {"uuid": "u-top", "content": "Redeemed $5 coffee creamer coupon", "session_id": "s43", "reference_time": "2023-05-29T13:28:00Z", "cosine": 0.91, "source": "user"},
        {"uuid": "u-other", "content": "Coffee table remotes", "session_id": "s12", "reference_time": "2023-05-24T09:33:00Z", "cosine": 0.72, "source": "user"},
    ]
    neighbor_rows = [
        {"uuid": "u-nb1", "content": "Target Cartwheel app " + ("x" * 900), "session_id": "s43", "reference_time": "2023-05-29T13:27:00Z", "cosine": 0.0, "source": "user"},
        # Exact content duplicate of u-top -> must be dropped
        {"uuid": "u-dup", "content": "Redeemed $5 coffee creamer coupon", "session_id": "s43", "reference_time": "2023-05-29T13:28:01Z", "cosine": 0.0, "source": "user"},
    ]

    graph_adapter = MagicMock()
    graph_adapter.search_episode_embeddings.return_value = search_rows
    graph_adapter.get_session_episode_neighbors.return_value = neighbor_rows

    async def fake_embed(_q: str) -> list[float]:
        return [0.1, 0.2]

    service = SimpleNamespace(
        graphiti_client=SimpleNamespace(embed_query=fake_embed),
        graph_adapter=graph_adapter,
    )

    # Default window=0 -> never calls get_session_episode_neighbors
    cfg_off = RetrievalTuningConfig(source_memory_limit=2, source_memory_session_window=0)
    sec_off, note_off, _ = await _build_source_memories(
        service, "coffee creamer", "lme-51a45a95", cfg_off, None, include_session=True
    )
    assert note_off is None
    assert [sm.uuid for sm in sec_off] == ["u-other", "u-top"]
    assert graph_adapter.get_session_episode_neighbors.call_count == 0

    # window=2 -> expands top-1 (u-top in s43), dedups u-dup, caps u-nb1 to neighbor_max_chars=200
    cfg_on = RetrievalTuningConfig(
        source_memory_limit=2,
        source_memory_session_window=2,
        source_memory_neighbor_max_chars=200,
    )
    sec_on, note_on, _ = await _build_source_memories(
        service, "coffee creamer", "lme-51a45a95", cfg_on, None, include_session=True
    )
    assert note_on is None
    assert [sm.uuid for sm in sec_on] == ["u-other", "u-nb1", "u-top"]
    nb = [sm for sm in sec_on if sm.uuid == "u-nb1"][0]
    assert nb.content.endswith("…")
    assert len(nb.content) == 201  # 200 chars + ellipsis
    graph_adapter.get_session_episode_neighbors.assert_called_once_with(
        "s43", "u-top", window=2, namespace="lme-51a45a95", sources=None
    )
