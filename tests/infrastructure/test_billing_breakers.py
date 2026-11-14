"""Billing breakers: embed refusals belong to the embed breaker (#493), every Graphiti embed goes
through it (#630), and a billing hold ends by itself once the provider answers again (#492).

Fake provider errors only; no network.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import openai
import pytest

pytest.importorskip("graphiti_core")

from menhir.infrastructure import circuit_breaker as cb  # noqa: E402
from menhir.infrastructure import graphiti_client as gc  # noqa: E402
from menhir.infrastructure.circuit_breaker import (  # noqa: E402
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    breaker_origin,
    tag_breaker_origin,
)
from menhir.infrastructure.graphiti_client import BreakerGuardedEmbedder, GraphitiClient  # noqa: E402
from menhir.infrastructure.openai_rate_limit import QUOTA_REASON_CODE  # noqa: E402
from menhir.services.enrichment_steps import add_episode_with_timeout  # noqa: E402

pytestmark = pytest.mark.unit

_REQ = httpx.Request("POST", "https://api.openai.com/v1/embeddings")
_WHEN = datetime(2024, 2, 14, tzinfo=timezone.utc)


def _quota_429() -> openai.RateLimitError:
    return openai.RateLimitError(
        "Error code: 429 - insufficient_quota",
        response=httpx.Response(429, request=_REQ),
        body={"code": "insufficient_quota", "type": "insufficient_quota", "message": "x"},
    )


def _payment_402() -> openai.APIStatusError:
    return openai.APIStatusError("402", response=httpx.Response(402, request=_REQ), body=None)


class _FakeEmbedder:
    """Stands in for OpenAIEmbedder; counts every provider call."""

    def __init__(self, error=None) -> None:
        self.error = error
        self.calls = 0
        self.config = SimpleNamespace(embedding_model="text-embedding-3-small")

    async def create(self, input_data):
        self.calls += 1
        if self.error is not None:
            raise self.error()
        return [0.1, 0.2, 0.3]

    async def create_batch(self, input_data_list):
        self.calls += 1
        if self.error is not None:
            raise self.error()
        return [[0.1, 0.2, 0.3] for _ in input_data_list]


class _Native:
    """A Graphiti stand-in that embeds through the embedder it was handed, as Graphiti does."""

    def __init__(self, embedder, *, llm_error=None) -> None:
        self.embedder = embedder
        self.llm_error = llm_error
        self.searches: list[list[str]] = []

    async def add_episode(self, **kwargs):
        if self.llm_error is not None:
            raise self.llm_error()
        await self.embedder.create_batch(["entity a", "entity b"])

    async def search_(self, query, config, group_ids=None):
        methods = [m.value for m in config.node_config.search_methods]
        self.searches.append(methods)
        if "cosine_similarity" in methods:
            await self.embedder.create(input_data=[query])
        node = SimpleNamespace(uuid="n1", name="Node", labels=["Entity"])
        return SimpleNamespace(nodes=[node], node_reranker_scores=[1.0])


def _client(inner: _FakeEmbedder, **native_kwargs) -> GraphitiClient:
    native = _Native(BreakerGuardedEmbedder(inner), **native_kwargs)
    return GraphitiClient(client=native, embedder_ref=inner)


async def _add(client: GraphitiClient) -> None:
    await add_episode_with_timeout(
        client,
        name="n",
        episode_body="b",
        source_description="s",
        reference_time=_WHEN,
        episode_uuid="ep-1",
        timeout_s=5.0,
    )


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    now = [1000.0]
    monkeypatch.setattr(cb, "monotonic", lambda: now[0])
    return now


async def _open_embed_for_billing(client: GraphitiClient, inner: _FakeEmbedder) -> None:
    inner.error = _quota_429
    with pytest.raises(Exception):
        await client.embed_query("q")
    assert client.circuit_breaker_snapshots()["embed"]["reason"] == QUOTA_REASON_CODE
    inner.error = None
    inner.calls = 0


# --- #493: an embed refusal inside add_episode is the embed breaker's ---------------------------


@pytest.mark.parametrize("error", [_quota_429, _payment_402], ids=["quota-429", "payment-402"])
async def test_embed_billing_refusal_during_ingest_opens_embed_not_llm(error, clock) -> None:
    inner = _FakeEmbedder(error)
    client = _client(inner)

    with pytest.raises(Exception):
        await _add(client)

    snaps = client.circuit_breaker_snapshots()
    assert snaps["embed"]["state"] == "open" and snaps["embed"]["reason"] == QUOTA_REASON_CODE
    assert snaps["llm"]["state"] == "closed" and snaps["llm"]["failures"] == 0
    assert "reason" not in snaps["llm"]


async def test_open_embed_breaker_during_ingest_raises_circuit_open_and_spares_llm(clock) -> None:
    inner = _FakeEmbedder()
    client = _client(inner)
    await _open_embed_for_billing(client, inner)

    with pytest.raises(CircuitOpenError):
        await _add(client)

    assert inner.calls == 0
    assert client.circuit_breaker_snapshots()["llm"]["state"] == "closed"


async def test_llm_billing_refusal_still_hard_opens_the_llm_breaker(clock) -> None:
    inner = _FakeEmbedder()
    client = _client(inner, llm_error=_quota_429)

    with pytest.raises(Exception):
        await _add(client)

    snaps = client.circuit_breaker_snapshots()
    assert snaps["llm"]["reason"] == QUOTA_REASON_CODE
    assert snaps["embed"]["state"] == "closed"


# --- #630: census of every GraphitiClient path that embeds --------------------------------------


async def test_open_embed_breaker_stops_every_embedding_path(clock, caplog) -> None:
    inner = _FakeEmbedder()
    client = _client(inner)
    await _open_embed_for_billing(client, inner)

    with pytest.raises(CircuitOpenError):
        await client.embed_query("q")

    with caplog.at_level("INFO"):
        scored = await client.search_scored("secret query text", num_results=5)
    assert scored == [("n1", "Node", 1.0)]
    # Fused search failed fast at the embed, then ran BM25-only, which does not embed.
    assert client.client.searches == [["bm25", "cosine_similarity"], ["bm25"]]
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    assert "secret query text" not in caplog.text

    assert await client.count_similar_by_cosine("q", exclude_uuid="x", min_cosine=0.5) == -1

    with pytest.raises(CircuitOpenError):
        await _add(client)

    assert inner.calls == 0


async def test_cosine_lane_is_skipped_quietly_when_embed_breaker_is_open(clock, monkeypatch, caplog) -> None:
    import graphiti_core.search.search_utils as su

    inner = _FakeEmbedder()
    client = _client(inner)
    client.client.driver = object()
    await _open_embed_for_billing(client, inner)

    async def bm25(*args, **kwargs):
        return [SimpleNamespace(uuid="n1", name="Node", labels=["Entity"])]

    async def cosine(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("cosine search ran without a query vector")

    monkeypatch.setattr(su, "node_fulltext_search", bm25)
    monkeypatch.setattr(su, "node_similarity_search", cosine)

    with caplog.at_level("INFO"):
        ranked = await client.search_ranked_by_method(
            "secret query text", methods=["bm25", "cosine_similarity"]
        )

    assert ranked == {"bm25": [("n1", "Node")], "cosine_similarity": []}
    assert inner.calls == 0
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_production_construction_hands_graphiti_the_guarded_embedder(monkeypatch) -> None:
    from menhir.config.settings_model import MemorySettings

    seen: dict = {}

    class _CapturingGraphiti:
        def __init__(self, *, edge_expiry_hook=None, preserved_edge_attributes=None, **kwargs) -> None:
            seen.update(kwargs)
            self.embedder = kwargs["embedder"]

    monkeypatch.setattr(gc, "Graphiti", _CapturingGraphiti)
    client = GraphitiClient.from_settings_with_capabilities(MemorySettings())
    client = client[0] if isinstance(client, tuple) else client

    guarded = seen["embedder"]
    assert isinstance(guarded, BreakerGuardedEmbedder)
    assert guarded.breaker is client._embed_breaker
    # embedder_ref stays the raw embedder: embed_query wraps it once, model naming reads it.
    assert client.embedder_ref is guarded.inner
    assert not isinstance(client.embedder_ref, BreakerGuardedEmbedder)


# --- #492: a billing hold ends without an operator --------------------------------------------


async def _billing_open(breaker: CircuitBreaker) -> None:
    async def refuse():
        raise _quota_429()

    with pytest.raises(Exception):
        await breaker.call(refuse)
    assert breaker.state_snapshot()["reason"] == QUOTA_REASON_CODE


async def test_billing_hold_probes_after_quota_cooldown_and_closes_when_paid(clock) -> None:
    breaker = CircuitBreaker(name="embed:x")
    await _billing_open(breaker)

    clock[0] += cb.QUOTA_COOLDOWN_SECONDS - 1
    with pytest.raises(CircuitOpenError):
        await breaker.call(lambda: asyncio.sleep(0))

    clock[0] += 1
    calls = []

    async def ok():
        calls.append(1)
        return "ok"

    assert await breaker.call(ok) == "ok"
    snap = breaker.state_snapshot()
    assert snap["state"] == "closed" and "reason" not in snap and calls == [1]


async def test_probe_timeout_ends_billing_hold_and_uses_normal_cooldown(clock) -> None:
    breaker = CircuitBreaker(name="embed:x", cooldown_seconds=30.0)
    await _billing_open(breaker)
    clock[0] += cb.QUOTA_COOLDOWN_SECONDS

    async def times_out():
        raise asyncio.TimeoutError("slow")

    with pytest.raises(asyncio.TimeoutError):
        await breaker.call(times_out)

    snap = breaker.state_snapshot()
    assert snap["state"] == "open" and "reason" not in snap
    assert snap["cooldown_remaining_s"] == pytest.approx(30.0)


async def test_probe_refused_again_stays_on_billing_cooldown(clock) -> None:
    breaker = CircuitBreaker(name="embed:x")
    await _billing_open(breaker)
    clock[0] += cb.QUOTA_COOLDOWN_SECONDS
    await _billing_open(breaker)
    assert breaker.state_snapshot()["cooldown_remaining_s"] == pytest.approx(cb.QUOTA_COOLDOWN_SECONDS)


async def test_success_then_new_refusal_hard_opens_again(clock) -> None:
    breaker = CircuitBreaker(name="embed:x")
    await _billing_open(breaker)
    clock[0] += cb.QUOTA_COOLDOWN_SECONDS
    assert await breaker.call(lambda: asyncio.sleep(0, "ok")) == "ok"
    await _billing_open(breaker)
    assert breaker.state_snapshot()["state"] == "open"


async def test_cancelled_billing_probe_does_not_wedge(clock) -> None:
    breaker = CircuitBreaker(name="embed:x")
    await _billing_open(breaker)
    clock[0] += cb.QUOTA_COOLDOWN_SECONDS

    started = asyncio.Event()

    async def hang():
        started.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(breaker.call(hang))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert breaker._probe_in_flight is False
    assert breaker.state_snapshot()["state"] == "open"


async def test_concurrent_callers_during_billing_probe_make_one_provider_call(clock) -> None:
    breaker = CircuitBreaker(name="embed:x")
    await _billing_open(breaker)
    clock[0] += cb.QUOTA_COOLDOWN_SECONDS

    gate = asyncio.Event()
    calls = []

    async def provider():
        calls.append(1)
        await gate.wait()
        return "ok"

    probe = asyncio.create_task(breaker.call(provider))
    await asyncio.sleep(0)
    others = [asyncio.create_task(breaker.call(provider)) for _ in range(5)]
    results = await asyncio.gather(*others, return_exceptions=True)
    assert all(isinstance(r, CircuitOpenError) for r in results)
    gate.set()
    assert await probe == "ok"
    assert calls == [1]


# --- breaker origin: one provider's failure never trips another's breaker -------------------------


async def test_foreign_breaker_error_changes_nothing(clock) -> None:
    outer = CircuitBreaker(name="llm:x", failure_threshold=1)

    async def inner_fails():
        exc = TimeoutError("embed provider down")
        tag_breaker_origin(exc, "embed:x")
        raise exc

    with pytest.raises(TimeoutError) as caught:
        await outer.call(inner_fails)

    assert breaker_origin(caught.value) == "embed:x"
    assert outer.state_snapshot()["state"] == "closed" and outer.state_snapshot()["failures"] == 0


async def test_foreign_error_during_probe_returns_to_open_and_next_caller_probes(clock) -> None:
    outer = CircuitBreaker(name="llm:x", failure_threshold=1, cooldown_seconds=30.0)

    async def down():
        raise TimeoutError("llm down")

    with pytest.raises(TimeoutError):
        await outer.call(down)
    clock[0] += 30.0

    async def foreign():
        exc = TimeoutError("embed down")
        tag_breaker_origin(exc, "embed:x")
        raise exc

    with pytest.raises(TimeoutError):
        await outer.call(foreign)
    assert outer.state_snapshot()["state"] == "open" and outer._probe_in_flight is False

    assert await outer.call(lambda: asyncio.sleep(0, "ok")) == "ok"
    assert outer._state == CircuitState.CLOSED


async def test_own_error_is_tagged_with_own_name(clock) -> None:
    breaker = CircuitBreaker(name="embed:x")

    async def fails():
        raise ValueError("parse")

    with pytest.raises(ValueError) as caught:
        await breaker.call(fails)
    assert breaker_origin(caught.value) == "embed:x"
