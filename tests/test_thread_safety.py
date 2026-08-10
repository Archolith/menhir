"""Tests for thread safety fixes — session cache lock and flagged bootstrap reads."""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest


class TestSessionCacheLock:
    """Verify _cached_session_for uses thread-safe access."""

    def test_concurrent_session_creation_no_duplicates(self):
        from menhir.mcp import service_access

        # Reset cache for test isolation
        original_cache = service_access._session_cache.copy()
        service_access._session_cache.clear()

        try:
            barrier = threading.Barrier(4)
            results = [None] * 4

            def worker(idx):
                barrier.wait()
                results[idx] = service_access._cached_session_for(
                    "test-user", "sess-1", client_id="c1", client_name="test",
                )

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # All threads should get the same session object
            assert all(r is results[0] for r in results)
            assert len(service_access._session_cache) == 1
        finally:
            service_access._session_cache.clear()
            service_access._session_cache.update(original_cache)


class TestFlaggedBootstrapReadsLock:
    """Verify flagged_bootstrap_reads is thread-safe."""

    def test_remember_and_check_flagged_read(self):
        from menhir.core.runtime import (
            _remember_flagged_bootstrap_read,
            _has_recent_flagged_bootstrap_read,
            _state,
        )

        # Clear for isolation
        with _state._flagged_lock:
            _state.flagged_bootstrap_reads.clear()

        _remember_flagged_bootstrap_read("reader-1", "v1")
        assert _has_recent_flagged_bootstrap_read("reader-1", "v1")
        assert not _has_recent_flagged_bootstrap_read("reader-1", "v2")
        assert not _has_recent_flagged_bootstrap_read("reader-2", "v1")

    def test_receipts_are_isolated_by_workspace(self):
        from menhir.core.runtime import (
            _has_recent_flagged_bootstrap_read,
            _remember_flagged_bootstrap_read,
            _state,
        )

        with _state._flagged_lock:
            _state.flagged_bootstrap_reads.clear()

        _remember_flagged_bootstrap_read("reader-1", "a-v1", workspace="archolith")
        _remember_flagged_bootstrap_read("reader-1", "y-v1", workspace="yawn")

        assert _has_recent_flagged_bootstrap_read(
            "reader-1", "a-v1", workspace="archolith"
        )
        assert _has_recent_flagged_bootstrap_read(
            "reader-1", "y-v1", workspace="yawn"
        )
        assert not _has_recent_flagged_bootstrap_read(
            "reader-1", "a-v1", workspace="yawn"
        )

    def test_concurrent_flagged_writes(self):
        from menhir.core.runtime import (
            _remember_flagged_bootstrap_read,
            _has_recent_flagged_bootstrap_read,
            _state,
        )

        with _state._flagged_lock:
            _state.flagged_bootstrap_reads.clear()

        barrier = threading.Barrier(4)

        def worker(idx):
            barrier.wait()
            _remember_flagged_bootstrap_read(f"reader-{idx}", f"v{idx}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for i in range(4):
            assert _has_recent_flagged_bootstrap_read(f"reader-{i}", f"v{i}")
