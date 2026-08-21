"""CF-161: a base-URL rebind drops the embedding cache and leaks the previous client.

The three `_maybe_update_*_base_url` rebind paths rebuilt their client via
`build_async_openai_client(...)` **omitting** the `embedding_cache` kwarg that bootstrap passes,
so after the first base-URL rotation every embedding request bypassed the cache while
`embedding_cache_stats` kept reporting on the orphaned cache object, and the replaced
`httpx.AsyncClient` was dropped without `aclose()` (leaking its connection pool).

The fix threads the same `embedding_cache` object into the llm and embed rebind paths (the
reranker path already matches its bootstrap, which correctly omits the cache since a reranker
does no embedding) and schedules `aclose()` on the client being replaced.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from menhir.config import MemorySettings
from menhir.infrastructure.graphiti_client import GraphitiClient
import menhir.infrastructure.graphiti_client as graphiti_module

pytestmark = pytest.mark.unit


class _Config:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url


class _Ref:
    """Stands in for an llm/embed/reranker ref exposing `.client` and `.config.base_url`."""

    def __init__(self, client: Any, base_url: str) -> None:
        self.client = client
        self.config = _Config(base_url)


class _CloseRecorder:
    def __init__(self) -> None:
        self.called = 0

    async def aclose(self) -> None:
        self.called += 1


class _FakeClient:
    """Stands in for a built client; carries aclose recording and the built-in cache."""

    def __init__(self, embedding_cache: Any = None) -> None:
        recorder = _CloseRecorder()
        self._close_recorder = recorder
        # Mirrors `_InstrumentedAsyncOpenAI` -> AsyncOpenAI -> httpx.AsyncClient.
        self._inner = SimpleNamespace(_client=SimpleNamespace(aclose=recorder.aclose))
        self.embedding_cache = embedding_cache


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._counter = 0

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        self._counter += 1
        return _FakeClient(embedding_cache=kwargs.get("embedding_cache"))


def _wrapper(
    *,
    cache: Any,
    llm_client: Any,
    embed_client: Any | None = None,
    reranker_client: Any | None = None,
) -> GraphitiClient:
    embed_base_url = "http://old-embed/v1" if embed_client is not None else "http://old/v1"
    reranker_base_url = "http://old-rerank/v1" if reranker_client is not None else "http://old/v1"
    return GraphitiClient(
        client=object(),
        scheduler_settings=MemorySettings(),
        scheduler_api_key="llm-key",
        scheduler_embed_api_key="embed-key",
        scheduler_reranker_api_key="rerank-key",
        llm_base_url="http://old/v1",
        embed_base_url=embed_base_url,
        reranker_base_url=reranker_base_url,
        llm_client_ref=_Ref(llm_client, "http://old/v1"),
        embedder_ref=_Ref(embed_client, embed_base_url) if embed_client is not None else None,
        reranker_ref=_Ref(reranker_client, reranker_base_url) if reranker_client is not None else None,
        embedding_cache=cache,
    )


@pytest.mark.parametrize(
    "rebind,new_url,kwarg_base",
    [
        ("_maybe_update_client_base_url", "http://new/v1", "llm_base_url"),
        ("_maybe_update_embed_base_url", "http://new-embed/v1", "embed_base_url"),
    ],
)
def test_rebind_passes_the_same_embedding_cache_object(
    monkeypatch: pytest.MonkeyPatch,
    rebind: str,
    new_url: str,
    kwarg_base: str,
) -> None:
    """The finding: the rebuilt client was constructed WITH the same `embedding_cache` object."""
    cache = object()
    recorder = _Recorder()
    monkeypatch.setattr(graphiti_module, "build_async_openai_client", recorder)

    wrapper = _wrapper(cache=cache, llm_client=_FakeClient(), embed_client=_FakeClient())
    method = getattr(wrapper, rebind)

    if rebind == "_maybe_update_client_base_url":
        method(llm_base_url=new_url)
    else:
        method(embed_base_url=new_url)

    assert recorder.calls, f"{rebind} never built a client"
    assert "embedding_cache" in recorder.calls[-1], (
        f"{rebind} omitted embedding_cache; a rebind must build exactly like bootstrap"
    )
    assert recorder.calls[-1]["embedding_cache"] is cache, (
        "rebind did not pass the SAME cache object bootstrap used (identity, not truthiness)"
    )


def test_rebind_still_rebinds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control: the rebind actually rebinds -- new base URL reaches the builder and the
    attribute is reassigned. A fix that skipped the rebind would satisfy the cache test above."""
    cache = object()
    recorder = _Recorder()
    monkeypatch.setattr(graphiti_module, "build_async_openai_client", recorder)

    old_llm = _FakeClient()
    wrapper = _wrapper(cache=cache, llm_client=old_llm)

    wrapper._maybe_update_client_base_url(llm_base_url="http://new/v1")

    assert recorder.calls[-1]["base_url"] == "http://new/v1"
    new_client = wrapper.llm_client_ref.client
    assert new_client is not old_llm
    assert wrapper.llm_client_ref.config.base_url == "http://new/v1"
    assert wrapper.llm_base_url == "http://new/v1"


def test_rebound_client_still_reaches_the_same_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cache stays connected end to end.

    Structural form: driving a real embedding call is impractical here, so assert that the
    object reachable from the rebound client is the same cache instance bootstrap passed.
    """
    cache = object()
    recorder = _Recorder()
    monkeypatch.setattr(graphiti_module, "build_async_openai_client", recorder)

    wrapper = _wrapper(cache=cache, llm_client=_FakeClient())
    wrapper._maybe_update_client_base_url(llm_base_url="http://new/v1")

    assert wrapper.llm_client_ref.client.embedding_cache is cache


def test_embed_rebind_builds_exactly_like_embed_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The embed rebind must pass the same kwargs bootstrap's embed client uses, not just the cache."""
    cache = object()
    recorder = _Recorder()
    monkeypatch.setattr(graphiti_module, "build_async_openai_client", recorder)

    wrapper = _wrapper(cache=cache, llm_client=_FakeClient(), embed_client=_FakeClient())
    wrapper._maybe_update_embed_base_url(embed_base_url="http://new-embed/v1")

    assert recorder.calls[-1]["embedding_cache"] is cache
    assert recorder.calls[-1]["api_key"] == "embed-key"


def test_reranker_rebind_matches_reranker_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reranker rebind correctly omits the cache, exactly as its bootstrap does (a reranker
    does no embedding). It must not pass MORE than bootstrap."""
    cache = object()
    recorder = _Recorder()
    monkeypatch.setattr(graphiti_module, "build_async_openai_client", recorder)

    wrapper = _wrapper(cache=cache, llm_client=_FakeClient(), reranker_client=_FakeClient())
    wrapper._maybe_update_reranker_base_url(reranker_base_url="http://new-rerank/v1")

    assert recorder.calls[-1]["base_url"] == "http://new-rerank/v1"
    assert "embedding_cache" not in recorder.calls[-1]


@pytest.mark.asyncio
async def test_rebind_closes_the_replaced_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """The replaced client's `aclose` is scheduled on the running loop and run."""
    cache = object()
    recorder = _Recorder()
    monkeypatch.setattr(graphiti_module, "build_async_openai_client", recorder)

    old_llm = _FakeClient()
    wrapper = _wrapper(cache=cache, llm_client=old_llm)

    wrapper._maybe_update_client_base_url(llm_base_url="http://new/v1")

    assert wrapper._pending_client_closes, "the close was not scheduled on the running loop"
    await asyncio.gather(*wrapper._pending_client_closes)
    assert old_llm._close_recorder.called == 1


@pytest.mark.asyncio
async def test_rebind_does_not_close_a_client_still_shared(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reranker may share the llm client when their base URLs match; a rebind that closes it
    would strand the sibling on a closed pool."""
    cache = object()
    recorder = _Recorder()
    monkeypatch.setattr(graphiti_module, "build_async_openai_client", recorder)

    shared = _FakeClient()
    # llm and reranker share `shared`; only the llm rotates.
    wrapper = GraphitiClient(
        client=object(),
        scheduler_settings=MemorySettings(),
        scheduler_api_key="key",
        llm_base_url="http://old/v1",
        embed_base_url="http://old-embed/v1",
        reranker_base_url="http://old/v1",
        llm_client_ref=_Ref(shared, "http://old/v1"),
        embedder_ref=None,
        reranker_ref=_Ref(shared, "http://old/v1"),
        embedding_cache=cache,
    )

    wrapper._maybe_update_client_base_url(llm_base_url="http://new/v1")

    await asyncio.gather(*wrapper._pending_client_closes) if wrapper._pending_client_closes else None
    assert wrapper._pending_client_closes == [], "shared client must not be closed"


def test_reset_client_cache_clears_on_rebind(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rebind invalidates the chat-client cache (CF-177 interaction), verified structurally."""
    import menhir.infrastructure.providers as providers

    providers.reset_client_cache()
    providers._openai_client_cache[("k",)] = object()

    recorder = _Recorder()
    monkeypatch.setattr(graphiti_module, "build_async_openai_client", recorder)
    wrapper = _wrapper(cache=object(), llm_client=_FakeClient())
    wrapper._maybe_update_client_base_url(llm_base_url="http://new/v1")

    assert providers._openai_client_cache == {}, "rebind did not invalidate the chat-client cache"
    providers.reset_client_cache()
