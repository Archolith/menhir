"""#967 / #969: repair and backfill redact before embedding; the backfill keeps the configured dimension."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from menhir.services.source_embedding_repair import repair_source_embeddings

pytestmark = pytest.mark.unit

GITHUB = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
STORED = f"deploy token {GITHUB} end"


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "backfill_episode_embeddings.py"
    spec = importlib.util.spec_from_file_location("backfill_redaction_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class _Graph:
    def __init__(self, contents: dict[str, str]) -> None:
        self.contents = dict(contents)
        self.written: dict[str, list[float]] = {}
        self.expected: dict[str, str | None] = {}

    def list_episodes_missing_content_embedding(self, namespace=None, limit=100, *, after_uuid=None,
                                                model=None, ready_only=False, settled_only=False,
                                                max_failures=None):
        return [{"uuid": u, "content": c} for u, c in sorted(self.contents.items())
                if u not in self.written and (after_uuid is None or u > after_uuid)][:limit]

    def record_source_embedding_failures(self, uuids, limit=None):
        return {}

    def set_episode_content_embedding(self, uuid, embedding, model, *, expected_content=None):
        self.expected[uuid] = expected_content
        if self.contents.get(uuid) != expected_content:
            return False
        self.written[uuid] = embedding
        return True


def test_service_repair_embeds_redacted_text_and_cas_uses_stored_content() -> None:
    graph = _Graph({"a": STORED})
    seen: list[str] = []

    async def embed(text: str) -> list[float]:
        seen.append(text)
        return [1.0]

    result = asyncio.run(repair_source_embeddings(graph, embed=embed, model_name=lambda: "m"))
    assert seen and GITHUB not in seen[0] and "[REDACTED" in seen[0]
    assert graph.expected["a"] == STORED  # the write premise is the stored value, not the redaction
    assert graph.written == {"a": [1.0]} and result["written"] == 1


class _Repo:
    def __init__(self, contents: dict[str, str]) -> None:
        self.graph = _Graph(contents)

    def list_episodes_missing_content_embedding(self, namespace, limit, *, after_uuid=None,
                                                model=None, ready_only=False):
        return self.graph.list_episodes_missing_content_embedding(
            namespace, limit, after_uuid=after_uuid)

    def set_episode_content_embedding(self, uuid, embedding, model, *, expected_content=None):
        return self.graph.set_episode_content_embedding(
            uuid, embedding, model, expected_content=expected_content)


def _run_backfill(monkeypatch, repo: _Repo, embed, dim: int | None = 4) -> int:
    script = _load_script()
    monkeypatch.setattr(script, "_load_settings", lambda: SimpleNamespace(
        neo4j_uri="bolt://x", neo4j_database="neo4j", neo4j_user="u", neo4j_password="p"))
    monkeypatch.setattr(script, "Neo4jRepository", lambda **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(script, "EpisodeRepository", lambda neo4j: repo)
    monkeypatch.setattr(script.ProviderConfig, "for_graphiti_embedder",
                        staticmethod(lambda settings: SimpleNamespace(embed_model="m1")))
    monkeypatch.setattr(script, "_resolve_embedder", lambda settings: (object(), "m1"))
    monkeypatch.setattr(script, "configured_embedding_dimension", lambda model: dim)
    monkeypatch.setattr(script, "_embed_text", embed)
    args = SimpleNamespace(namespace=None, batch=10, limit=None, dry_run=False, require_complete=False)
    return asyncio.run(script._run(args))


def test_backfill_embeds_redacted_text_and_cas_uses_stored_content(monkeypatch) -> None:
    repo = _Repo({"a": STORED})
    seen: list[str] = []

    async def embed(client, *, model, text):
        seen.append(text)
        return [0.0] * 4

    assert _run_backfill(monkeypatch, repo, embed) == 0
    assert seen and GITHUB not in seen[0] and "[REDACTED" in seen[0]
    assert repo.graph.expected["a"] == STORED
    assert "a" in repo.graph.written


def test_backfill_truncates_oversized_vectors_like_graphiti(monkeypatch) -> None:
    repo = _Repo({"a": "plain"})

    async def embed(client, *, model, text):
        return [float(i) for i in range(10)]

    _run_backfill(monkeypatch, repo, embed, dim=4)
    assert repo.graph.written == {"a": [0.0, 1.0, 2.0, 3.0]}


def test_backfill_refuses_a_short_vector(monkeypatch, capsys) -> None:
    repo = _Repo({"a": "plain"})

    async def embed(client, *, model, text):
        return [0.0, 1.0]

    _run_backfill(monkeypatch, repo, embed, dim=4)
    assert repo.graph.written == {}
    assert "REFUSED 1" in capsys.readouterr().err
