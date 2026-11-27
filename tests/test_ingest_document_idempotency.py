"""ingest_document: honest timeout, stable idempotency key, replayed FAILED episode."""

from __future__ import annotations

import asyncio
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

NARRATIVE = 'Document "notes.md" (proj):\n\nsome content'


def _session() -> MagicMock:
    session = MagicMock()
    session.session_id = "sess-1"
    session.user_id = "user-1"
    return session


def _run(tmp_path, queue_episode) -> str:
    from menhir.mcp.tools.ingest.ingest_document import IngestDocumentTool

    doc = tmp_path / "notes.md"
    doc.write_text("some content", encoding="utf-8")
    backend = MagicMock()
    backend.ingest_document = AsyncMock(return_value={
        "structure_project": "proj",
        "structure_path": str(doc.resolve()),
        "content_length": 12,
        "narrative": NARRATIVE,
    })
    backend.queue_episode = queue_episode
    tool = IngestDocumentTool()
    tool.get_backend = MagicMock(return_value=backend)
    with patch(
        "menhir.mcp.tools.ingest.ingest_document.get_mcp_session", return_value=_session()
    ):
        return asyncio.run(tool.endpoint(path=str(doc), project="proj"))


@pytest.mark.unit
def test_timeout_reports_outcome_unknown_not_deferred(tmp_path, monkeypatch) -> None:
    real_wait_for = asyncio.wait_for

    async def short_wait_for(aw, timeout=None):
        return await real_wait_for(aw, timeout=min(timeout, 0.05))

    monkeypatch.setattr(asyncio, "wait_for", short_wait_for)

    async def slow_queue(*args, **kwargs):
        await asyncio.sleep(999)

    result = _run(tmp_path, slow_queue)

    assert "outcome unknown" in result
    assert "may already be saved" in result
    assert "re-running" in result.lower()
    assert "deferred" not in result


@pytest.mark.unit
def test_idempotency_key_is_passed_and_stable_across_calls(tmp_path) -> None:
    queue = AsyncMock(return_value={"episode_id": "ep-1"})

    _run(tmp_path, queue)
    _run(tmp_path, queue)

    keys = [call.kwargs.get("idempotency_key") for call in queue.await_args_list]
    assert keys[0] == keys[1]
    assert keys[0] == "document-ingest:" + hashlib.sha256(NARRATIVE.encode("utf-8")).hexdigest()
    # Nothing time-varying may feed the request digest.
    assert all("occurred_at" not in call.kwargs for call in queue.await_args_list)


@pytest.mark.unit
def test_replayed_failed_episode_is_reported_failed_with_id(tmp_path) -> None:
    queue = AsyncMock(return_value={
        "episode_id": "ep-failed",
        "idempotent_replay": True,
        "processing_state": "FAILED",
    })

    result = _run(tmp_path, queue)

    assert "episode queue failed" in result
    assert "ep-failed" in result
    assert "episode_id=ep-failed" in result
    assert "force_reenrich" in result
