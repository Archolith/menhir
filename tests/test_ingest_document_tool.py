"""Unit tests for the IngestDocumentTool MCP tool endpoint."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool(backend: MagicMock) -> "IngestDocumentTool":  # noqa: F821
    from menhir.mcp.tools.ingest.ingest_document import IngestDocumentTool

    tool = IngestDocumentTool()
    tool.get_backend = MagicMock(return_value=backend)
    return tool


def _mock_session() -> MagicMock:
    session = MagicMock()
    session.session_id = "sess-1"
    session.user_id = "user-1"
    return session


# ---------------------------------------------------------------------------
# File validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rejects_nonexistent_file() -> None:
    from menhir.mcp.tools.ingest.ingest_document import IngestDocumentTool

    tool = IngestDocumentTool()
    result = asyncio.run(tool.endpoint(path="/nonexistent/file.md"))

    assert result.startswith("Error: not a file:")


# ---------------------------------------------------------------------------
# Successful ingest
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_successful_ingest_with_episode(tmp_path) -> None:
    doc = tmp_path / "notes.md"
    doc.write_text("# My Notes\nSome content here.", encoding="utf-8")

    backend = MagicMock()
    backend.ingest_document = AsyncMock(return_value={
        "entity_written": True,
        "structure_project": "myproject",
        "structure_path": str(doc.resolve()),
        "content_length": 30,
        "narrative": 'Document "notes.md" (myproject):\n\n# My Notes\nSome content here.',
    })
    backend.queue_episode = AsyncMock(return_value={"episode_id": "ep-123"})

    tool = _make_tool(backend)
    with patch("menhir.mcp.tools.ingest.ingest_document.get_mcp_session", return_value=_mock_session()):
        result = asyncio.run(tool.endpoint(path=str(doc), project="myproject"))

    assert "Ingested notes.md" in result
    assert "episode_id=ep-123" in result
    assert "myproject" in result
    assert "content_length=30" in result
    backend.ingest_document.assert_awaited_once()
    backend.queue_episode.assert_awaited_once()


@pytest.mark.unit
def test_ingest_passes_project_and_session(tmp_path) -> None:
    doc = tmp_path / "readme.txt"
    doc.write_text("hello", encoding="utf-8")

    backend = MagicMock()
    backend.ingest_document = AsyncMock(return_value={
        "entity_written": True,
        "structure_project": "proj",
        "structure_path": str(doc.resolve()),
        "content_length": 5,
        "narrative": "hello",
    })
    backend.queue_episode = AsyncMock(return_value={"episode_id": "ep-1"})

    tool = _make_tool(backend)
    with patch("menhir.mcp.tools.ingest.ingest_document.get_mcp_session", return_value=_mock_session()):
        asyncio.run(tool.endpoint(path=str(doc), project="proj"))

    call_kwargs = backend.ingest_document.call_args
    assert call_kwargs.kwargs["project"] == "proj"
    assert call_kwargs.kwargs["session_id"] == "sess-1"
    assert call_kwargs.kwargs["user_id"] == "user-1"


@pytest.mark.unit
def test_ingest_forwards_identity_decision_fields(tmp_path) -> None:
    doc = tmp_path / "readme.txt"
    doc.write_text("hello", encoding="utf-8")

    backend = MagicMock()
    backend.ingest_document = AsyncMock(return_value={
        "entity_written": True,
        "structure_project": "proj",
        "structure_path": str(doc.resolve()),
        "content_length": 5,
        "narrative": "hello",
    })
    backend.queue_episode = AsyncMock(return_value={"episode_id": "ep-1"})

    tool = _make_tool(backend)
    with patch("menhir.mcp.tools.ingest.ingest_document.get_mcp_session", return_value=_mock_session()):
        asyncio.run(
            tool.endpoint(
                path=str(doc), project="proj", identity_action="adopt",
                adopt_project_id="project-id-1",
            )
        )

    kwargs = backend.ingest_document.call_args.kwargs
    assert kwargs["identity_action"] == "adopt"
    assert kwargs["adopt_project_id"] == "project-id-1"


@pytest.mark.unit
def test_needs_decision_is_rendered_without_episode_queue_eligibility(tmp_path) -> None:
    doc = tmp_path / "ambiguous.md"
    doc.write_text("must not become narrative", encoding="utf-8")

    decision = {
        "status": "needs_decision",
        "decision_type": "project_identity",
        "actions": ["adopt", "new"],
        "candidates": [{"project_id": "existing-id", "display_name": "proj"}],
    }
    backend = MagicMock()
    backend.ingest_document = AsyncMock(return_value=decision)
    backend.queue_episode = AsyncMock()

    tool = _make_tool(backend)
    with patch("menhir.mcp.tools.ingest.ingest_document.get_mcp_session", return_value=_mock_session()):
        result = asyncio.run(tool.endpoint(path=str(doc), project="proj"))

    assert '"status": "needs_decision"' in result
    assert '"decision_type": "project_identity"' in result
    assert '"project_id": "existing-id"' in result
    backend.queue_episode.assert_not_awaited()


# ---------------------------------------------------------------------------
# Empty file
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_empty_file_skips_episode(tmp_path) -> None:
    doc = tmp_path / "empty.md"
    doc.write_text("", encoding="utf-8")

    backend = MagicMock()
    backend.ingest_document = AsyncMock(return_value={
        "entity_written": True,
        "structure_project": "proj",
        "structure_path": str(doc.resolve()),
        "content_length": 0,
        "narrative": "",
    })

    tool = _make_tool(backend)
    with patch("menhir.mcp.tools.ingest.ingest_document.get_mcp_session", return_value=_mock_session()):
        result = asyncio.run(tool.endpoint(path=str(doc)))

    assert "no content (empty file)" in result
    backend.queue_episode = AsyncMock()
    backend.queue_episode.assert_not_awaited()


# ---------------------------------------------------------------------------
# Episode queue timeout
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_episode_queue_timeout(tmp_path) -> None:
    doc = tmp_path / "big.md"
    doc.write_text("content", encoding="utf-8")

    backend = MagicMock()
    backend.ingest_document = AsyncMock(return_value={
        "entity_written": True,
        "structure_project": "proj",
        "structure_path": str(doc.resolve()),
        "content_length": 7,
        "narrative": "content",
    })

    async def slow_queue(*args, **kwargs):
        await asyncio.sleep(999)

    backend.queue_episode = slow_queue

    tool = _make_tool(backend)
    with patch("menhir.mcp.tools.ingest.ingest_document.get_mcp_session", return_value=_mock_session()):
        result = asyncio.run(tool.endpoint(path=str(doc)))

    assert "deferred" in result


# ---------------------------------------------------------------------------
# Episode queue error
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_episode_queue_error(tmp_path) -> None:
    doc = tmp_path / "fail.md"
    doc.write_text("content", encoding="utf-8")

    backend = MagicMock()
    backend.ingest_document = AsyncMock(return_value={
        "entity_written": True,
        "structure_project": "proj",
        "structure_path": str(doc.resolve()),
        "content_length": 7,
        "narrative": "content",
    })
    backend.queue_episode = AsyncMock(side_effect=RuntimeError("neo4j down"))

    tool = _make_tool(backend)
    with patch("menhir.mcp.tools.ingest.ingest_document.get_mcp_session", return_value=_mock_session()):
        result = asyncio.run(tool.endpoint(path=str(doc)))

    assert "episode queue error" in result
    assert "neo4j down" in result
