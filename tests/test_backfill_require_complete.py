"""`backfill_episode_embeddings.py --require-complete`: the build gate for unembedded READY rows."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit


def _load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "backfill_episode_embeddings.py"
    spec = importlib.util.spec_from_file_location("backfill_require_complete_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class _Repo:
    """Rows with a processing state; honours the cursor, ``ready_only`` and ``model``."""

    def __init__(self, rows: list[dict[str, Any]], refuse: set[str] = frozenset()) -> None:
        self.rows = sorted(rows, key=lambda r: r["uuid"])
        self.refuse = refuse
        self.embedded: dict[str, str] = {}
        self.ready_only_calls: list[tuple[bool, Any]] = []

    def list_episodes_missing_content_embedding(self, namespace, limit, *, after_uuid=None,
                                                model=None, ready_only=False):
        if ready_only:
            self.ready_only_calls.append((ready_only, model))
        return [r for r in self.rows
                if r["uuid"] not in self.embedded
                and (not ready_only or r["state"] == "READY")
                and (after_uuid is None or r["uuid"] > after_uuid)][:limit]

    def set_episode_content_embedding(self, uuid, embedding, model):
        if uuid in self.refuse:
            return False
        self.embedded[uuid] = model
        return True


def _run(monkeypatch, repo: _Repo, **args: Any) -> int:
    script = _load_script()
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
    base = dict(namespace=None, batch=3, limit=None, dry_run=False, require_complete=True)
    return asyncio.run(script._run(SimpleNamespace(**{**base, **args})))


def _rows(n: int, state: str = "READY", prefix: str = "r") -> list[dict[str, Any]]:
    return [{"uuid": f"{prefix}{i:02d}", "content": f"text {i}", "state": state} for i in range(n)]


def test_complete_after_backfill_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _Repo(_rows(7))
    assert _run(monkeypatch, repo) == 0
    assert len(repo.embedded) == 7
    # The check counts missing embeddings only, READY only.
    assert repo.ready_only_calls and all(c == (True, None) for c in repo.ready_only_calls)


def test_a_ready_row_left_unembedded_fails_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _Repo(_rows(4), refuse={"r02"})
    assert _run(monkeypatch, repo) == 3


def test_non_ready_rows_do_not_fail_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # A FAILED row is the failed-episode gate's business; it is not a missing READY embedding.
    repo = _Repo(_rows(2) + _rows(1, state="FAILED", prefix="f"), refuse={"f00"})
    assert _run(monkeypatch, repo) == 0


def test_dry_run_checks_without_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _Repo(_rows(2))
    assert _run(monkeypatch, repo, dry_run=True) == 3
    assert repo.embedded == {}


def test_without_the_flag_gaps_do_not_change_the_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _Repo(_rows(2), refuse={"r00"})
    assert _run(monkeypatch, repo, require_complete=False) == 0
