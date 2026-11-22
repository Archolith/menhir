"""Breaker gaps left after #638.

#639 sync clients honour and feed the matching breaker; #640 a call refused for "probe in flight"
can wait for the probe; #641 the reranker breaker guards cross-encoder calls; #642 recall_history
and timeline queries degrade with the breaker note instead of raising.

Fake providers only; no network.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

pytest.importorskip("graphiti_core")

from menhir.infrastructure import circuit_breaker as cb  # noqa: E402
from menhir.infrastructure.circuit_breaker import CircuitBreaker, CircuitOpenError  # noqa: E402
from menhir.infrastructure import graphiti_client as gc  # noqa: E402
from menhir.infrastructure.graphiti_client import (  # noqa: E402
    BreakerGuardedEmbedder,
    GraphitiClient,
)
from menhir.infrastructure.openai_rate_limit import QUOTA_REASON_CODE  # noqa: E402

pytestmark = pytest.mark.unit

_REQ = httpx.Request("POST", "https://api.openai.com/v1/embeddings")


def _quota_429() -> openai.RateLimitError:
    return openai.RateLimitError(
        "Error code: 429 - insufficient_quota",
        response=httpx.Response(429, request=_REQ),
        body={"code": "insufficient_quota", "type": "insufficient_quota", "message": "x"},
    )


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    now = [1000.0]
    monkeypatch.setattr(cb, "monotonic", lambda: now[0])
    return now


async def _trip(breaker: CircuitBreaker) -> None:
    async def slow():
        raise asyncio.TimeoutError("slow")

    for _ in range(breaker.failure_threshold):
        with pytest.raises(asyncio.TimeoutError):
            await breaker.call(slow)
    assert breaker.state_snapshot()["state"] == "open"


async def _start_probe(breaker: CircuitBreaker, clock) -> tuple[asyncio.Task, asyncio.Event]:
    """Trip the breaker, let the cooldown pass, and start a probe that blocks until released."""
    await _trip(breaker)
    clock[0] += breaker.cooldown_seconds
    release = asyncio.Event()
    outcome: dict[str, Any] = {}

    async def probe():
        await release.wait()
        if "error" in outcome:
            raise outcome["error"]
        return "probe"

    task = asyncio.create_task(breaker.call(probe))
    await asyncio.sleep(0)
    assert breaker.state_snapshot()["state"] == "half_open" and breaker._probe_in_flight
    release.outcome = outcome  # type: ignore[attr-defined]
    return task, release


# --- #640: waiting for the probe ------------------------------------------------------------


async def test_without_wait_a_call_beside_the_probe_is_still_refused(clock) -> None:
    breaker = CircuitBreaker(name="embed:x")
    probe, release = await _start_probe(breaker, clock)
    with pytest.raises(CircuitOpenError, match="probe in flight"):
        await breaker.call(lambda: asyncio.sleep(0))
    release.set()
    assert await probe == "probe"


async def test_waiter_runs_after_the_probe_succeeds(clock) -> None:
    breaker = CircuitBreaker(name="embed:x")
    probe, release = await _start_probe(breaker, clock)
    calls: list[str] = []

    async def second():
        calls.append("second")
        return "second"

    waiter = asyncio.create_task(breaker.call(second, wait_for_probe_s=30.0))
    await asyncio.sleep(0)
    assert not waiter.done() and calls == []  # waiting, not refused, not run
    release.set()
    assert await probe == "probe"
    assert await waiter == "second"
    assert breaker.state_snapshot()["state"] == "closed"


async def test_waiter_is_refused_without_a_call_when_the_probe_fails(clock) -> None:
    breaker = CircuitBreaker(name="embed:x")
    probe, release = await _start_probe(breaker, clock)
    release.outcome["error"] = asyncio.TimeoutError("still down")  # type: ignore[attr-defined]
    calls: list[str] = []

    async def second():
        calls.append("second")

    waiter = asyncio.create_task(breaker.call(second, wait_for_probe_s=30.0))
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.TimeoutError):
        await probe
    with pytest.raises(CircuitOpenError):
        await waiter
    assert calls == []
    assert breaker.state_snapshot()["state"] == "open"


async def test_waiter_is_woken_when_the_probe_is_cancelled(clock) -> None:
    breaker = CircuitBreaker(name="embed:x")
    probe, _release = await _start_probe(breaker, clock)
    waiter = asyncio.create_task(breaker.call(lambda: asyncio.sleep(0), wait_for_probe_s=30.0))
    await asyncio.sleep(0)
    probe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe
    with pytest.raises(CircuitOpenError):
        await asyncio.wait_for(waiter, timeout=1.0)
    assert breaker._probe_in_flight is False  # not wedged


async def test_wait_is_bounded_when_the_probe_hangs(clock) -> None:
    breaker = CircuitBreaker(name="embed:x")
    probe, release = await _start_probe(breaker, clock)
    with pytest.raises(CircuitOpenError, match="waited"):
        await breaker.call(lambda: asyncio.sleep(0), wait_for_probe_s=0.05)
    assert breaker._probe_in_flight  # the hung probe keeps its slot
    release.set()
    await probe


class _BatchEmbedder:
    def __init__(self) -> None:
        self.config = SimpleNamespace(embedding_model="m")
        self.calls = 0
        self.gate = asyncio.Event()

    async def create_batch(self, texts):
        self.calls += 1
        await self.gate.wait()
        return [[0.0] for _ in texts]


async def test_add_episode_batches_both_succeed_when_one_is_the_probe(clock) -> None:
    """#640 shape: two concurrent embed batches arrive as the cooldown ends."""
    breaker = CircuitBreaker(name="embed:x")
    await _trip(breaker)
    clock[0] += breaker.cooldown_seconds
    inner = _BatchEmbedder()
    guarded = BreakerGuardedEmbedder(inner)
    guarded.breaker = breaker
    batches = asyncio.gather(guarded.create_batch(["a"]), guarded.create_batch(["b", "c"]))
    await asyncio.sleep(0)
    inner.gate.set()
    first, second = await batches
    assert len(first) == 1 and len(second) == 2 and inner.calls == 2
    assert breaker.state_snapshot()["state"] == "closed"


async def test_single_create_beside_a_probe_is_refused_at_once(clock) -> None:
    """Recall and search embed through create(): they must degrade now, not wait on the probe."""
    breaker = CircuitBreaker(name="embed:x")
    probe, release = await _start_probe(breaker, clock)

    class _Single:
        config = SimpleNamespace(embedding_model="m")

        async def create(self, input_data):
            raise AssertionError("no provider call while the probe runs")

    guarded = BreakerGuardedEmbedder(_Single())
    guarded.breaker = breaker
    with pytest.raises(CircuitOpenError, match="probe in flight"):
        await asyncio.wait_for(guarded.create("q"), timeout=1.0)
    release.set()
    await probe


# --- #641: reranker breaker -----------------------------------------------------------------


class _FakeReranker:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def rank(self, query, passages):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [(p, 1.0) for p in passages]


def _client_with(cross_encoder: Any) -> GraphitiClient:
    return GraphitiClient(client=SimpleNamespace(embedder=None, cross_encoder=cross_encoder))


async def test_cross_encoder_calls_go_through_the_reranker_breaker() -> None:
    inner = _FakeReranker(error=asyncio.TimeoutError("reranker down"))
    guarded = gc.BreakerGuardedCrossEncoder(inner)
    client = _client_with(guarded)
    assert guarded.breaker is client._reranker_breaker
    for _ in range(3):
        with pytest.raises(asyncio.TimeoutError):
            await guarded.rank("q", ["a"])
    assert client.circuit_breaker_snapshots()["reranker"]["state"] == "open"
    with pytest.raises(CircuitOpenError):
        await guarded.rank("q", ["a"])
    assert inner.calls == 3


async def test_cross_encoder_success_passes_through() -> None:
    guarded = gc.BreakerGuardedCrossEncoder(_FakeReranker())
    _client_with(guarded)
    assert await guarded.rank("q", ["a", "b"]) == [("a", 1.0), ("b", 1.0)]


# --- #639: sync clients ---------------------------------------------------------------------


def test_breaker_for_only_matches_the_same_endpoint() -> None:
    client = GraphitiClient(
        client=SimpleNamespace(embedder=None),
        llm_base_url="https://api.openai.com/v1",
        embed_base_url="http://localhost:1234/v1/",
    )
    assert client.breaker_for("llm", "https://api.openai.com/v1/") is client.llm_breaker
    assert client.breaker_for("embed", "http://localhost:1234/v1") is client.embed_breaker
    assert client.breaker_for("llm", "http://localhost:1234/v1") is None


class _FakeOpenAI:
    """Stands in for openai.OpenAI; every create raises ``error`` or returns a minimal response."""

    error: Exception | None = None
    calls = 0

    def __init__(self, **_: Any) -> None:
        self.chat = SimpleNamespace(completions=self)
        self.embeddings = self

    def create(self, **_: Any) -> Any:
        type(self).calls += 1
        if type(self).error is not None:
            raise type(self).error
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=[0.5])],
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=None,
        )


@pytest.fixture
def fake_openai(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MENHIR_CHAT_PROVIDER", "local")
    monkeypatch.setenv("MENHIR_LOCAL_LLM_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("MENHIR_LOCAL_LLM_API_KEY", "test-key")
    monkeypatch.setenv("MENHIR_LOCAL_LLM_CHAT_MODEL", "test-model")
    # The embed settings are read without the MENHIR_ prefix.
    monkeypatch.setenv("LOCAL_LLM_EMBED_BASE_URL", "http://localhost:1235/v1")
    monkeypatch.setenv("LOCAL_LLM_EMBED_MODEL", "test-embed")
    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    _FakeOpenAI.error = None
    _FakeOpenAI.calls = 0
    yield _FakeOpenAI


def _view_embed(breaker):
    from menhir.config import MemorySettings
    from menhir.infrastructure.view_embedder import make_view_embedder

    embed = make_view_embedder(MemorySettings.from_env(), breaker=breaker)
    if embed is None:
        pytest.skip("view embedder unavailable in this configuration")
    return embed


def _sync_chat(breaker):
    from menhir.config import MemorySettings
    from menhir.infrastructure.sync_llm import make_sync_chat

    chat = make_sync_chat(MemorySettings.from_env(), breaker=breaker)
    assert chat is not None
    return chat


async def _billing_open(breaker: CircuitBreaker) -> None:
    async def refuse():
        raise _quota_429()

    with pytest.raises(Exception):
        await breaker.call(refuse)
    assert breaker.state_snapshot().get("reason") == QUOTA_REASON_CODE


async def test_view_embedder_makes_no_call_while_the_embed_breaker_refuses(fake_openai) -> None:
    breaker = CircuitBreaker(name="embed:x")
    await _billing_open(breaker)
    assert _view_embed(breaker)("text") is None
    assert fake_openai.calls == 0


async def test_sync_chat_makes_no_call_while_the_llm_breaker_refuses(fake_openai) -> None:
    breaker = CircuitBreaker(name="llm:x")
    await _billing_open(breaker)
    chat = _sync_chat(breaker)
    with pytest.raises(CircuitOpenError):
        await asyncio.to_thread(chat, "system", "user")
    assert fake_openai.calls == 0


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0.01)


async def test_view_embedder_billing_refusal_on_a_worker_thread_hard_opens(fake_openai) -> None:
    breaker = CircuitBreaker(name="embed:x")
    await breaker.call(lambda: asyncio.sleep(0))  # the breaker has a loop, as in production
    fake_openai.error = _quota_429()
    embed = _view_embed(breaker)
    assert await asyncio.to_thread(embed, "text") is None
    await _settle()
    snap = breaker.state_snapshot()
    assert snap["state"] == "open" and snap.get("reason") == QUOTA_REASON_CODE
    calls = fake_openai.calls
    assert await asyncio.to_thread(embed, "text") is None
    assert fake_openai.calls == calls  # refused without a call now


async def test_sync_chat_billing_refusal_on_a_worker_thread_hard_opens(fake_openai) -> None:
    breaker = CircuitBreaker(name="llm:x")
    await breaker.call(lambda: asyncio.sleep(0))
    fake_openai.error = _quota_429()
    chat = _sync_chat(breaker)
    with pytest.raises(Exception):
        await asyncio.to_thread(chat, "system", "user")
    await _settle()
    assert breaker.state_snapshot().get("reason") == QUOTA_REASON_CODE


async def test_billing_refusal_on_the_loop_thread_hard_opens(fake_openai) -> None:
    breaker = CircuitBreaker(name="embed:x")
    await breaker.call(lambda: asyncio.sleep(0))
    fake_openai.error = _quota_429()
    assert _view_embed(breaker)("text") is None  # sync call made on the loop thread
    await _settle()
    assert breaker.state_snapshot().get("reason") == QUOTA_REASON_CODE


def test_billing_refusal_before_any_call_hard_opens_directly(fake_openai) -> None:
    breaker = CircuitBreaker(name="embed:x")
    fake_openai.error = _quota_429()
    assert _view_embed(breaker)("text") is None
    assert breaker.state_snapshot().get("reason") == QUOTA_REASON_CODE


async def test_non_billing_errors_do_not_open_the_breaker(fake_openai) -> None:
    breaker = CircuitBreaker(name="embed:x")
    await breaker.call(lambda: asyncio.sleep(0))
    fake_openai.error = RuntimeError("bad payload")
    assert await asyncio.to_thread(_view_embed(breaker), "text") is None
    await _settle()
    assert breaker.state_snapshot()["state"] == "closed"


async def test_report_during_a_probe_keeps_the_probe_slot(clock) -> None:
    """A billing report while a probe runs must not free the slot for a second probe."""
    breaker = CircuitBreaker(name="embed:x")
    probe, release = await _start_probe(breaker, clock)
    assert await breaker.record_quota_refusal(_quota_429())
    assert breaker._probe_in_flight
    with pytest.raises(CircuitOpenError):
        await breaker.call(lambda: asyncio.sleep(0))
    release.set()
    await probe


# --- #642: recall_history and timeline degrade ----------------------------------------------


class _RefusingGraphiti:
    """embed_query goes through a billing-open breaker, as GraphitiClient.embed_query does."""

    def __init__(self, breaker: CircuitBreaker) -> None:
        self.breaker = breaker

    async def embed_query(self, query: str) -> list[float]:
        return await self.breaker.call(lambda: asyncio.sleep(0))

    def circuit_breaker_snapshots(self) -> dict[str, dict[str, Any]]:
        return {"embed": self.breaker.state_snapshot()}


class _NoSearchAdapter:
    def search_episode_embeddings(self, *args: Any, **kwargs: Any):
        raise AssertionError("no search without a query vector")

    def resolve_timeline_subject(self, *, subject, namespace):
        return [{"uuid": "entity-1", "name": "Rachel"}]

    def list_recall_visible_scalar_history_views(self, *, subject_uuid, namespace=None):
        return []

    def list_scalar_history_views(self, *, subject_uuid, namespace=None):
        return []


def _svc(graphiti: Any, adapter: Any = None):
    from menhir.services.recall_service import RecallService
    from menhir.services.scoring_service import ScoringService

    return RecallService(
        graphiti_client=graphiti,
        graph_adapter=adapter or _NoSearchAdapter(),
        scoring_service=ScoringService(),
    )


async def test_recall_history_returns_the_breaker_note_instead_of_raising() -> None:
    breaker = CircuitBreaker(name="embed:x")
    await _billing_open(breaker)
    result = await _svc(_RefusingGraphiti(breaker)).recall_history("how did auth change")
    assert result.memories == ()
    assert result.note is not None
    assert result.note.startswith("semantic search off: embedding provider refused for billing")
    assert "recall_history needs embeddings" in result.note


async def test_recall_history_still_raises_a_plain_embed_error() -> None:
    class _Broken:
        async def embed_query(self, query):
            raise ValueError("bad input")

        def circuit_breaker_snapshots(self):
            return {"embed": {"state": "closed"}}

    with pytest.raises(ValueError):
        await _svc(_Broken()).recall_history("q")


async def test_timeline_query_returns_the_breaker_note_instead_of_raising() -> None:
    breaker = CircuitBreaker(name="embed:x")
    await _billing_open(breaker)
    result = await _svc(_RefusingGraphiti(breaker)).recall_timeline(namespace="ns", query="q")
    assert result.entries == ()
    assert result.note is not None and result.note.startswith("semantic search off:")
    assert "needs embeddings to find its anchor" in result.note


async def test_source_memory_lane_logs_a_refusal_at_info_without_traceback(caplog) -> None:
    import logging

    from menhir.domain.retrieval_tuning import RetrievalTuningConfig
    from menhir.services.recall_pipeline import _build_source_memories

    breaker = CircuitBreaker(name="embed:x")
    await _billing_open(breaker)
    service = SimpleNamespace(graphiti_client=_RefusingGraphiti(breaker))
    caplog.set_level(logging.INFO, logger="menhir.services.recall_pipeline")
    section, warning, _ms = await _build_source_memories(
        service, "q", "ns", RetrievalTuningConfig(), source_memory_limit=3, include_session=True,
    )
    assert section is None and warning == "Source-memory lane unavailable"
    lane = [r for r in caplog.records if "Source-memory lane" in r.getMessage()]
    assert lane and all(r.levelno == logging.INFO and r.exc_info is None for r in lane)
