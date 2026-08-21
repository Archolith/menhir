"""CF-89: bound the two module-level MCP caches that grew without bound.

1. ``service_access._session_cache`` -- an LRU-bounded memoization of caller sessions.
2. ``contracts._query_add_memory_events`` -- the query-auth ``add_memory`` rate-limit dict,
   which must evict fully-expired keys without ever handing a caller a free reset.

The rate-limit tests drive ``now`` through the ``now`` parameter the limiter already accepts
(no sleeping). The key comes from ``_query_auth_rate_limit_key()`` reading the request
session, so we monkeypatch ``contracts.get_request_session`` to control it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from menhir.mcp import contracts, service_access


def _set_key(monkeypatch, key: str) -> None:
    monkeypatch.setattr(
        contracts,
        "get_request_session",
        lambda: SimpleNamespace(client_id=key, session_id=key, user_id=key),
    )


# ---------------------------------------------------------------------------
# service_access: LRU-bounded session cache
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_session_cache_never_exceeds_bound() -> None:
    bound = service_access._SESSION_CACHE_MAX
    original = service_access._session_cache.copy()
    service_access._session_cache.clear()
    try:
        for i in range(bound * 2):
            service_access._cached_session_for("u", f"s{i}", client_id=f"c{i}", client_name="n")
            assert len(service_access._session_cache) <= bound
        assert len(service_access._session_cache) == bound
    finally:
        service_access._session_cache.clear()
        service_access._session_cache.update(original)


@pytest.mark.unit
def test_session_cache_evicts_least_recently_used() -> None:
    bound = service_access._SESSION_CACHE_MAX
    original = service_access._session_cache.copy()
    service_access._session_cache.clear()
    try:
        for i in range(bound):
            service_access._cached_session_for("u", f"s{i}", client_id=f"c{i}", client_name="n")
        # Re-touch the oldest key (s0) so it is no longer least-recently-used.
        service_access._cached_session_for("u", "s0", client_id="c0", client_name="n")
        # Insert one more; the LRU entry (now s1) must be evicted, s0 must survive.
        service_access._cached_session_for("u", "new", client_id="new", client_name="n")
        assert len(service_access._session_cache) == bound
        assert ("u", "s0", "c0", "n") in service_access._session_cache
        assert ("u", "s1", "c1", "n") not in service_access._session_cache
    finally:
        service_access._session_cache.clear()
        service_access._session_cache.update(original)


@pytest.mark.unit
def test_session_cache_still_returns_same_object_for_same_key() -> None:
    original = service_access._session_cache.copy()
    service_access._session_cache.clear()
    try:
        a = service_access._cached_session_for("u", "s", client_id="c", client_name="n")
        b = service_access._cached_session_for("u", "s", client_id="c", client_name="n")
        assert a is b
    finally:
        service_access._session_cache.clear()
        service_access._session_cache.update(original)


# ---------------------------------------------------------------------------
# contracts: bounded query-auth add_memory rate-limit dict
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_query_add_memory_events_do_not_grow_unbounded(monkeypatch) -> None:
    contracts._query_add_memory_events.clear()
    contracts._query_add_memory_sweep_cursor = 0
    try:
        n = 100
        # Create many distinct keys spread across time.
        for i in range(n):
            _set_key(monkeypatch, f"k{i}")
            contracts._consume_query_add_memory_budget(now=float(i))
        # Advance far enough that every one of those keys' windows fully expires.
        late = float(n - 1) + contracts.QUERY_AUTH_ADD_MEMORY_WINDOW_SECONDS + 1.0
        budget = contracts._QUERY_AUTH_SWEEP_BUDGET
        calls = (n + 1 + budget) // budget + 1
        _set_key(monkeypatch, "final")
        for _ in range(calls):
            contracts._consume_query_add_memory_budget(now=late)
        # All the long-expired keys must have been swept out.
        for i in range(n):
            assert f"k{i}" not in contracts._query_add_memory_events
        assert len(contracts._query_add_memory_events) <= 2
    finally:
        contracts._query_add_memory_events.clear()


@pytest.mark.unit
def test_sweep_does_not_evict_live_rate_limited_key(monkeypatch) -> None:
    """The rate-limit bypass guard: a live key must never be freed by eviction."""
    contracts._query_add_memory_events.clear()
    contracts._query_add_memory_sweep_cursor = 0
    try:
        base = 1000.0
        # Consume the full budget for key "A".
        _set_key(monkeypatch, "A")
        for _ in range(contracts.QUERY_AUTH_ADD_MEMORY_LIMIT):
            contracts._consume_query_add_memory_budget(now=base)
        count, retry = contracts._consume_query_add_memory_budget(now=base + 1.0)
        assert retry > 0

        # Trigger sweeps under many other keys, all within "A"'s window.
        for i in range(200):
            _set_key(monkeypatch, f"B{i}")
            contracts._consume_query_add_memory_budget(now=base + 2.0 + i)

        # "A" is still rate-limited (its window has not expired).
        _set_key(monkeypatch, "A")
        assert "A" in contracts._query_add_memory_events
        count, retry = contracts._consume_query_add_memory_budget(now=base + 250.0)
        assert retry > 0
    finally:
        contracts._query_add_memory_events.clear()


@pytest.mark.unit
def test_fully_expired_key_is_allowed_again(monkeypatch) -> None:
    """Positive control: the limiter still resets once a window fully expires."""
    contracts._query_add_memory_events.clear()
    contracts._query_add_memory_sweep_cursor = 0
    try:
        base = 1000.0
        _set_key(monkeypatch, "A")
        for _ in range(contracts.QUERY_AUTH_ADD_MEMORY_LIMIT):
            contracts._consume_query_add_memory_budget(now=base)
        count, retry = contracts._consume_query_add_memory_budget(
            now=base + contracts.QUERY_AUTH_ADD_MEMORY_WINDOW_SECONDS + 1.0
        )
        assert retry == 0.0
    finally:
        contracts._query_add_memory_events.clear()
