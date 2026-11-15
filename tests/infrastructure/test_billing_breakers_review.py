"""Review follow-ups for the billing breakers (#638): cache hits, the tripping call in search,
a billing refusal racing a probe, implicit exception context, and a hostile exception.

Fake provider errors only; no network.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("graphiti_core")

from menhir.infrastructure import circuit_breaker as cb  # noqa: E402
from menhir.infrastructure.circuit_breaker import (  # noqa: E402
    CircuitBreaker,
    CircuitOpenError,
    tag_breaker_origin,
)
from menhir.infrastructure.embedding_cache import EmbeddingCache  # noqa: E402
from menhir.infrastructure.openai_rate_limit import QUOTA_REASON_CODE  # noqa: E402
from tests.infrastructure.test_billing_breakers import (  # noqa: E402
    _Cache,
    _FakeEmbedder,
    _billing_open,
    _client,
    _open_embed_for_billing,
    _quota_429,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    now = [1000.0]
    monkeypatch.setattr(cb, "monotonic", lambda: now[0])
    return now


# --- F2: a cache hit is not a provider call ------------------------------------------------------


async def test_cache_hits_are_served_while_the_breaker_is_open(clock) -> None:
    inner = _FakeEmbedder()
    inner.config.embedding_dim = 3
    client = _client(inner, cache=_Cache({"cached q": [9.0, 8.0, 7.0, 6.0]}))
    await _open_embed_for_billing(client, inner)

    assert await client.embed_query("cached q") == [9.0, 8.0, 7.0]
    assert await client.client.embedder.create_batch(["cached q"]) == [[9.0, 8.0, 7.0]]
    assert inner.calls == 0
    with pytest.raises(CircuitOpenError):
        await client.embed_query("not cached")


async def test_a_cache_hit_is_not_a_probe_and_does_not_end_a_billing_hold(clock) -> None:
    inner = _FakeEmbedder()
    client = _client(inner, cache=_Cache({"cached q": [1.0, 2.0, 3.0]}))
    await _open_embed_for_billing(client, inner)
    clock[0] += cb.QUOTA_COOLDOWN_SECONDS

    await client.embed_query("cached q")

    state = client.circuit_breaker_snapshots()["embed"]
    assert state["state"] == "open" and state["reason"] == QUOTA_REASON_CODE
    assert inner.calls == 0


async def test_cache_hits_do_not_reset_the_failure_count(clock) -> None:
    inner = _FakeEmbedder(lambda: TimeoutError("slow"))
    client = _client(inner, cache=_Cache({"cached q": [1.0, 2.0, 3.0]}))
    for _ in range(2):
        with pytest.raises(TimeoutError):
            await client.embed_query("miss")
        await client.embed_query("cached q")
    with pytest.raises(TimeoutError):
        await client.embed_query("miss")

    assert client.circuit_breaker_snapshots()["embed"]["state"] == "open"


def test_real_cache_get_all_needs_every_text_and_counts_only_full_hits() -> None:
    cache = EmbeddingCache(max_size=8)
    cache.set("a", [1.0], model="m")
    assert cache.get_all(["a", "b"], model="m") is None
    assert cache.get_all(["a"], model="other") is None
    assert cache.stats()["hits"] == 0 and cache.stats()["misses"] == 0
    assert cache.get_all(["a"], model="m") == [[1.0]]
    assert cache.stats()["hits"] == 1
    assert cache.get_all([], model="m") is None


# --- F6: the call that trips the breaker ---------------------------------------------------------


async def test_the_call_that_trips_the_breaker_degrades_search_quietly(clock, caplog) -> None:
    inner = _FakeEmbedder(_quota_429)
    client = _client(inner)

    with caplog.at_level("INFO"):
        scored = await client.search_scored("secret query text", num_results=5)

    assert scored == [("n1", "Node", 1.0)]
    assert client.client.searches == [["bm25", "cosine_similarity"], ["bm25"]]
    assert inner.calls == 1  # the refusal itself; no second embed through the cosine lane
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    assert "secret query text" not in caplog.text


# --- F4: a billing refusal that lands while a probe runs ------------------------------------------


async def test_billing_refusal_during_a_probe_survives_the_probe_timing_out(clock) -> None:
    breaker = CircuitBreaker(name="embed:x", cooldown_seconds=30.0)

    slow_gate = asyncio.Event()

    async def slow_then_refused():
        await slow_gate.wait()
        raise _quota_429()

    # A admitted while closed, still in flight; B hard-opens the breaker meanwhile.
    slow = asyncio.create_task(breaker.call(slow_then_refused))
    await asyncio.sleep(0)
    await _billing_open(breaker)
    clock[0] += cb.QUOTA_COOLDOWN_SECONDS

    probe_gate = asyncio.Event()

    async def probe_times_out():
        await probe_gate.wait()
        raise asyncio.TimeoutError("probe slow")

    probe = asyncio.create_task(breaker.call(probe_times_out))
    await asyncio.sleep(0)
    assert breaker._probe_in_flight

    slow_gate.set()  # A's billing refusal lands while the probe runs
    with pytest.raises(Exception):
        await slow
    assert breaker._probe_in_flight  # the slot stays with the running probe

    clock[0] += cb.QUOTA_COOLDOWN_SECONDS
    calls = []

    async def second_probe():
        calls.append(1)

    with pytest.raises(CircuitOpenError):  # no second probe beside the first
        await breaker.call(second_probe)
    assert calls == []

    probe_gate.set()
    with pytest.raises(asyncio.TimeoutError):
        await probe

    state = breaker.state_snapshot()
    assert state["state"] == "open" and state.get("reason") == QUOTA_REASON_CODE
    assert state["cooldown_remaining_s"] == pytest.approx(cb.QUOTA_COOLDOWN_SECONDS)
    assert breaker._probe_in_flight is False


# --- OQ1 / F5: attribution and a hostile exception ------------------------------------------------


async def test_an_error_raised_while_handling_a_foreign_error_counts_as_own(clock) -> None:
    outer = CircuitBreaker(name="llm:x", failure_threshold=1)

    async def llm_fails_in_handler():
        try:
            exc = TimeoutError("embed down")
            tag_breaker_origin(exc, "embed:x")
            raise exc
        except TimeoutError:
            raise ConnectionError("llm down too")  # implicit __context__ only

    with pytest.raises(ConnectionError):
        await outer.call(llm_fails_in_handler)
    assert outer.state_snapshot()["state"] == "open"


async def test_an_explicitly_chained_foreign_error_is_still_foreign(clock) -> None:
    outer = CircuitBreaker(name="llm:x", failure_threshold=1)

    async def wrapped_embed_error():
        inner = TimeoutError("embed down")
        tag_breaker_origin(inner, "embed:x")
        raise RuntimeError("graphiti wrapper") from inner

    with pytest.raises(RuntimeError):
        await outer.call(wrapped_embed_error)
    assert outer.state_snapshot()["state"] == "closed"


async def test_an_exception_whose_attributes_raise_does_not_leak_the_lock(clock) -> None:
    class Hostile(Exception):
        def __getattribute__(self, name):
            if name == cb.BREAKER_ORIGIN_ATTR:
                raise RuntimeError("boom")
            return super().__getattribute__(name)

    breaker = CircuitBreaker(name="embed:x")

    async def fails():
        raise Hostile("x")

    with pytest.raises(Hostile):
        await breaker.call(fails)
    assert not breaker._lock.locked()
    assert await asyncio.wait_for(breaker.call(lambda: asyncio.sleep(0, "ok")), 1) == "ok"
