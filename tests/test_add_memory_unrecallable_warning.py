"""`add_memory` must tell its caller when writes are being dropped.

`add_memory` returns `state=PENDING, queued` in milliseconds; enrichment runs 30-100s
later. If it fails, the episode keeps its content but gets no `:Entity` nodes, and recall
searches entities -- so the memory is unrecallable while the caller was already told the
write succeeded. Nothing ever closed that loop, which is why 196 dropped writes went
unnoticed for eight months.

A `PostToolUse` host hook cannot fix this: it fires while the episode is still PENDING, so
it would report PENDING every time. The signal has to come from the write's own response,
where it also reaches every client rather than only the harness that installed a hook.
"""

from __future__ import annotations

import pytest

from menhir.mcp import formatters
from menhir.mcp.formatters import _queue_summary, _standing_unrecallable_count


class _Backend:
    """Runtime-shaped backend (no get_queue_depth => the sync graph_adapter path)."""

    def __init__(self, failed_count: int = 0) -> None:
        self.overview_calls = 0
        self._failed_count = failed_count
        outer = self

        class _Adapter:
            def fetch_memory_overview(self, namespace=None) -> dict[str, object]:
                outer.overview_calls += 1
                return {"failed_count": outer._failed_count}

            def list_episode_processing(self, processing_states=None, limit=200):
                return []

        self.graph_adapter = _Adapter()

        class _Ingest:
            def get_queue_depth(self) -> int:
                return 0

        self.ingest_service = _Ingest()


@pytest.fixture(autouse=True)
def _clear_cache():
    formatters._stuck_count_cache = None
    yield
    formatters._stuck_count_cache = None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_queue_summary_warns_when_writes_are_being_dropped() -> None:
    summary = await _queue_summary(_Backend(failed_count=196))

    assert "queue_depth=" in summary            # existing snapshot preserved
    assert "WARNING: 196 earlier memories are FAILED and NOT recallable" in summary
    assert "get_enrichment_status" in summary   # names the way to check this write


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clean_system_adds_no_noise() -> None:
    summary = await _queue_summary(_Backend(failed_count=0))

    assert "WARNING" not in summary
    assert summary.count("\n") == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_singular_wording_for_one_failure() -> None:
    summary = await _queue_summary(_Backend(failed_count=1))

    assert "1 earlier memory is FAILED" in summary


@pytest.mark.unit
@pytest.mark.asyncio
async def test_count_is_cached_so_add_memory_stays_fast() -> None:
    """add_memory is a hot path; the backlog moves slowly. One query, not one per write."""
    backend = _Backend(failed_count=5)

    for _ in range(10):
        await _standing_unrecallable_count(backend)

    assert backend.overview_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_counting_failure_never_breaks_the_write() -> None:
    class _Broken(_Backend):
        def __init__(self) -> None:
            super().__init__()

            class _Adapter:
                def fetch_memory_overview(self, namespace=None):
                    raise RuntimeError("neo4j down")

                def list_episode_processing(self, processing_states=None, limit=200):
                    return []

            self.graph_adapter = _Adapter()

    summary = await _queue_summary(_Broken())

    assert "queue_depth=" in summary  # the write still reports successfully
    assert "WARNING" not in summary


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_backend_path_is_supported() -> None:
    """The remote-client shape (async get_queue_depth) must warn too, not just the runtime."""

    class _HttpBackend:
        async def get_queue_depth(self) -> int:
            return 0

        async def list_episode_processing(self, states=None, limit=200):
            return []

        async def scheduler_status_snapshot(self):
            return {"running": True}

        async def fetch_memory_overview(self, namespace=None) -> dict[str, object]:
            return {"failed_count": 3}

    summary = await _queue_summary(_HttpBackend())

    assert "scheduler=running" in summary
    assert "3 earlier memories are FAILED" in summary
