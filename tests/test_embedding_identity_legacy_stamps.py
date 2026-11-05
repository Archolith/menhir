"""CF-260: the embedder model name resolves from Graphiti's OpenAIEmbedder, and ingest's legacy
'unknown' stamps stay recallable while being re-embedded under the real model."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from menhir.infrastructure.episode_repository import EpisodeRepository
from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.services.embedding_identity import (
    LEGACY_EMBEDDING_MODELS,
    comparable_model,
    embedder_model_name,
)

pytestmark = [pytest.mark.unit]


class _Recording:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append((query, params or {}))
        return []


def test_model_name_resolves_from_openai_embedder_config() -> None:
    embedder = SimpleNamespace(config=SimpleNamespace(embedding_model=" text-embedding-3-small "))
    client = SimpleNamespace(embedder_ref=embedder)
    assert embedder_model_name(client) == "text-embedding-3-small"
    assert comparable_model(embedder_model_name(client)) == "text-embedding-3-small"
    # Earlier attributes still win; nothing resolvable stays 'unknown'.
    assert embedder_model_name(SimpleNamespace(embedder_ref=SimpleNamespace(model="m0", config=embedder.config))) == "m0"
    assert embedder_model_name(SimpleNamespace(embedder_ref=SimpleNamespace(config=SimpleNamespace()))) == "unknown"


def test_model_name_resolves_from_the_real_openai_embedder() -> None:
    # The original bug passed every test that used a fake; pin the shipped class.
    openai_embedder = pytest.importorskip("graphiti_core.embedder.openai")
    embedder = openai_embedder.OpenAIEmbedder(
        config=openai_embedder.OpenAIEmbedderConfig(
            api_key="not-used", embedding_model="text-embedding-3-small"
        )
    )
    assert embedder_model_name(SimpleNamespace(embedder_ref=embedder)) == "text-embedding-3-small"


def test_backfill_stamps_the_stripped_model_name(monkeypatch: pytest.MonkeyPatch) -> None:
    path = Path(__file__).resolve().parents[1] / "scripts" / "backfill_episode_embeddings.py"
    spec = importlib.util.spec_from_file_location("backfill_strip_under_test", path)
    assert spec and spec.loader
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    provider = SimpleNamespace(
        embed_model=" m1 ", base_url=None, api_key="k",
        supports_graphiti_openai_contract=lambda: True,
    )
    monkeypatch.setattr(script.ProviderConfig, "for_graphiti_embedder",
                        staticmethod(lambda settings: provider))
    monkeypatch.setattr(script, "build_async_openai_client", lambda **kw: object())

    _client, model = script._resolve_embedder(object())

    assert model == "m1" == comparable_model(provider.embed_model)


def test_legacy_set_is_unknown_only() -> None:
    # Unstamped (blank/NULL) vectors stay excluded under a known model (#171 A5).
    assert set(LEGACY_EMBEDDING_MODELS) == {"unknown"}


def test_recall_filter_admits_model_or_legacy_only() -> None:
    neo4j = _Recording()
    MemoryQueryRepository(neo4j).search_episode_embeddings(  # type: ignore[arg-type]
        [0.1], limit=5, namespace="ns", model="m1"
    )
    query, params = neo4j.calls[0]
    assert (
        "(coalesce(n.content_embedding_model, '') = $embedding_model"
        " OR coalesce(n.content_embedding_model, '') IN $legacy_embedding_models)"
    ) in query
    assert params["embedding_model"] == "m1"
    legacy = params["legacy_embedding_models"]
    # Emulate the predicate over stamps: NULL coalesces to ''.
    def admitted(stamp: str | None) -> bool:
        s = stamp or ""
        return s == "m1" or s in legacy

    assert [admitted(s) for s in ("m1", "unknown")] == [True] * 2
    assert [admitted(s) for s in ("m2", "", None)] == [False] * 3


def test_selector_reselects_legacy_and_other_models_not_current() -> None:
    neo4j = _Recording()
    EpisodeRepository(neo4j).list_episodes_missing_content_embedding(  # type: ignore[arg-type]
        "ns", limit=10, after_uuid="0", model="m1"
    )
    query, params = neo4j.calls[0]
    assert "coalesce(n.content_embedding_model, '') <> $model" in query
    assert "n.uuid > $after_uuid" in query and "ORDER BY n.uuid" in query
    assert params["model"] == "m1"
    # '<> $model' re-selects '', NULL, 'unknown' and 'm2' but never 'm1'.
    def selected(stamp: str | None) -> bool:
        return (stamp or "") != "m1"

    assert [selected(s) for s in ("", None, "unknown", "m2")] == [True] * 4
    assert selected("m1") is False


def test_has_embedding_check_treats_legacy_as_stale() -> None:
    neo4j = _Recording()
    EpisodeRepository(neo4j).episode_has_content_embedding("ep", "m1")  # type: ignore[arg-type]
    query, _ = neo4j.calls[0]
    assert "coalesce(n.content_embedding_model, '') = $model" in query
