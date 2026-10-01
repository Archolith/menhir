"""Audit 2026-10-01 F1/F2: source-memory backfill paging and embedding-model identity.

F1: the backfill filtered blank rows after LIMIT and stopped on any short page, and rows it
skipped were re-listed first forever. F2: stored vectors were compared/kept regardless of which
embedder model produced them.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from menhir.infrastructure.episode_repository import EpisodeRepository
from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.services.embedding_identity import comparable_model, embedder_model_name

pytestmark = [pytest.mark.unit]


class _Recording:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows = rows or []

    def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append((query, params or {}))
        return list(self.rows)


# ---------------------------------------------------------------- embedding identity


def test_embedder_model_name_and_comparable_model() -> None:
    client = SimpleNamespace(embedder_ref=SimpleNamespace(model=" text-embedding-3-small "))
    assert embedder_model_name(client) == "text-embedding-3-small"
    assert embedder_model_name(SimpleNamespace(embedder_ref=None)) == "unknown"
    assert comparable_model("text-embedding-3-small") == "text-embedding-3-small"
    assert comparable_model("unknown") is None
    assert comparable_model("") is None
    assert comparable_model(None) is None


# ---------------------------------------------------------------- queries


def test_listing_filters_blank_in_cypher_pages_by_cursor_and_relists_other_models() -> None:
    rows = [{"uuid": "a", "content": "x"}, {"uuid": "b", "content": ""}]
    neo4j = _Recording(rows)
    out = EpisodeRepository(neo4j).list_episodes_missing_content_embedding(  # type: ignore[arg-type]
        "ns", limit=10, after_uuid="0", model="m1"
    )
    query, params = neo4j.calls[0]
    assert "trim(coalesce(n.content, '')) <> ''" in query
    assert "($after_uuid IS NULL OR n.uuid > $after_uuid)" in query
    assert "coalesce(n.content_embedding_model, '') <> $model" in query
    assert "ORDER BY n.uuid" in query
    assert params["after_uuid"] == "0" and params["model"] == "m1" and params["limit"] == 10
    # No Python-side filtering after LIMIT: the page length is the query's.
    assert out == rows


def test_has_embedding_check_is_model_aware() -> None:
    neo4j = _Recording([{"has_embedding": False}])
    assert EpisodeRepository(neo4j).episode_has_content_embedding("ep", "m1") is False  # type: ignore[arg-type]
    query, params = neo4j.calls[0]
    assert "coalesce(n.content_embedding_model, '') = $model" in query
    assert params == {"uuid": "ep", "model": "m1"}


def test_search_filters_on_model_when_given() -> None:
    neo4j = _Recording()
    MemoryQueryRepository(neo4j).search_episode_embeddings(  # type: ignore[arg-type]
        [0.1, 0.2], limit=5, namespace="ns", model="m1"
    )
    query, params = neo4j.calls[0]
    assert "coalesce(n.content_embedding_model, '') = $embedding_model" in query
    assert params["embedding_model"] == "m1"
    # Unresolved model: no clause, no parameter -- byte-identical to the pre-fix query.
    neo4j = _Recording()
    MemoryQueryRepository(neo4j).search_episode_embeddings([0.1], limit=5, namespace="ns")  # type: ignore[arg-type]
    query, params = neo4j.calls[0]
    assert "content_embedding_model" not in query and "embedding_model" not in params


# ---------------------------------------------------------------- production callers


class _SearchSpy:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def search_episode_embeddings(self, query_vector: list[float], **kwargs: Any) -> list[dict[str, Any]]:
        self.kwargs = kwargs
        return []


@pytest.mark.parametrize("model,expected", [("m1", "m1"), (None, None)])
def test_recall_history_and_section_pass_the_current_model(model: str | None, expected: str | None) -> None:
    from menhir.domain.retrieval_tuning import RetrievalTuningConfig
    from menhir.services.recall_pipeline import _build_source_memories, run_recall_history

    async def embed_query(_q: str) -> list[float]:
        return [0.1, 0.2]

    spy = _SearchSpy()
    service = SimpleNamespace(
        graph_adapter=spy,
        graphiti_client=SimpleNamespace(
            embed_query=embed_query,
            embedder_ref=SimpleNamespace(model=model) if model else None,
        ),
    )
    asyncio.run(run_recall_history(service, "q", namespace="ns"))
    assert spy.kwargs.get("model") == expected

    spy.kwargs = {}
    tuning = RetrievalTuningConfig(enable_source_memories=True)
    asyncio.run(_build_source_memories(
        service, "q", "ns", tuning, None, include_session=True, content_query_vector=[0.1, 0.2],
    ))
    assert spy.kwargs.get("model") == expected
    if expected is None:
        assert "model" not in spy.kwargs


def test_ingest_step_reembeds_a_vector_from_another_model() -> None:
    from menhir.services.enrichment_steps import embed_episode_content

    seen: dict[str, Any] = {}

    class _Adapter:
        def episode_has_content_embedding(self, uuid: str, model: str | None = None) -> bool:
            seen["has_model"] = model
            return False  # stored vector is from another model -> not "has"

        def set_episode_content_embedding(self, uuid: str, embedding: list[float], model: str) -> bool:
            seen["set"] = (uuid, model)
            return True

    async def embed_query(_q: str) -> list[float]:
        return [0.3]

    ctx = SimpleNamespace(
        source_memories_enabled=True,
        claimed={"content": "I moved to the suburbs", "is_evidence_projection": False},
        graph_adapter=_Adapter(),
        episode_uuid="ep-1",
        graphiti_client=SimpleNamespace(embed_query=embed_query, embedder_ref=SimpleNamespace(model="m2")),
    )
    asyncio.run(embed_episode_content(ctx))
    assert seen["has_model"] == "m2"
    assert seen["set"] == ("ep-1", "m2")


# ---------------------------------------------------------------- backfill script loop


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "backfill_episode_embeddings.py"
    spec = importlib.util.spec_from_file_location("backfill_episode_embeddings_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class _FakeRepo:
    """Cursor-honoring stand-in for EpisodeRepository: rows sorted by uuid."""

    def __init__(self, rows: list[dict[str, Any]], refuse: set[str]) -> None:
        self.rows = sorted(rows, key=lambda r: r["uuid"])
        self.refuse = refuse
        self.embedded: dict[str, str] = {}
        self.pages = 0

    def list_episodes_missing_content_embedding(self, namespace, limit, *, after_uuid=None, model=None):
        self.pages += 1
        assert self.pages < 50, "backfill did not make progress"
        out = [r for r in self.rows
               if r["uuid"] not in self.embedded and (after_uuid is None or r["uuid"] > after_uuid)]
        return out[:limit]

    def set_episode_content_embedding(self, uuid, embedding, model):
        if uuid in self.refuse:
            return False
        self.embedded[uuid] = model
        return True


def test_backfill_finishes_past_skipped_rows_and_short_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _load_script()
    # Whitespace-only and refused rows sort FIRST and fill whole pages: the old loop either
    # stopped on the first short page or re-listed them forever.
    rows = [{"uuid": f"a{i:02d}", "content": "   "} for i in range(5)]
    rows += [{"uuid": "b00", "content": "refused"}]
    rows += [{"uuid": f"c{i:02d}", "content": f"memory {i}"} for i in range(7)]
    repo = _FakeRepo(rows, refuse={"b00"})

    monkeypatch.setattr(script, "_load_settings", lambda: SimpleNamespace(
        neo4j_uri="bolt://x", neo4j_database="neo4j", neo4j_user="u", neo4j_password="p"))
    monkeypatch.setattr(script, "Neo4jRepository", lambda **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(script, "EpisodeRepository", lambda neo4j: repo)
    monkeypatch.setattr(script.ProviderConfig, "for_graphiti_embedder",
                        staticmethod(lambda settings: SimpleNamespace(embed_model="m1")))
    monkeypatch.setattr(script, "_resolve_embedder", lambda settings: (object(), "m1"))

    async def fake_embed(client, *, model, text):
        return [0.5]

    monkeypatch.setattr(script, "_embed_text", fake_embed)
    args = SimpleNamespace(namespace=None, batch=3, limit=None, dry_run=False)
    assert asyncio.run(script._run(args)) == 0
    assert sorted(repo.embedded) == [f"c{i:02d}" for i in range(7)]
    assert set(repo.embedded.values()) == {"m1"}
