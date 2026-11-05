"""Resolve each outbound LLM host once per TTL instead of once per connection (#409).

asyncio resolves hostnames on the loop's default executor, the same pool the sync Neo4j
driver's ``asyncio.to_thread`` calls run on. When graph I/O holds every worker, each new
connection's lookup queues behind it and the connect times out. This backend resolves on a
short-lived thread of its own, caches the addresses, and connects to an IP. TLS is unchanged: httpcore
passes the request's hostname to ``start_tls`` as ``server_hostname``, so SNI and certificate
checks still use the name, not the IP.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import ipaddress
import logging
import socket
import threading
import time
from typing import Any, Callable, Iterable

import httpcore
import httpx

logger = logging.getLogger(__name__)

#: How long resolved addresses are reused. A failed connect to every cached address also
#: drops the entry, so a moved endpoint is re-resolved without waiting out the TTL.
DNS_CACHE_TTL_S = 300.0

Resolver = Callable[[str, int], list[str]]


def system_resolve(host: str, port: int) -> list[str]:
    """getaddrinfo in the system's preference order, one entry per distinct IP."""
    seen: set[str] = set()
    addresses: list[str] = []
    for *_, sockaddr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        ip = str(sockaddr[0])
        if ip not in seen:
            seen.add(ip)
            addresses.append(ip)
    return addresses


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _resolve_on_own_thread(resolver: Resolver, host: str, port: int) -> concurrent.futures.Future[list[str]]:
    """Run one lookup on a short-lived thread, so it never queues behind graph I/O on the
    default executor. The thread runs in a copy of the caller's context, like asyncio.to_thread
    (CF-79). Callers coalesce misses per (host, port), so there is at most one thread per key."""
    future: concurrent.futures.Future[list[str]] = concurrent.futures.Future()
    context = contextvars.copy_context()

    def _run() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            result = context.run(resolver, host, port)
        except BaseException as exc:  # noqa: BLE001 -- delivered to the awaiting caller
            future.set_exception(exc)
        else:
            future.set_result(result)

    threading.Thread(target=_run, name=f"menhir-dns-{host}", daemon=True).start()
    return future


class CachingResolverBackend(httpcore.AsyncNetworkBackend):
    """httpcore network backend that caches name resolution and connects to the cached IPs."""

    def __init__(
        self,
        *,
        ttl_s: float = DNS_CACHE_TTL_S,
        inner: httpcore.AsyncNetworkBackend | None = None,
        resolver: Resolver = system_resolve,
        clock: Callable[[], float] = time.monotonic,
        executor: concurrent.futures.Executor | None = None,
    ) -> None:
        self._ttl_s = ttl_s
        self._inner = inner if inner is not None else httpcore.AnyIOBackend()
        self._resolver = resolver
        self._clock = clock
        self._executor = executor
        # Reentrant: a lookup that finishes before add_done_callback runs settles inline, under the lock.
        self._lock = threading.RLock()
        self._cache: dict[tuple[str, int], tuple[float, tuple[str, ...]]] = {}
        self._inflight: dict[tuple[str, int], concurrent.futures.Future[list[str]]] = {}

    def invalidate(self, host: str, port: int) -> None:
        with self._lock:
            self._cache.pop((host, port), None)

    def _settle(self, key: tuple[str, int], future: concurrent.futures.Future[list[str]]) -> None:
        with self._lock:
            if self._inflight.get(key) is future:
                del self._inflight[key]
            if future.cancelled() or future.exception() is not None:
                return
            addresses = tuple(future.result())
            if addresses:
                self._cache[key] = (self._clock() + self._ttl_s, addresses)

    async def _addresses(self, host: str, port: int, timeout_s: float | None) -> tuple[str, ...]:
        key = (host, port)
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and hit[0] > self._clock():
                return hit[1]
            future = self._inflight.get(key)
            if future is None:
                # One lookup per key at a time; concurrent misses all wait on it.
                if self._executor is not None:
                    future = self._executor.submit(self._resolver, host, port)
                else:
                    future = _resolve_on_own_thread(self._resolver, host, port)
                self._inflight[key] = future
                future.add_done_callback(lambda f, key=key: self._settle(key, f))
        # shield: one caller's timeout must not cancel the lookup other callers share.
        waiter = asyncio.shield(asyncio.wrap_future(future))
        try:
            addresses = await (waiter if timeout_s is None else asyncio.wait_for(waiter, timeout_s))
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise httpcore.ConnectTimeout(f"DNS resolution of {host} timed out") from exc
        except OSError as exc:
            raise httpcore.ConnectError(f"DNS resolution of {host} failed: {exc}") from exc
        if not addresses:
            raise httpcore.ConnectError(f"DNS resolution of {host} returned no addresses")
        return tuple(addresses)

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 -- httpcore backend interface
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            asyncio.get_running_loop()
        except RuntimeError:  # not asyncio (e.g. trio): keep the stock behaviour
            return await self._inner.connect_tcp(
                host, port, timeout=timeout, local_address=local_address, socket_options=socket_options
            )
        if _is_ip_literal(host):
            return await self._inner.connect_tcp(
                host, port, timeout=timeout, local_address=local_address, socket_options=socket_options
            )
        deadline = None if timeout is None else time.monotonic() + timeout
        addresses = await self._addresses(host, port, timeout)
        last_exc: Exception | None = None
        for index, ip in enumerate(addresses):
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                break
            # Split what is left across the untried addresses so one dead address cannot
            # spend the whole connect timeout.
            attempt_timeout = None if remaining is None else remaining / (len(addresses) - index)
            try:
                return await self._inner.connect_tcp(
                    ip, port, timeout=attempt_timeout, local_address=local_address, socket_options=socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_exc = exc
        self.invalidate(host, port)
        logger.warning("connect to %s:%s failed on every cached address %s; re-resolving next time", host, port, addresses)
        if last_exc is not None:
            raise last_exc
        raise httpcore.ConnectTimeout(f"connect to {host}:{port} timed out")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 -- httpcore backend interface
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        return await self._inner.connect_unix_socket(path, timeout=timeout, socket_options=socket_options)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


_shared_backend_lock = threading.Lock()
_shared_backend: CachingResolverBackend | None = None


def shared_resolver_backend() -> CachingResolverBackend:
    """One process-wide backend, so every LLM client shares one cache and one lookup per host."""
    global _shared_backend
    with _shared_backend_lock:
        if _shared_backend is None:
            _shared_backend = CachingResolverBackend()
        return _shared_backend


class CachingDNSTransport(httpx.AsyncHTTPTransport):
    """httpx transport whose direct connection pool resolves through ``CachingResolverBackend``.

    httpx takes no network backend, so the pool it built gets one after construction. A test
    pins the private attribute (``test_dns_cache.py``) so an httpx/httpcore upgrade that renames
    it fails loudly instead of silently resolving per connection again.
    """

    def __init__(self, *, backend: CachingResolverBackend | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        pool = self._pool
        if type(pool) is httpcore.AsyncConnectionPool:
            if not hasattr(pool, "_network_backend"):
                raise RuntimeError("httpcore.AsyncConnectionPool has no _network_backend; update dns_cache")
            pool._network_backend = backend if backend is not None else shared_resolver_backend()
