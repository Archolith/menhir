"""Source-memory embedding repair: the scheduler retry for embeds the ingest step gave up on."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from menhir.infrastructure.episode_repository import EpisodeRepository
from menhir.services import source_embedding_repair as repair_mod
from menhir.services.maintenance_scheduler import MaintenanceScheduler
from menhir.services.source_embedding_repair import repair_source_embeddings

pytestmark = [pytest.mark.unit]


class _Graph:
    """Fake adapter: READY rows missing an embedding, paged by an exclusive uuid cursor."""

    def __init__(self, contents: dict[str, str]) -> None:
        self.contents = dict(contents)
        self.written: dict[str, tuple[list[float], str]] = {}
        self.list_calls: list[dict[str, Any]] = []
        self.failures: dict[str, int] = {}

    def list_episodes_missing_content_embedding(self, namespace=None, limit=100, *, after_uuid=None,
                                                model=None, ready_only=False, settled_only=False,
                                                max_failures=None):
        self.list_calls.append({"namespace": namespace, "limit": limit, "after_uuid": after_uuid,
                                "model": model, "ready_only": ready_only,
                                "settled_only": settled_only})
        rows = [{"uuid": u, "content": c} for u, c in sorted(self.contents.items())
                if u not in self.written and (after_uuid is None or u > after_uuid)
                and (max_failures is None or self.failures.get(u, 0) < max_failures)]
        return rows[:limit]

    def record_source_embedding_failures(self, uuids, limit=None):
        for u in uuids:
            self.failures[u] = self.failures.get(u, 0) + 1
        return [u for u in uuids if limit is not None and self.failures[u] >= limit]

    def set_episode_content_embedding(self, uuid, embedding, model, *, expected_content=None):
        if uuid not in self.contents:
            return False
        if expected_content is not None and self.contents[uuid] != expected_content:
            return False
        self.written[uuid] = (embedding, model)
        self.failures.pop(uuid, None)
        return True


def _run(graph: _Graph, embed, **kw) -> dict[str, object]:
    return asyncio.run(repair_source_embeddings(graph, embed=embed, model_name=lambda: "m-1", **kw))


async def _ok(text: str) -> list[float]:
    return [float(len(text))]


def test_embeds_ready_missing_rows_with_the_model_stamp() -> None:
    graph = _Graph({"a": "alpha", "b": "beta"})
    result = _run(graph, _ok)
    assert graph.written == {"a": ([5.0], "m-1"), "b": ([4.0], "m-1")}
    assert result == {"written": 2, "failed": 0, "skipped": 0, "embed_calls": 2, "excluded": 0,
                      "stopped": None}
    # Settled (READY or FAILED, #662) and missing-only: never a model re-embed (model=None),
    # all namespaces.
    assert all(c["settled_only"] is True and c["ready_only"] is False and c["model"] is None
               and c["namespace"] is None for c in graph.list_calls)


def test_a_failed_row_is_counted_and_the_run_moves_past_it() -> None:
    graph = _Graph({"a": "alpha", "b": "boom", "c": "gamma"})

    async def embed(text: str) -> list[float]:
        if text == "boom":
            raise TimeoutError()
        return [1.0]

    result = _run(graph, embed)
    assert set(graph.written) == {"a", "c"}
    assert result["failed"] == 1 and result["written"] == 2 and result["stopped"] is None


def test_the_next_run_retries_a_row_that_failed() -> None:
    graph = _Graph({"a": "alpha"})
    calls = {"n": 0}

    async def flaky(text: str) -> list[float]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError()
        return [1.0]

    assert _run(graph, flaky)["failed"] == 1 and not graph.written
    assert _run(graph, flaky)["written"] == 1 and "a" in graph.written


def test_a_slow_embed_is_bounded_by_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repair_mod, "EMBED_TIMEOUT_S", 0.01)
    graph = _Graph({"a": "alpha"})

    async def slow(text: str) -> list[float]:
        await asyncio.sleep(1)
        return [1.0]

    assert _run(graph, slow)["failed"] == 1 and not graph.written


def test_consecutive_failures_end_the_run_early() -> None:
    graph = _Graph({f"r{i:02d}": f"text {i}" for i in range(20)})

    async def down(text: str) -> list[float]:
        raise ConnectionError("embedder down")

    result = _run(graph, down)
    assert result["failed"] == repair_mod.MAX_CONSECUTIVE_FAILURES
    assert result["stopped"] == "embedder_failing" and not graph.written


def test_call_budget_bounds_one_run_across_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repair_mod, "PAGE_SIZE", 3)
    graph = _Graph({f"r{i:02d}": f"text {i}" for i in range(10)})
    result = _run(graph, _ok, call_budget=7)
    assert result["written"] == 7 and result["stopped"] == "call_budget"
    assert len(graph.list_calls) == 3  # 3 + 3 + 1 rows from three pages


def test_content_changed_after_listing_is_not_stamped() -> None:
    # Counterexample: the row is listed, the text changes before the write lands.
    graph = _Graph({"a": "old text"})

    async def embed_then_edit(text: str) -> list[float]:
        graph.contents["a"] = "new text"
        return [1.0]

    result = _run(graph, embed_then_edit)
    assert not graph.written and result["skipped"] == 1 and result["written"] == 0


def test_erased_between_listing_and_write_is_not_stamped() -> None:
    graph = _Graph({"a": "alpha"})

    async def embed_then_erase(text: str) -> list[float]:
        del graph.contents["a"]
        return [1.0]

    assert _run(graph, embed_then_erase)["skipped"] == 1 and not graph.written


def test_empty_embedding_is_skipped_not_written() -> None:
    graph = _Graph({"a": "alpha"})

    async def empty(text: str) -> list[float]:
        return []

    assert _run(graph, empty)["skipped"] == 1 and not graph.written


def _poison_head(n: int = 5) -> dict[str, str]:
    contents = {f"a{i}": "poison" for i in range(n)}
    contents["z"] = "healthy"
    return contents


async def _fails_on_poison(text: str) -> list[float]:
    if text == "poison":
        raise ValueError("bad item")
    return [1.0]


def test_failing_head_no_longer_blocks_later_episodes_across_runs() -> None:
    graph = _Graph(_poison_head())
    first = _run(graph, _fails_on_poison)
    assert first["stopped"] == "embedder_failing" and "z" not in graph.written
    for _ in range(repair_mod.MAX_EPISODE_FAILURES - 1):
        _run(graph, _fails_on_poison)
    assert "z" not in graph.written  # head rows are still under the limit
    _run(graph, _fails_on_poison)
    assert "z" in graph.written  # head rows reached the limit, so the listing moved past them


def test_an_outage_does_not_count_against_episodes() -> None:
    graph = _Graph({f"r{i:02d}": f"text {i}" for i in range(20)})

    async def down(text: str) -> list[float]:
        raise ConnectionError("embedder down")

    for _ in range(repair_mod.MAX_EPISODE_FAILURES + 2):
        assert _run(graph, down)["stopped"] == "embedder_failing"
    assert graph.failures == {}
    assert len(_run(graph, _ok)["stopped"] or "") == 0 and len(graph.written) == 20


def test_empty_embeddings_are_counted_and_eventually_skipped() -> None:
    graph = _Graph({"a": "alpha", "b": "beta"})

    async def empty_for_a(text: str) -> list[float]:
        return [] if text == "alpha" else [1.0]

    for _ in range(repair_mod.MAX_EPISODE_FAILURES):
        _run(graph, empty_for_a)
    assert graph.failures == {"a": repair_mod.MAX_EPISODE_FAILURES}
    assert _run(graph, empty_for_a)["embed_calls"] == 0  # "a" no longer listed


def test_success_clears_the_failure_count() -> None:
    graph = _Graph({"a": "alpha"})
    calls = {"n": 0}

    async def flaky(text: str) -> list[float]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError()
        return [1.0]

    _run(graph, flaky)
    assert graph.failures == {"a": 1}
    _run(graph, flaky)
    assert "a" in graph.written and graph.failures == {}


class _Recording:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append((query, params or {}))
        return []


def test_set_binds_the_write_to_expected_content_only_when_given() -> None:
    neo4j = _Recording()
    repo = EpisodeRepository(neo4j)  # type: ignore[arg-type]
    repo.set_episode_content_embedding("u", [1.0], "m")
    repo.set_episode_content_embedding("u", [1.0], "m", expected_content="text")
    (q1, p1), (q2, p2) = neo4j.calls
    assert "$expected_content IS NULL OR n.content = $expected_content" in q1
    assert p1["expected_content"] is None and p2["expected_content"] == "text"


def test_listing_settled_only_keeps_ready_and_failed() -> None:
    from menhir.infrastructure.cypher import EPISODE_NOT_ERASED

    neo4j = _Recording()
    repo = EpisodeRepository(neo4j)  # type: ignore[arg-type]
    repo.list_episodes_missing_content_embedding(None, 10, settled_only=True)
    query, params = neo4j.calls[0]
    assert "($settled_only = false OR n.processing_state IN ['READY', 'FAILED'])" in query
    assert params["settled_only"] is True
    # Erase tombstones (FAILED, deleted_by_user) are never listed for embedding.
    assert EPISODE_NOT_ERASED in query
    assert "n.processing_state <> 'FAILED'" not in query


def test_listing_failure_limit_is_opt_in() -> None:
    neo4j = _Recording()
    repo = EpisodeRepository(neo4j)  # type: ignore[arg-type]
    repo.list_episodes_missing_content_embedding(None, 10)
    repo.list_episodes_missing_content_embedding(None, 10, max_failures=3)
    (q1, p1), (_q2, p2) = neo4j.calls
    assert "$max_failures IS NULL OR coalesce(n.source_embedding_failures, 0) < $max_failures" in q1
    assert p1["max_failures"] is None and p2["max_failures"] == 3


def test_listing_ready_only_adds_the_state_predicate() -> None:
    neo4j = _Recording()
    repo = EpisodeRepository(neo4j)  # type: ignore[arg-type]
    repo.list_episodes_missing_content_embedding(None, 10)
    repo.list_episodes_missing_content_embedding(None, 10, ready_only=True)
    (q1, p1), (_q2, p2) = neo4j.calls
    assert "($ready_only = false OR n.processing_state = 'READY')" in q1
    assert p1["ready_only"] is False and p2["ready_only"] is True


def _scheduler(**kw: Any) -> MaintenanceScheduler:
    return MaintenanceScheduler(ingest_service=object(), graph_adapter=_Graph({"a": "alpha"}),  # type: ignore[arg-type]
                                structure_watcher_enabled=False, experience_counter_enabled=False, **kw)


def test_job_registers_only_when_enabled_and_wired() -> None:
    wired = {"source_embedding_embed": _ok, "source_embedding_model_name": lambda: "m-1"}
    assert "repair_source_embeddings" not in _scheduler()._jobs
    assert "repair_source_embeddings" in _scheduler(**wired)._jobs  # on by default (#168 freeze)
    assert "repair_source_embeddings" not in _scheduler(
        source_embedding_repair_enabled=False, **wired)._jobs
    assert "repair_source_embeddings" not in _scheduler(
        source_embedding_repair_enabled=True, source_embedding_embed=_ok)._jobs
    sched = _scheduler(source_embedding_repair_enabled=True, **wired)
    assert sched._jobs["repair_source_embeddings"].interval_s == 300.0
    result = asyncio.run(sched._make_repair_source_embeddings())
    assert result["written"] == 1


def test_listing_relists_excluded_episode_after_cooldown() -> None:
    from menhir.infrastructure.episode_lifecycle import SOURCE_EMBEDDING_FAILURE_COOLDOWN

    neo4j = _Recording()
    EpisodeRepository(neo4j).list_episodes_missing_content_embedding(  # type: ignore[arg-type]
        None, 10, max_failures=3)
    (query, params), = neo4j.calls
    assert "n.source_embedding_last_failed_at < datetime() - duration($failure_cooldown)" in query
    assert params["failure_cooldown"] == SOURCE_EMBEDDING_FAILURE_COOLDOWN


def test_an_outage_with_few_rows_does_not_count_against_episodes() -> None:
    # Fewer rows than MAX_CONSECUTIVE_FAILURES: the run ends without an embedder_failing stop.
    graph = _Graph({"a": "alpha", "b": "beta"})

    async def down(text: str) -> list[float]:
        raise ConnectionError("embedder down")

    for _ in range(repair_mod.MAX_EPISODE_FAILURES + 2):
        assert _run(graph, down)["stopped"] is None
    assert graph.failures == {}


def test_the_outage_probe_cannot_be_answered_by_a_cache() -> None:
    graph = _Graph({f"r{i:02d}": f"text {i}" for i in range(10)})
    cache: dict[str, list[float]] = {}
    probes: list[str] = []
    up = {"value": True}

    async def cached_embed(text: str) -> list[float]:
        if text.startswith(repair_mod._PROBE_TEXT):
            probes.append(text)
        if text in cache:
            return cache[text]
        if not up["value"] or not text.startswith(repair_mod._PROBE_TEXT):
            raise ConnectionError("embedder down")
        cache[text] = [1.0]
        return cache[text]

    _run(graph, cached_embed)  # rows fail, probe succeeds and is cached: item faults counted
    counted = dict(graph.failures)
    up["value"] = False
    _run(graph, cached_embed)  # real outage: a cached probe answer must not count it
    assert graph.failures == counted
    assert len(probes) == len(set(probes)) == 2
