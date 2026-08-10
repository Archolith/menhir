"""Minimal, dependency-free in-process rate limiting for the embedded OAuth AS.

A fixed-window counter keyed by a caller key (client IP). It is sufficient for the
single-host VPS target (AS-002 / AS-004); a distributed/shared limiter is explicitly out
of scope. Safe under the ASGI event loop and Starlette's threadpool: all mutable state is
guarded by a ``threading.Lock``.

Env knobs (per-IP), with safe defaults:
  * ``MENHIR_OAUTH_AS_REGISTER_RATE`` / ``MENHIR_OAUTH_AS_REGISTER_WINDOW_S``
    — DCR ``/oauth/register`` throttle (default 20 per 600s).
  * ``MENHIR_OAUTH_AS_APPROVE_RATE`` / ``MENHIR_OAUTH_AS_APPROVE_WINDOW_S``
    — failed-``/oauth/authorize``-POST throttle (default 10 per 300s).
"""

from __future__ import annotations

import threading
import time

from menhir.config import MemorySettings
from menhir.config.oauth import _as_bool, _as_tuple, _get_setting

# Hard cap on the number of distinct keys tracked at once. Bounds memory (and the
# per-call cost) under a many-distinct-IP flood within a single window, where
# window-based pruning alone frees nothing because every entry is still current.
_MAX_TRACKED_KEYS = 4096


class FixedWindowLimiter:
    """Per-key fixed-window counter. ``allow(key)`` returns ``False`` once more than
    ``max_per_window`` calls land in the current ``window_s`` window for that key."""

    def __init__(self, *, max_per_window: int, window_s: int) -> None:
        self._max = max(1, int(max_per_window))
        self._window_s = max(1, int(window_s))
        self._lock = threading.Lock()
        # key -> (window_index, count_in_window). Insertion-ordered (dict), so the
        # oldest-inserted key is ``next(iter(self._state))`` for FIFO eviction.
        self._state: dict[str, tuple[int, int]] = {}

    def _window_index(self, now: float) -> int:
        return int(now // self._window_s)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        window = self._window_index(now)
        with self._lock:
            state = self._state
            existing = state.get(key)
            if existing is None:
                # New key: enforce the hard cap before inserting. Drop entries
                # from expired windows first; if still at the cap (distinct-key
                # flood within one window), evict oldest-inserted keys. Failing
                # open for an evicted key is acceptable — the DCR client-table
                # cap is the real backstop, and a distinct-IP flood needs a
                # genuine botnet since X-Forwarded-For is not trusted by default.
                if len(state) >= _MAX_TRACKED_KEYS:
                    for stale_key in [k for k, v in state.items() if v[0] != window]:
                        del state[stale_key]
                    while len(state) >= _MAX_TRACKED_KEYS:
                        state.pop(next(iter(state)))
                state[key] = (window, 1)
                return 1 <= self._max
            prev_window, count = existing
            count = count + 1 if prev_window == window else 1
            state[key] = (window, count)
            return count <= self._max


def _settings_or_env(settings: object | None) -> object:
    return settings if settings is not None else MemorySettings.from_env()


def _trusted_proxy_enabled(settings: object | None = None) -> bool:
    """True when the operator asserts a trusted reverse proxy fronts Menhir."""
    resolved = _settings_or_env(settings)
    return _as_bool(
        _get_setting(resolved, "trusted_proxy", "MENHIR_TRUSTED_PROXY", False)
    )


def _forwarded_for_last_hop(request: object) -> str:
    """Return the last ``X-Forwarded-For`` hop, i.e. the IP the local proxy saw.

    A trusted proxy (nginx ``$proxy_add_x_forwarded_for``) appends the peer it
    observed to the end of the list, so the LAST entry is the real client the
    proxy connected to — even if the client forged earlier (leftmost) entries.
    """
    headers = getattr(request, "headers", None)
    getter = getattr(headers, "get", None)
    raw = getter("x-forwarded-for") if callable(getter) else None
    if not raw:
        return ""
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    return parts[-1] if parts else ""


def _peer_ip(request: object) -> str:
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) if client is not None else None
    return str(host) if host else "unknown"


def client_ip(request: object, settings: object | None = None) -> str:
    """Best-effort caller key for rate limiting: the peer socket address.

    Deliberately does NOT trust ``X-Forwarded-For`` by default — that header is
    caller-controlled and would let an attacker rotate the rate-limit key at will.
    Behind a same-host reverse proxy the peer is always the proxy (e.g. loopback),
    which collapses every external caller onto one shared key and turns the per-IP
    windows global. When (and only when) an operator asserts a trusted proxy via
    ``MENHIR_TRUSTED_PROXY=1``, resolve the real client from the last forwarded
    hop the proxy appended (RL-001).
    """
    resolved_settings = _settings_or_env(settings)
    peer = _peer_ip(request)
    trusted_peers = _as_tuple(
        _get_setting(
            resolved_settings,
            "trusted_proxy_peers",
            "MENHIR_TRUSTED_PROXY_PEERS",
            ("127.0.0.1", "::1"),
        )
    )
    if _trusted_proxy_enabled(resolved_settings) and peer in trusted_peers:
        forwarded = _forwarded_for_last_hop(request)
        if forwarded:
            return forwarded
    return peer


def build_register_limiter(settings: object | None = None) -> FixedWindowLimiter:
    resolved = _settings_or_env(settings)
    return FixedWindowLimiter(
        max_per_window=int(getattr(resolved, "oauth_as_register_rate", 20)),
        window_s=int(getattr(resolved, "oauth_as_register_window_s", 600)),
    )


def build_approve_limiter(settings: object | None = None) -> FixedWindowLimiter:
    resolved = _settings_or_env(settings)
    return FixedWindowLimiter(
        max_per_window=int(getattr(resolved, "oauth_as_approve_rate", 10)),
        window_s=int(getattr(resolved, "oauth_as_approve_window_s", 300)),
    )
