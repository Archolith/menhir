"""#409: outbound LLM connections resolve hostnames once per TTL, off the default executor."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time

import httpcore
import httpx
import openai
import pytest

import menhir.infrastructure.observability as observability
from menhir.config import MemorySettings
from menhir.infrastructure.dns_cache import CachingDNSTransport, CachingResolverBackend


class _Stream(httpcore.AsyncNetworkStream):
    def __init__(self, log: list[tuple[str, object]]) -> None:
        self._log = log

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        raise httpcore.ReadError("fake stream")

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        raise httpcore.WriteError("fake stream: stop after connect")

    async def aclose(self) -> None:
        return None

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None) -> "_Stream":
        self._log.append(("start_tls", server_hostname))
        return self

    def get_extra_info(self, info: str):
        return None


class _Inner(httpcore.AsyncNetworkBackend):
    """Records connect targets; fails the IPs listed in ``dead``."""

    def __init__(self, dead: set[str] | None = None) -> None:
        self.log: list[tuple[str, object]] = []
        self.dead = dead or set()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.log.append(("connect_tcp", host))
        if host in self.dead:
            raise httpcore.ConnectError(f"{host} refused")
        return _Stream(self.log)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise NotImplementedError

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class _CountingResolver:
    def __init__(self, answer: list[str], gate: threading.Event | None = None) -> None:
        self.answer = answer
        self.gate = gate
        self.calls = 0

    def __call__(self, host: str, port: int) -> list[str]:
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(5)
        return list(self.answer)


@pytest.fixture
def executor():
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    yield pool
    pool.shutdown(wait=False, cancel_futures=True)


def _backend(resolver, executor, inner=None, **kw) -> tuple[CachingResolverBackend, _Inner]:
    inner = inner or _Inner()
    return CachingResolverBackend(inner=inner, resolver=resolver, executor=executor, **kw), inner


@pytest.mark.unit
@pytest.mark.asyncio
async def test_second_connection_reuses_the_cached_address(executor) -> None:
    resolver = _CountingResolver(["203.0.113.7"])
    backend, inner = _backend(resolver, executor)

    await backend.connect_tcp("api.example.test", 443, timeout=5)
    await backend.connect_tcp("api.example.test", 443, timeout=5)

    assert resolver.calls == 1
    assert inner.log == [("connect_tcp", "203.0.113.7"), ("connect_tcp", "203.0.113.7")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrent_misses_share_one_lookup(executor) -> None:
    gate = threading.Event()
    resolver = _CountingResolver(["203.0.113.7"], gate=gate)
    backend, inner = _backend(resolver, executor)

    tasks = [asyncio.create_task(backend.connect_tcp("api.example.test", 443, timeout=5)) for _ in range(50)]
    await asyncio.sleep(0.05)
    gate.set()
    await asyncio.gather(*tasks)

    assert resolver.calls == 1
    assert len(inner.log) == 50


@pytest.mark.unit
@pytest.mark.asyncio
async def test_saturated_default_executor_does_not_delay_resolution(executor) -> None:
    """The #409 failure: every default-executor worker busy with blocking graph I/O."""
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, lambda: None)
    workers = loop._default_executor._max_workers  # noqa: SLF001
    release = threading.Event()
    blockers = [loop.run_in_executor(None, release.wait, 10) for _ in range(workers)]
    try:
        backend, inner = _backend(_CountingResolver(["203.0.113.7"]), executor)
        started = time.monotonic()
        await backend.connect_tcp("api.example.test", 443, timeout=5)
        assert time.monotonic() - started < 1.0
        assert inner.log == [("connect_tcp", "203.0.113.7")]
    finally:
        release.set()
        await asyncio.gather(*blockers)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_entry_expires_after_the_ttl(executor) -> None:
    now = [1000.0]
    resolver = _CountingResolver(["203.0.113.7"])
    backend, _ = _backend(resolver, executor, ttl_s=300, clock=lambda: now[0])

    await backend.connect_tcp("api.example.test", 443, timeout=5)
    now[0] += 299
    await backend.connect_tcp("api.example.test", 443, timeout=5)
    assert resolver.calls == 1
    now[0] += 2
    await backend.connect_tcp("api.example.test", 443, timeout=5)
    assert resolver.calls == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dead_address_fails_over_to_the_next(executor) -> None:
    resolver = _CountingResolver(["203.0.113.7", "203.0.113.8"])
    backend, inner = _backend(resolver, executor, inner=_Inner(dead={"203.0.113.7"}))

    await backend.connect_tcp("api.example.test", 443, timeout=5)

    assert inner.log == [("connect_tcp", "203.0.113.7"), ("connect_tcp", "203.0.113.8")]
    assert resolver.calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_addresses_dead_raises_and_re_resolves_next_time(executor) -> None:
    resolver = _CountingResolver(["203.0.113.7"])
    backend, inner = _backend(resolver, executor, inner=_Inner(dead={"203.0.113.7"}))

    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("api.example.test", 443, timeout=5)
    inner.dead.clear()
    await backend.connect_tcp("api.example.test", 443, timeout=5)

    assert resolver.calls == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolution_failure_is_a_connect_error_and_is_not_cached(executor) -> None:
    calls = []

    def failing(host: str, port: int) -> list[str]:
        calls.append(host)
        raise OSError("name not known")

    backend, _ = _backend(failing, executor)
    for _ in range(2):
        with pytest.raises(httpcore.ConnectError):
            await backend.connect_tcp("api.example.test", 443, timeout=5)
    assert len(calls) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_one_callers_timeout_does_not_cancel_the_shared_lookup(executor) -> None:
    gate = threading.Event()
    resolver = _CountingResolver(["203.0.113.7"], gate=gate)
    backend, inner = _backend(resolver, executor)

    patient = asyncio.create_task(backend.connect_tcp("api.example.test", 443, timeout=5))
    await asyncio.sleep(0.02)
    with pytest.raises(httpcore.ConnectTimeout):
        await backend.connect_tcp("api.example.test", 443, timeout=0.05)
    gate.set()
    await patient

    assert resolver.calls == 1
    assert inner.log == [("connect_tcp", "203.0.113.7")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ip_literal_is_never_resolved(executor) -> None:
    resolver = _CountingResolver(["203.0.113.7"])
    backend, inner = _backend(resolver, executor)

    await backend.connect_tcp("127.0.0.1", 8080, timeout=5)

    assert resolver.calls == 0
    assert inner.log == [("connect_tcp", "127.0.0.1")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tls_still_verifies_the_hostname_not_the_ip(executor) -> None:
    resolver = _CountingResolver(["203.0.113.7"])
    backend, inner = _backend(resolver, executor)

    async with httpx.AsyncClient(transport=CachingDNSTransport(backend=backend)) as client:
        with pytest.raises(httpx.TransportError):
            await client.get("https://api.example.test/v1/models")

    assert inner.log[:2] == [("connect_tcp", "203.0.113.7"), ("start_tls", "api.example.test")]


@pytest.mark.unit
def test_transport_installs_the_backend_on_the_direct_pool() -> None:
    """Pins the private httpcore attribute CachingDNSTransport sets (see its docstring)."""
    backend = CachingResolverBackend()
    transport = CachingDNSTransport(backend=backend)

    assert type(transport._pool) is httpcore.AsyncConnectionPool  # noqa: SLF001
    assert transport._pool._network_backend is backend  # noqa: SLF001


@pytest.mark.unit
def test_remote_openai_client_keeps_sdk_defaults_and_uses_the_cache() -> None:
    http_client = observability._build_http_client("https://api.openai.com/v1")  # noqa: SLF001

    assert isinstance(http_client, openai.DefaultAsyncHttpxClient)
    assert http_client.timeout == openai.DEFAULT_TIMEOUT
    assert isinstance(http_client._transport, CachingDNSTransport)  # noqa: SLF001
    pool = http_client._transport._pool  # noqa: SLF001
    assert pool._max_connections == openai.DEFAULT_CONNECTION_LIMITS.max_connections  # noqa: SLF001
    assert pool._max_keepalive_connections == openai.DEFAULT_CONNECTION_LIMITS.max_keepalive_connections  # noqa: SLF001
    assert isinstance(pool._network_backend, CachingResolverBackend)  # noqa: SLF001


@pytest.mark.unit
def test_every_built_client_shares_one_resolver_cache() -> None:
    a = observability.build_async_openai_client(base_url="https://api.openai.com/v1", api_key="k", settings=MemorySettings())
    b = observability.build_async_openai_client(base_url="http://localhost:1234/v1", api_key="k", settings=MemorySettings())

    def backend_of(client) -> object:
        return client._client._transport._pool._network_backend  # noqa: SLF001  (SDK's httpx client)

    assert backend_of(a) is backend_of(b)
