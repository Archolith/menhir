"""Tests for Neo4jRepository — retry logic and thread-safe driver init."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from menhir.infrastructure.neo4j import Neo4jRepository, _TRANSIENT_RETRIES


def _make_repo(**overrides):
    defaults = dict(uri="bolt://localhost:7687", database="test", user="neo4j", password="pw")
    defaults.update(overrides)
    return Neo4jRepository(**defaults)


class TestDriverDoubleLocking:
    """Verify only one driver is created under concurrent access."""

    def test_single_thread_creates_one_driver(self):
        repo = _make_repo()
        mock_driver = MagicMock()
        with patch("menhir.infrastructure.neo4j.GraphDatabase") as gdb:
            gdb.driver.return_value = mock_driver
            d1 = repo._get_driver()
            d2 = repo._get_driver()
        assert d1 is d2
        gdb.driver.assert_called_once()

    def test_concurrent_threads_create_one_driver(self):
        repo = _make_repo()
        mock_driver = MagicMock()
        call_count = 0
        barrier = threading.Barrier(4)

        original_driver = None

        def slow_driver(*args, **kwargs):
            nonlocal call_count, original_driver
            call_count += 1
            time.sleep(0.05)
            if original_driver is None:
                original_driver = mock_driver
            return original_driver

        with patch("menhir.infrastructure.neo4j.GraphDatabase") as gdb:
            gdb.driver.side_effect = slow_driver
            results = [None] * 4

            def worker(idx):
                barrier.wait()
                results[idx] = repo._get_driver()

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # Double-checked locking: only one call should succeed
        assert call_count == 1
        assert all(r is mock_driver for r in results)


class TestExecuteRetry:
    """Verify transient error retry with backoff in execute()."""

    def test_succeeds_on_first_try(self):
        repo = _make_repo()
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_record = MagicMock()
        mock_record.data.return_value = {"x": 1}
        mock_result.__iter__ = MagicMock(return_value=iter([mock_record]))
        mock_session.run.return_value = mock_result
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_driver = MagicMock()
        mock_driver.session.return_value = mock_session
        repo._driver = mock_driver

        rows = repo.execute("RETURN 1 AS x")
        assert rows == [{"x": 1}]

    def test_retries_on_transient_error(self):
        repo = _make_repo()

        mock_session_fail = MagicMock()
        mock_session_ok = MagicMock()
        mock_result = MagicMock()
        mock_record = MagicMock()
        mock_record.data.return_value = {"ok": True}
        mock_result.__iter__ = MagicMock(return_value=iter([mock_record]))
        mock_session_ok.run.return_value = mock_result
        mock_session_ok.__enter__ = MagicMock(return_value=mock_session_ok)
        mock_session_ok.__exit__ = MagicMock(return_value=False)

        # Create a real-looking transient exception
        class FakeTransientError(Exception):
            pass

        call_count = 0

        def mock_session_factory(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                ctx = MagicMock()
                ctx.__enter__ = MagicMock(return_value=ctx)
                ctx.__exit__ = MagicMock(return_value=False)
                ctx.run.side_effect = FakeTransientError("Connection lost")
                return ctx
            return mock_session_ok

        mock_driver = MagicMock()
        mock_driver.session.side_effect = mock_session_factory
        repo._driver = mock_driver

        # Patch the exception classes to include our fake
        with patch("menhir.infrastructure.neo4j.ServiceUnavailable", FakeTransientError):
            with patch("menhir.infrastructure.neo4j._TRANSIENT_BACKOFF_BASE", 0.01):
                rows = repo.execute("RETURN 1")

        assert rows == [{"ok": True}]
        assert call_count == 2

    def test_exhausts_retries_then_raises(self):
        repo = _make_repo()

        class FakeTransientError(Exception):
            pass

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.run.side_effect = FakeTransientError("down")

        mock_driver = MagicMock()
        mock_driver.session.return_value = mock_session
        repo._driver = mock_driver

        with patch("menhir.infrastructure.neo4j.ServiceUnavailable", FakeTransientError):
            with patch("menhir.infrastructure.neo4j._TRANSIENT_BACKOFF_BASE", 0.01):
                with pytest.raises(FakeTransientError, match="down"):
                    repo.execute("RETURN 1")

        assert mock_session.run.call_count == _TRANSIENT_RETRIES

    def test_non_transient_error_not_retried(self):
        repo = _make_repo()
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.run.side_effect = ValueError("bad query")

        mock_driver = MagicMock()
        mock_driver.session.return_value = mock_session
        repo._driver = mock_driver

        with pytest.raises(ValueError, match="bad query"):
            repo.execute("BAD")

        mock_session.run.assert_called_once()
