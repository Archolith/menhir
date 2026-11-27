"""A refused (too-short) vector must not let the embedding scripts exit 0."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_refusal_exit_under_test", _SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class _Repo:
    def __init__(self, uuids: list[str]) -> None:
        self.uuids = sorted(uuids)
        self.written: set[str] = set()

    def list_episodes_missing_content_embedding(self, namespace, limit, *, after_uuid=None,
                                                model=None, ready_only=False):
        return [{"uuid": u, "content": f"text {u}"} for u in self.uuids
                if u not in self.written and (after_uuid is None or u > after_uuid)][:limit]

    def set_episode_content_embedding(self, uuid, embedding, model, *, expected_content=None):
        self.written.add(uuid)
        return True


def _backfill(monkeypatch, vector: list[float], **args) -> tuple[int, _Repo]:
    script = _load("backfill_episode_embeddings")
    repo = _Repo(["a", "b"])
    monkeypatch.setattr(script, "_load_settings", lambda: SimpleNamespace(
        neo4j_uri="bolt://x", neo4j_database="neo4j", neo4j_user="u", neo4j_password="p"))
    monkeypatch.setattr(script, "Neo4jRepository", lambda **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(script, "EpisodeRepository", lambda neo4j: repo)
    monkeypatch.setattr(script.ProviderConfig, "for_graphiti_embedder",
                        staticmethod(lambda settings: SimpleNamespace(embed_model="m1")))
    monkeypatch.setattr(script, "_resolve_embedder", lambda settings: (object(), "m1"))
    monkeypatch.setattr(script, "configured_embedding_dimension", lambda model: 4)

    async def embed(client, *, model, text):
        return vector

    monkeypatch.setattr(script, "_embed_text", embed)
    base = dict(namespace=None, batch=10, limit=None, dry_run=False, require_complete=False)
    return asyncio.run(script._run(SimpleNamespace(**{**base, **args}))), repo


def test_backfill_exits_4_when_a_short_vector_was_refused(monkeypatch) -> None:
    code, repo = _backfill(monkeypatch, [0.0, 1.0])
    assert code == 4
    assert repo.written == set()


def test_backfill_clean_run_still_exits_zero(monkeypatch) -> None:
    code, repo = _backfill(monkeypatch, [0.0] * 4)
    assert code == 0
    assert repo.written == {"a", "b"}


def test_backfill_require_complete_keeps_exit_3_over_4(monkeypatch) -> None:
    code, _ = _backfill(monkeypatch, [0.0, 1.0], require_complete=True)
    assert code == 3


def test_backfill_dry_run_does_not_exit_4(monkeypatch) -> None:
    code, _ = _backfill(monkeypatch, [0.0, 1.0], dry_run=True)
    assert code == 0


def _run_repair_main(monkeypatch, result: dict, argv: list[str]) -> int:
    script = _load("repair_embedding_dimensions")
    monkeypatch.setattr(sys, "argv", ["repair_embedding_dimensions.py", *argv])
    monkeypatch.setattr(script, "_load_settings", lambda: SimpleNamespace(
        neo4j_uri="bolt://x", neo4j_database="neo4j", neo4j_user="u", neo4j_password="p"))
    monkeypatch.setattr(script, "Neo4jRepository", lambda **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(script, "embedding_dimension_health", lambda neo4j, expected_dim=None: {"ok": True})

    async def fake_apply(neo4j, **kw):
        return result

    monkeypatch.setattr(script, "_run_apply", fake_apply)
    return script.main()


def _result(wrong: int, ok: bool = True) -> dict:
    empty = {"updated": 0, "skipped": 0, "stale": 0, "wrong_dimension": 0}
    return {
        "reembedded": {"entities": {**empty, "wrong_dimension": wrong},
                       "communities": dict(empty), "relationships": dict(empty)},
        "post_health": {"ok": ok},
    }


def test_repair_apply_exits_4_when_a_vector_was_refused(monkeypatch) -> None:
    assert _run_repair_main(monkeypatch, _result(2), ["--apply", "--expected-dim", "4"]) == 4


def test_repair_apply_clean_run_exits_zero(monkeypatch) -> None:
    assert _run_repair_main(monkeypatch, _result(0), ["--apply", "--expected-dim", "4"]) == 0


def test_repair_unhealthy_graph_keeps_exit_1_over_4(monkeypatch) -> None:
    assert _run_repair_main(monkeypatch, _result(2, ok=False), ["--apply", "--expected-dim", "4"]) == 1
