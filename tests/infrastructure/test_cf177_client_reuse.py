"""CF-177: a fresh OpenAI client and connection pool per chat completion.

`OpenAIStyleChatBackend.create_chat_completion` called `openai_client_factory` on every request,
so each LLM call built a fresh `httpx.AsyncClient` (and connection pool, or a full TLS handshake
for a remote provider) with no keep-alive. The fix caches one client per distinct configuration
and keys on everything that changes client identity -- the factory, base URL, API key, request
timeout, and the Langfuse fields `build_async_openai_client` branches on -- with `reset_client_cache()`
clearing it, which CF-161's base-URL rebind path invokes.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from menhir.config import MemorySettings
from menhir.infrastructure.graphiti_client import GraphitiClient
import menhir.infrastructure.providers as providers
from menhir.infrastructure.providers import (
    OpenAIStyleChatBackend,
    ProviderConfig,
    ProviderKind,
    ProviderRuntimeDependencies,
)

pytestmark = pytest.mark.unit


class _Response:
    def __init__(self, content: str = "ok") -> None:
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=content))]


async def _create(**_: Any) -> _Response:
    return _Response()


class _FakeClient:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=_create))


def _make_factory() -> tuple[Callable[..., Any], dict[str, int], list[_FakeClient]]:
    calls: dict[str, int] = {"n": 0}
    clients: list[_FakeClient] = []

    def factory(**_: Any) -> _FakeClient:
        calls["n"] += 1
        client = _FakeClient()
        clients.append(client)
        return client

    return factory, calls, clients


def _backend(factory: Callable[..., Any], *, base_url: str = "http://localhost:1234/v1", api_key: str = "k") -> OpenAIStyleChatBackend:
    provider = ProviderConfig(
        kind=ProviderKind.LOCAL,
        base_url=base_url,
        api_key=api_key,
        chat_model="test-model",
    )
    return OpenAIStyleChatBackend(
        provider=provider,
        settings=MemorySettings(),
        dependencies=ProviderRuntimeDependencies(openai_client_factory=factory, request_timeout_s=0.5),
    )


async def _call(backend: OpenAIStyleChatBackend) -> str:
    return await backend.create_chat_completion(
        system_prompt="sys",
        user_prompt="user",
        operation="compression",
        max_tokens=16,
        temperature=0.0,
    )


@pytest.fixture(autouse=True)
def _reset_cache():
    providers.reset_client_cache()
    yield
    providers.reset_client_cache()


@pytest.mark.asyncio
async def test_same_configuration_builds_one_client() -> None:
    """The finding: N calls with the same configuration construct ONE client."""
    factory, calls, clients = _make_factory()
    backend = _backend(factory)

    for _ in range(5):
        assert await _call(backend) == "ok"

    assert calls["n"] == 1, f"expected one client construction for 5 identical calls, got {calls['n']}"
    assert len(clients) == 1


@pytest.mark.asyncio
async def test_different_base_urls_do_not_share_a_client() -> None:
    """The load-bearing correctness bound: a cache keyed too coarsely passes the reuse test above
    and is a routing bug. Two calls with DIFFERENT base URLs must NOT share a client."""
    factory, calls, clients = _make_factory()
    backend_a = _backend(factory, base_url="http://localhost:1001/v1")
    backend_b = _backend(factory, base_url="http://localhost:1002/v1")

    assert await _call(backend_a) == "ok"
    assert await _call(backend_b) == "ok"

    assert calls["n"] == 2
    assert clients[1] is not clients[0], "different base URLs reused the same client"


@pytest.mark.asyncio
async def test_different_api_keys_do_not_share_a_client() -> None:
    """Same for the API key, which is part of what the factory uses to build the client."""
    factory, calls, clients = _make_factory()
    backend_a = _backend(factory, api_key="key-a")
    backend_b = _backend(factory, api_key="key-b")

    assert await _call(backend_a) == "ok"
    assert await _call(backend_b) == "ok"

    assert calls["n"] == 2
    assert clients[1] is not clients[0], "different API keys reused the same client"


@pytest.mark.asyncio
async def test_reset_builds_a_fresh_client() -> None:
    """Positive control: after the reset function, the next call builds a fresh client."""
    factory, calls, clients = _make_factory()
    backend = _backend(factory)

    assert await _call(backend) == "ok"
    assert calls["n"] == 1

    providers.reset_client_cache()
    assert await _call(backend) == "ok"

    assert calls["n"] == 2
    assert clients[1] is not clients[0], "reset did not force a fresh client"


@pytest.mark.asyncio
async def test_base_url_rebind_invalidates_the_cached_client() -> None:
    """CF-161 interaction: after a base-URL rebind, the next chat completion does not reuse the
    pre-rebind client."""
    factory, calls, clients = _make_factory()
    backend = _backend(factory)

    assert await _call(backend) == "ok"
    pre_rebind_client = clients[0]

    # A graphiti base-URL rebind (which must invalidate the chat-client cache).
    class _Ref:
        client: Any
        config: Any

    ref = _Ref()
    ref.client = object()
    ref.config = SimpleNamespace(base_url="http://old/v1")
    wrapper = GraphitiClient(
        client=object(),
        scheduler_settings=MemorySettings(),
        scheduler_api_key="k",
        llm_base_url="http://old/v1",
        embed_base_url="http://old-embed/v1",
        reranker_base_url="http://old-rerank/v1",
        llm_client_ref=ref,
        embedding_cache=object(),
    )
    wrapper._maybe_update_client_base_url(llm_base_url="http://new/v1")

    assert await _call(backend) == "ok"

    assert calls["n"] == 2
    assert clients[1] is not pre_rebind_client, "rebind did not invalidate the cached chat client"


def test_reset_function_is_named_and_public() -> None:
    """The reset/clear function is `reset_client_cache` and is importable, so rebind paths and
    tests can call it rather than poking the private dict."""
    assert callable(providers.reset_client_cache)
    assert providers.reset_client_cache.__name__ == "reset_client_cache"
