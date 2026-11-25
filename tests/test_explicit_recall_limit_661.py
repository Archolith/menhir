"""#661: explicit recall limits on recall_context_memories and against an older server."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import menhir.mcp.tools.recall.recall_context_memories as rcm
from menhir.core.backend_client_ops import BackendClientOpsMixin
from tests.mcp.error_contract import error_of, text_of

pytestmark = pytest.mark.asyncio


def _context_tool(monkeypatch, note=None):
    backend = MagicMock()
    backend.fetch_flagged_memory_bootstrap_version = AsyncMock(return_value="v1")
    backend.recall = AsyncMock(return_value={"results": [], **({"note": note} if note else {})})
    backend.fetch_recent_memories = AsyncMock(return_value=[])
    backend.list_todos = AsyncMock(return_value=[])
    monkeypatch.setattr(rcm, "_has_recent_flagged_bootstrap_read", lambda *a, **k: True)
    monkeypatch.setattr(rcm, "session_start_warning_lines", AsyncMock(return_value=[]))
    monkeypatch.setattr(rcm, "namespace_note", AsyncMock(return_value=None))
    tool = rcm.RecallContextMemoriesTool()
    tool.get_backend = lambda: backend  # type: ignore[method-assign]
    return tool, backend


@pytest.mark.parametrize("params", [{}, {"limit": 5}, {"limit": 20}])
async def test_context_recall_marks_explicit_limit(monkeypatch, params):
    tool, backend = _context_tool(monkeypatch)
    await tool.execute(reader_id="bot", query="q", **params)
    kwargs = backend.recall.await_args.kwargs
    assert kwargs["limit"] == params.get("limit", 5)
    assert kwargs.get("limit_is_explicit", False) is bool(params)


async def test_context_recall_surfaces_the_cap_note(monkeypatch):
    note = "Reranking capped the default result limit at 3."
    tool, _ = _context_tool(monkeypatch, note=note)
    payload = json.loads(await tool.execute(reader_id="bot", query="q"))
    assert payload["relevant_note"] == note


async def test_context_recall_refuses_limit_below_one(monkeypatch):
    tool, backend = _context_tool(monkeypatch)
    out = await tool.execute(reader_id="bot", query="q", limit=0)
    error_of(out, code="invalid_argument", tool="recall_context_memories")
    assert "limit must be at least 1" in text_of(out)
    backend.recall.assert_not_awaited()


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://menhir/api/internal/backend/recall")
    return httpx.HTTPStatusError("err", request=request, response=httpx.Response(code, request=request))


def _client(responses):
    client = BackendClientOpsMixin()
    calls: list[dict] = []

    async def request(operation, params):
        calls.append(dict(params))
        outcome = responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    client._request = request  # type: ignore[attr-defined]
    return client, calls


async def test_older_server_rejecting_the_flag_gets_a_retry_without_it():
    client, calls = _client([_status_error(500), {"results": [], "note": "n"}])
    result = await client.recall("q", limit=20, limit_is_explicit=True)
    assert calls[0]["limit_is_explicit"] is True
    assert "limit_is_explicit" not in calls[1] and calls[1]["limit"] == 20
    assert result["note"].startswith("n | ") and "older than this client" in result["note"]


async def test_a_real_server_error_still_raises_after_one_retry():
    client, calls = _client([_status_error(500), _status_error(500)])
    with pytest.raises(httpx.HTTPStatusError):
        await client.recall("q", limit=20, limit_is_explicit=True)
    assert len(calls) == 2


@pytest.mark.parametrize("explicit, code", [(False, 500), (True, 400), (True, 503)])
async def test_no_retry_without_the_flag_or_on_other_statuses(explicit, code):
    client, calls = _client([_status_error(code)])
    with pytest.raises(httpx.HTTPStatusError):
        await client.recall("q", limit=20, limit_is_explicit=explicit)
    assert len(calls) == 1


async def test_a_current_server_gets_no_skew_note():
    client, _ = _client([{"results": []}])
    result = await client.recall("q", limit=20, limit_is_explicit=True)
    assert "note" not in result
