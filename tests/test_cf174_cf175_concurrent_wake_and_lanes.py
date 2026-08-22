"""CF-174 / CF-175 -- the wake sequence and the two retrieval lanes run concurrently.

**CF-174's diagnosis was measured and found to point at the wrong cost.** The entry blames three
serialized scheduler round trips and prescribes `asyncio.gather`. Against a stub scheduler on this
host, gathering alone bought 9% at localhost latency, because the dominant cost was never I/O:
`acquire_llama_url_async` built a fresh `httpx.AsyncClient` per call at ~190 ms a time (SSL context
construction, which does not warm up), so a three-endpoint wake paid ~570 ms of pure CPU before a
byte moved. One event loop cannot parallelize CPU, which is exactly why the prescribed fix
underperformed.

    stub latency          0 ms    40 ms   150 ms
    before             2273     1908     2978    ms (median wake)
    after               215      282      493    ms

OWNER RULING 2026-08-22, two parts:

*Scope:* fix both -- gather the acquires in `graphiti_client` AND reuse the HTTP client in
`llama_endpoint`, even though CF-174 never names the second file.

*No memoization.* The entry also asks for a short-TTL memo keyed by `task`, which would not fix the
re-entrance it cites as its own evidence (the nested `embed_query` wake uses a different task
string, so it would never hit the memo). Keying by kind instead would fix it only by making the
scheduler's per-task trace lie -- the acquired URL carries the task id -- and by suppressing
`/acquire` calls that also serve as the idle watchdog's keepalive. Ruled: keep every acquire, keep
task attribution honest, make redundant wakes cheap rather than absent.

**The load-bearing invariant is that the REBINDS stay ordered even though the ACQUIRES do not.**
`_maybe_update_client_base_url` does not only touch the LLM: when the embedder currently shares the
LLM's base URL it retargets the embedder too, deciding that by reading `self.embed_base_url` before
the embed branch has run. Applying the rebinds in completion order would make the embedder's final
target depend on which HTTP response landed first. That is what most of this file is about.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from menhir.infrastructure import graphiti_client as graphiti_client_module
from menhir.infrastructure.graphiti_client import GraphitiClient
from menhir.infrastructure.llama_endpoint import (
    _scheduler_http_clients,
    _shared_scheduler_http_client,
    aclose_scheduler_http_client,
)

pytestmark = pytest.mark.unit

FALLBACK = "http://127.0.0.1:8081/v1"


class _Ref:
    def __init__(self) -> None:
        self.client: object | None = None
        self.config = type("C", (), {"base_url": ""})()


def _wake_client(**overrides: object) -> GraphitiClient:
    """A GraphitiClient with only the fields the wake sequence reads."""

    client = GraphitiClient.__new__(GraphitiClient)
    client.scheduler_settings = object()
    client.scheduler_api_key = "k"
    client.scheduler_embed_api_key = "k"
    client.scheduler_reranker_api_key = "k"
    client.scheduler_fallback_base_url = FALLBACK
    client.scheduler_fallback_embed_base_url = FALLBACK
    client.scheduler_fallback_reranker_base_url = FALLBACK
    client.llm_base_url = FALLBACK
    client.embed_base_url = FALLBACK
    client.reranker_base_url = FALLBACK
    client.llm_client_ref = _Ref()
    client.embedder_ref = _Ref()
    client.reranker_ref = _Ref()
    client.embedding_cache = None
    client._pending_client_closes = []
    for key, value in overrides.items():
        setattr(client, key, value)
    return client


@pytest.fixture
def wake_env(monkeypatch: pytest.MonkeyPatch):
    """Stub the scheduler so acquire latency is controllable and URL identity follows `task`."""

    monkeypatch.setattr(graphiti_client_module, "should_use_scheduler", lambda _base: True)
    monkeypatch.setattr(
        graphiti_client_module, "build_async_openai_client", lambda **kw: object()
    )

    async def _noop_thread(fn, *a, **kw):
        return None

    # The wake writes 8 lifecycle rows per call through asyncio.to_thread; they are not what is
    # under test here and their thread dispatch would swamp the timing assertion below.
    monkeypatch.setattr(graphiti_client_module.asyncio, "to_thread", _noop_thread)

    state: dict[str, object] = {"delay": 0.0, "tasks": [], "order": []}

    async def _acquire(*, fallback: str, task: str | None = None, timeout_s: float = 30.0) -> str:
        state["tasks"].append(task)
        await asyncio.sleep(float(state["delay"]))
        state["order"].append(task)
        return f"http://scheduler/v1/t/{task}"

    monkeypatch.setattr(graphiti_client_module, "acquire_llama_url_async", _acquire)
    return state


# ---------------------------------------------------------------------------
# CF-174 -- the acquires overlap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_three_endpoint_acquires_overlap_rather_than_queue(wake_env) -> None:
    """THE FINDING. Three 100 ms acquires took 300 ms serially; overlapped they take ~100 ms.

    The bound is deliberately loose (< 250 ms): this asserts "not three in a row", which is the
    claim, and not a latency budget that would flake on a loaded CI box.
    """
    wake_env["delay"] = 0.1

    started = time.perf_counter()
    await _wake_client()._ensure_graphiti_endpoints_alive(task="t")
    elapsed = time.perf_counter() - started

    assert len(wake_env["tasks"]) == 3
    assert elapsed < 0.25, f"wake took {elapsed * 1000:.0f} ms; three 100 ms acquires still serial"


@pytest.mark.asyncio
async def test_every_endpoint_still_acquires_under_its_own_task(wake_env) -> None:
    """POSITIVE CONTROL, and the owner's no-memo ruling as an assertion.

    A wake that got fast by dropping two of the three acquires would pass the timing test above
    while silently breaking two things: the scheduler traces by task id, and `_last_acquire_time`
    -- its idle watchdog's keepalive -- is refreshed per acquire.
    """
    await _wake_client()._ensure_graphiti_endpoints_alive(task="memory: search")

    assert sorted(wake_env["tasks"]) == [
        "memory: search",
        "memory: search embed",
        "memory: search reranker",
    ]


@pytest.mark.asyncio
async def test_a_disabled_endpoint_is_not_acquired(wake_env) -> None:
    """The branch filter still runs before anything is launched."""
    client = _wake_client(scheduler_fallback_reranker_base_url="")
    await client._ensure_graphiti_endpoints_alive(task="t")

    assert wake_env["tasks"] == ["t", "t embed"]


# ---------------------------------------------------------------------------
# CF-174 -- rebinds stay ordered even when acquires do not
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_endpoint_is_rebound_to_its_own_acquired_url(
    wake_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE ORDERING HAZARD, forced. The embed acquire is made to finish FIRST, which is the
    interleaving that never happens serially.

    `_maybe_update_client_base_url` retargets the embedder whenever it currently shares the LLM's
    base URL -- a read of state the embed branch also writes. Applying rebinds in completion order
    would let the embedder end up on the LLM's endpoint here.
    """

    async def _acquire(*, fallback: str, task: str | None = None, timeout_s: float = 30.0) -> str:
        # reranker first, embed second, llm last -- the exact reverse of the rebind order
        await asyncio.sleep(0.0 if "reranker" in (task or "") else 0.01 if "embed" in (task or "") else 0.02)
        return f"http://scheduler/v1/t/{task}"

    monkeypatch.setattr(graphiti_client_module, "acquire_llama_url_async", _acquire)

    client = _wake_client()
    await client._ensure_graphiti_endpoints_alive(task="t")

    assert client.llm_base_url == "http://scheduler/v1/t/t"
    assert client.embed_base_url == "http://scheduler/v1/t/t embed"
    assert client.reranker_base_url == "http://scheduler/v1/t/t reranker"


@pytest.mark.asyncio
async def test_rebinds_are_applied_in_llm_embed_reranker_order(
    wake_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The invariant stated directly, independent of what the rebinds compute.

    The test above asserts the OUTCOME and would still pass if the retarget-the-embedder branch
    happened not to fire for the fixture's inputs. This asserts the ORDER itself, so the guard
    does not quietly depend on that.
    """
    applied: list[str] = []
    for name in ("_maybe_update_client_base_url", "_maybe_update_embed_base_url",
                 "_maybe_update_reranker_base_url"):
        monkeypatch.setattr(
            GraphitiClient, name, lambda self, _n=name, **kw: applied.append(_n)
        )

    async def _acquire(*, fallback: str, task: str | None = None, timeout_s: float = 30.0) -> str:
        await asyncio.sleep(0.0 if "reranker" in (task or "") else 0.01)
        return "http://scheduler/x"

    monkeypatch.setattr(graphiti_client_module, "acquire_llama_url_async", _acquire)
    await _wake_client()._ensure_graphiti_endpoints_alive(task="t")

    assert applied == [
        "_maybe_update_client_base_url",
        "_maybe_update_embed_base_url",
        "_maybe_update_reranker_base_url",
    ]


@pytest.mark.asyncio
async def test_one_failing_endpoint_does_not_stop_the_others(
    wake_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Serially each branch had its own try/except, so an LLM acquire failure still left embed and
    reranker rebound. `gather(return_exceptions=True)` plus a per-branch handler keeps that; a
    bare `gather` would have lost it."""

    async def _acquire(*, fallback: str, task: str | None = None, timeout_s: float = 30.0) -> str:
        if task == "t":
            raise OSError("scheduler down")
        return f"http://scheduler/v1/t/{task}"

    monkeypatch.setattr(graphiti_client_module, "acquire_llama_url_async", _acquire)

    client = _wake_client()
    await client._ensure_graphiti_endpoints_alive(task="t")

    assert client.llm_base_url == FALLBACK, "failed branch must not be rebound"
    assert client.embed_base_url == "http://scheduler/v1/t/t embed"
    assert client.reranker_base_url == "http://scheduler/v1/t/t reranker"


@pytest.mark.asyncio
async def test_an_unexpected_exception_still_propagates(
    wake_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The serial version swallowed exactly (HTTPError, OSError, TimeoutError, RuntimeError) and
    let everything else out. `return_exceptions=True` makes it easy to swallow the lot by
    accident, which would convert a programming error into a silent fallback."""

    async def _acquire(*, fallback: str, task: str | None = None, timeout_s: float = 30.0) -> str:
        raise KeyError("url")

    monkeypatch.setattr(graphiti_client_module, "acquire_llama_url_async", _acquire)

    with pytest.raises(KeyError):
        await _wake_client()._ensure_graphiti_endpoints_alive(task="t")


# ---------------------------------------------------------------------------
# CF-174 -- the cost that actually dominated: one HTTP client per loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_scheduler_http_client_is_reused_within_a_loop() -> None:
    """THE MEASURED FIX. A fresh `httpx.AsyncClient` per acquire cost ~190 ms of SSL context
    construction -- more than the round trips CF-174 filed."""
    try:
        first = _shared_scheduler_http_client()
        assert _shared_scheduler_http_client() is first
    finally:
        await aclose_scheduler_http_client()


@pytest.mark.asyncio
async def test_the_client_is_keyed_on_the_loop_not_the_process() -> None:
    """A plain module global would be reused across `asyncio.run` boundaries -- every CLI entry
    point, every test -- and an `httpx.AsyncClient` binds its connection pool to the loop that
    drove it. This is the difference between a cache and a latent cross-loop failure."""
    loop = asyncio.get_running_loop()
    try:
        client = _shared_scheduler_http_client()
        assert _scheduler_http_clients[loop] is client

        other: list[object] = []

        def _in_another_loop() -> None:
            async def _get() -> None:
                other.append(_shared_scheduler_http_client())
                await aclose_scheduler_http_client()

            asyncio.run(_get())

        await asyncio.to_thread(_in_another_loop)
        assert other and other[0] is not client, "a second loop must not share the first's client"
    finally:
        await aclose_scheduler_http_client()


@pytest.mark.asyncio
async def test_closing_drops_the_cache_entry_so_it_is_not_reused_closed() -> None:
    """Shutdown closes it; a later acquire on the same loop must build a fresh one rather than
    post to a closed pool."""
    loop = asyncio.get_running_loop()
    first = _shared_scheduler_http_client()
    await aclose_scheduler_http_client()

    assert loop not in _scheduler_http_clients
    assert first.is_closed
    try:
        assert _shared_scheduler_http_client() is not first
    finally:
        await aclose_scheduler_http_client()


# ---------------------------------------------------------------------------
# CF-175 -- the two retrieval lanes
# ---------------------------------------------------------------------------


class _LaneClient:
    """A GraphitiClient stub whose two lanes each sleep, so serialization is visible in the clock."""

    def __init__(self, delay: float = 0.1) -> None:
        self.delay = delay
        self.driver = object()


def _lane_wrapper(monkeypatch: pytest.MonkeyPatch, *, delay: float = 0.1) -> GraphitiClient:
    wrapper = GraphitiClient.__new__(GraphitiClient)
    wrapper.client = type("C", (), {"driver": object()})()

    async def _no_wake(*, task: str) -> None:
        return None

    async def _embed(query: str) -> list[float]:
        await asyncio.sleep(delay / 2)
        return [0.1, 0.2]

    monkeypatch.setattr(wrapper, "_ensure_graphiti_endpoints_alive", _no_wake)
    monkeypatch.setattr(wrapper, "embed_query", _embed)
    return wrapper


def _stub_lanes(monkeypatch: pytest.MonkeyPatch, *, delay: float = 0.1) -> dict[str, object]:
    """Patch graphiti_core's two search helpers, which the method imports at call time."""
    from graphiti_core.search import search_utils

    seen: dict[str, object] = {"calls": []}

    class _Node:
        def __init__(self, uuid: str, name: str) -> None:
            self.uuid = uuid
            self.name = name
            self.labels = ["Entity"]

    async def _bm25(driver, query, filters, group_ids, limit):
        seen["calls"].append("bm25")
        await asyncio.sleep(delay)
        return [_Node("b1", "bm25-hit")]

    async def _cosine(driver, vector, filters, group_ids, limit):
        seen["calls"].append("cosine")
        await asyncio.sleep(delay / 2)
        return [_Node("c1", "cosine-hit")]

    monkeypatch.setattr(search_utils, "node_fulltext_search", _bm25)
    monkeypatch.setattr(search_utils, "node_similarity_search", _cosine)
    return seen


@pytest.mark.asyncio
async def test_bm25_and_cosine_lanes_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE FINDING. bm25 (100 ms) plus embed (50 ms) plus cosine (50 ms) was 200 ms in series and
    is max(100, 50 + 50) = 100 ms overlapped."""
    _stub_lanes(monkeypatch)
    wrapper = _lane_wrapper(monkeypatch)

    started = time.perf_counter()
    ranked = await wrapper.search_ranked_by_method(
        "q", methods=["bm25", "cosine_similarity"], num_results=5
    )
    elapsed = time.perf_counter() - started

    assert set(ranked) == {"bm25", "cosine_similarity"}
    assert elapsed < 0.17, f"lanes took {elapsed * 1000:.0f} ms; still serial"


@pytest.mark.asyncio
async def test_both_lanes_still_return_their_own_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    """POSITIVE CONTROL. A version that dropped a lane, or let one lane's result overwrite the
    other's key, would pass the timing test above."""
    _stub_lanes(monkeypatch)
    wrapper = _lane_wrapper(monkeypatch)

    ranked = await wrapper.search_ranked_by_method(
        "q", methods=["bm25", "cosine_similarity"], num_results=5
    )

    assert ranked["bm25"] == [("b1", "bm25-hit")]
    assert ranked["cosine_similarity"] == [("c1", "cosine-hit")]


@pytest.mark.asyncio
async def test_one_failing_lane_leaves_the_other_intact(monkeypatch: pytest.MonkeyPatch) -> None:
    """The docstring promises "other retrieval lanes will continue". Under `gather` that needs the
    handler INSIDE the lane; a bare `gather` cancels siblings on the first exception."""
    from graphiti_core.search import search_utils

    class _Node:
        def __init__(self) -> None:
            self.uuid = "b1"
            self.name = "bm25-hit"
            self.labels = ["Entity"]

    async def _bm25(driver, query, filters, group_ids, limit):
        await asyncio.sleep(0.02)
        return [_Node()]

    async def _cosine(driver, vector, filters, group_ids, limit):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(search_utils, "node_fulltext_search", _bm25)
    monkeypatch.setattr(search_utils, "node_similarity_search", _cosine)

    wrapper = _lane_wrapper(monkeypatch)
    ranked = await wrapper.search_ranked_by_method(
        "q", methods=["bm25", "cosine_similarity"], num_results=5
    )

    assert ranked["bm25"] == [("b1", "bm25-hit")]
    assert ranked["cosine_similarity"] == []


@pytest.mark.asyncio
async def test_all_lanes_failing_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """The method's contract distinguishes "one lane degraded" from "retrieval is down"; swallowing
    both into an empty dict would make a total outage look like zero results."""
    from graphiti_core.search import search_utils

    async def _boom(*a, **kw):
        raise RuntimeError("down")

    monkeypatch.setattr(search_utils, "node_fulltext_search", _boom)
    monkeypatch.setattr(search_utils, "node_similarity_search", _boom)

    wrapper = _lane_wrapper(monkeypatch)
    with pytest.raises(RuntimeError, match="all requested Graphiti search lanes failed"):
        await wrapper.search_ranked_by_method(
            "q", methods=["bm25", "cosine_similarity"], num_results=5
        )


@pytest.mark.asyncio
async def test_an_unsupported_method_is_rejected_before_any_lane_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DELIBERATE, DOCUMENTED CHANGE. Serially the ValueError landed only after every earlier
    lane had already queried Neo4j. Concurrently there is no "earlier", so the check has to be
    hoisted -- and hoisting it is the stricter behaviour: no work is done for a request that was
    always going to be rejected."""
    seen = _stub_lanes(monkeypatch)
    wrapper = _lane_wrapper(monkeypatch)

    with pytest.raises(ValueError, match="Unsupported search method"):
        await wrapper.search_ranked_by_method(
            "q", methods=["bm25", "nonsense"], num_results=5
        )

    assert seen["calls"] == [], "bm25 ran before the unsupported method was rejected"
