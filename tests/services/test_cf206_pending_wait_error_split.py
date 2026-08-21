"""CF-206: the pending-episode degradation handler must re-raise programming errors.

The degradation is deliberate: a slow or unavailable graph must not fail recall. But a
`TypeError`/`AttributeError`/`NameError`/`KeyError` from a call whose signature we control is a
bug, not an outage -- a stale `StubMemoryGraphAdapter` signature once got swallowed into
`assert 0 == 1` instead of surfacing the real cause. These pin the split: programming errors
propagate, everything else still degrades.
"""

from __future__ import annotations

import logging

import pytest

from menhir.domain.recall import RecallResult
from menhir.services.recall_pipeline import run_recall

pytestmark = pytest.mark.unit


class _Client:
    async def search_scored(self, query, num_results, group_ids):
        return []


class _Service:
    def __init__(self, pending_raises=None, pending_result=None):
        self.client = _Client()
        self.pending_raises = pending_raises
        self.pending_result = pending_result if pending_result is not None else ([], [])
        self.fallback_rows = None

    async def _wait_for_pending_episodes(self, query, limit, timeout_s, *, namespace=None):
        if self.pending_raises is not None:
            raise self.pending_raises
        return self.pending_result

    def _pending_fallback_results(self, rows, preset, limit):
        self.fallback_rows = rows
        return [("fallback", rows)]


@pytest.mark.asyncio
async def test_programming_error_propagates_out_of_run_recall():
    """The finding: a TypeError from the pending-episode seam is a bug, not an outage, so it must
    NOT be swallowed into a degraded recall."""
    service = _Service(pending_raises=TypeError("stale stub signature"))
    with pytest.raises(TypeError, match="stale stub signature"):
        await run_recall(service, "some query", wait_for_pending=True)


@pytest.mark.asyncio
async def test_infrastructure_failure_still_degrades(caplog):
    """POSITIVE CONTROL: a non-programming failure keeps the degradation -- run_recall continues
    and returns a normal result, logging the continue message."""
    service = _Service(pending_raises=RuntimeError("graph timeout"))
    with caplog.at_level(logging.WARNING, logger="menhir.services.recall_pipeline"):
        result = await run_recall(service, "some query", wait_for_pending=True)
    assert isinstance(result, RecallResult)
    assert result.results == []
    assert "continuing with normal recall" in caplog.text


@pytest.mark.asyncio
async def test_no_exception_pending_rows_still_used():
    """POSITIVE CONTROL: when no error is raised, pending rows returned normally are still used."""
    pending_row = {"uuid": "ep-1", "processing_state": "PENDING", "content": "x"}
    service = _Service(pending_result=([pending_row], ["ep-1"]))
    result = await run_recall(service, "some query", wait_for_pending=True)
    assert isinstance(result, RecallResult)
    assert service.fallback_rows == [pending_row]
