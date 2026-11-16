"""#669: every ingest tool failure is an isError result with the spec'd code, not a returned string."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.config.snapshot_mode import SNAPSHOT_RECEIVE_MODE_ENV
from menhir.mcp.tools.ingest import snapshot_staging
from menhir.mcp.tools.ingest.add_memory import AddMemoryTool
from menhir.mcp.tools.ingest.add_memory_and_track import AddMemoryAndTrackTool
from menhir.mcp.tools.ingest.close_memory import CloseMemoryTool
from menhir.mcp.tools.ingest.delete_memories import DeleteMemoriesTool
from menhir.mcp.tools.ingest.delete_memory import DeleteMemoryTool
from menhir.mcp.tools.ingest.flag_memory import FlagMemoryTool
from menhir.mcp.tools.ingest.ingest_document import IngestDocumentTool
from menhir.mcp.tools.ingest.ingest_project import IngestProjectTool
from menhir.mcp.tools.ingest.promote_memory import PromoteMemoryTool
from menhir.mcp.tools.ingest.review_candidate import ReviewCandidateTool
from menhir.mcp.tools.ingest.unflag_memory import UnflagMemoryTool
from menhir.snapshot.receive import ReceiveError
from menhir.services.project_ingest import ProjectIngestOutcome
from tests.mcp.error_contract import error_of, text_of

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

NODE = "0b8f6a2e-4c1d-4e7a-9f3b-2d5c8e1a7b40"


def _tool(cls, backend):
    tool = cls()
    tool.get_backend = lambda: backend  # type: ignore[method-assign]
    return tool


def _foreign_backend() -> MagicMock:
    """A node that exists, but not in the caller's namespace."""
    backend = MagicMock()

    async def fetch(uuid, namespace=None):
        return None if namespace else {"uuid": uuid}

    backend.fetch_memory_by_uuid = fetch
    return backend


# --- add_memory / add_memory_and_track -----------------------------------------------------------


async def test_add_memory_invalid_role() -> None:
    result = await _tool(AddMemoryTool, MagicMock()).execute(text="x", role="human")
    error_of(result, code="invalid_argument", tool="add_memory")
    assert text_of(result).startswith("Cannot store memory: invalid role")


async def test_add_memory_user_statement_and_turn_evidence_are_exclusive() -> None:
    result = await _tool(AddMemoryTool, MagicMock()).execute(
        text="x", user_statement="words", turn_evidence_uuid=NODE
    )
    error_of(result, code="invalid_argument")
    assert "mutually exclusive" in text_of(result)


async def test_add_memory_bootstrap_scope_requires_flagged() -> None:
    result = await _tool(AddMemoryTool, MagicMock()).execute(text="x", bootstrap_scope="general")
    error_of(result, code="invalid_argument")
    assert text_of(result) == "Cannot store memory: bootstrap_scope requires flagged=true."


async def test_add_memory_and_track_invalid_role() -> None:
    result = await _tool(AddMemoryAndTrackTool, MagicMock()).execute(text="x", role="human")
    error_of(result, code="invalid_argument", tool="add_memory_and_track")
    assert text_of(result).startswith("Cannot store memory: invalid role")


# --- close / flag / unflag / promote -------------------------------------------------------------


async def test_close_memory_outside_namespace() -> None:
    result = await _tool(CloseMemoryTool, _foreign_backend()).execute(uuid=NODE, namespace="a")
    error_of(result, code="outside_namespace")
    assert "exists but is outside namespace a" in text_of(result)


async def test_close_memory_not_found_or_already_completed() -> None:
    backend = MagicMock()
    backend.complete_temporal = AsyncMock(return_value=False)
    result = await _tool(CloseMemoryTool, backend).execute(uuid=NODE)
    error_of(result, code="not_found")
    assert "not found or already completed" in text_of(result)


@pytest.mark.parametrize(
    "cls", [FlagMemoryTool, UnflagMemoryTool, PromoteMemoryTool]
)
async def test_flag_family_outside_namespace(cls) -> None:
    result = await _tool(cls, _foreign_backend()).execute(node_uuid=NODE, namespace="a")
    error_of(result, code="outside_namespace")
    assert text_of(result).startswith("Refused: memory")


async def test_flag_memory_value_error_is_invalid_argument_with_the_real_message() -> None:
    backend = MagicMock()
    backend.flag_memory = AsyncMock(side_effect=ValueError("structural node"))
    result = await _tool(FlagMemoryTool, backend).execute(node_uuid=NODE)
    error_of(result, code="invalid_argument")
    assert text_of(result) == "Cannot flag: structural node"


async def test_flag_memory_not_found() -> None:
    backend = MagicMock()
    backend.flag_memory = AsyncMock(return_value=False)
    result = await _tool(FlagMemoryTool, backend).execute(node_uuid=NODE)
    error_of(result, code="not_found")
    assert text_of(result) == f"No memory found with uuid={NODE}."


async def test_unflag_memory_value_error_is_invalid_argument() -> None:
    backend = MagicMock()
    backend.unflag_memory = AsyncMock(side_effect=ValueError("structural node"))
    result = await _tool(UnflagMemoryTool, backend).execute(node_uuid=NODE)
    error_of(result, code="invalid_argument")
    assert text_of(result) == "Cannot unflag: structural node"


async def test_unflag_memory_not_found() -> None:
    backend = MagicMock()
    backend.unflag_memory = AsyncMock(return_value=False)
    result = await _tool(UnflagMemoryTool, backend).execute(node_uuid=NODE)
    error_of(result, code="not_found")
    assert text_of(result) == f"No memory found with uuid={NODE}."


async def test_promote_memory_not_found() -> None:
    backend = MagicMock()
    backend.promote_memory = AsyncMock(return_value=False)
    result = await _tool(PromoteMemoryTool, backend).execute(node_uuid=NODE)
    error_of(result, code="not_found")
    assert "No PERSISTENT memory found" in text_of(result)


# --- delete_memory -------------------------------------------------------------------------------


async def test_delete_memory_invalid_target() -> None:
    result = await _tool(DeleteMemoryTool, MagicMock()).execute(node_uuid="e")
    error_of(result, code="invalid_argument")
    assert text_of(result).startswith("Refused: 'e' is not a valid memory uuid")


async def test_delete_memory_outside_namespace() -> None:
    result = await _tool(DeleteMemoryTool, _foreign_backend()).execute(
        node_uuid=NODE, namespace="a"
    )
    error_of(result, code="outside_namespace")
    assert "outside namespace a" in text_of(result)


@pytest.mark.parametrize(
    ("outcome", "code", "text"),
    [
        ({"reason": "residual_content_after_purge"}, "write_failed", "did NOT complete"),
        ({"reason": "prepare_failed"}, "write_failed", "did NOT run"),
        ({"reason": "erased_incomplete", "derived": {}}, "write_failed", "INCOMPLETE"),
        ({"reason": "weird"}, "write_failed", "unexpected result"),
        (
            {"reason": "erased_incomplete", "incomplete_reasons": ["foreign_target"]},
            "outside_namespace",
            "belongs to another namespace",
        ),
    ],
)
async def test_delete_memory_failure_outcomes(outcome, code, text) -> None:
    backend = MagicMock()
    backend.fetch_memory_by_uuid = AsyncMock(return_value=None)
    backend.erase_memory = AsyncMock(return_value=outcome)
    result = await _tool(DeleteMemoryTool, backend).execute(node_uuid=NODE)
    error_of(result, code=code)
    assert text in text_of(result)


@pytest.mark.parametrize(
    "outcome",
    [
        {"reason": "erased", "purged": {}},
        {"reason": "graph_already_absent", "purged": {"t": 1}},
        {"reason": "nothing_to_erase"},
    ],
)
async def test_delete_memory_success_outcomes_stay_successes(outcome) -> None:
    backend = MagicMock()
    backend.fetch_memory_by_uuid = AsyncMock(return_value=None)
    backend.erase_memory = AsyncMock(return_value=outcome)
    result = await _tool(DeleteMemoryTool, backend).execute(node_uuid=NODE)
    assert isinstance(result, str)


# --- delete_memories -----------------------------------------------------------------------------


async def test_delete_memories_empty_list() -> None:
    result = await _tool(DeleteMemoriesTool, MagicMock()).execute(node_uuids=[])
    error = error_of(result, code="invalid_argument", tool="delete_memories")
    assert "no ids given; nothing was erased" in error["message"]


async def test_delete_memories_over_the_cap() -> None:
    result = await _tool(DeleteMemoriesTool, MagicMock()).execute(node_uuids=["x"] * 101)
    error = error_of(result, code="invalid_argument")
    assert "101 ids exceeds the limit of 100" in error["message"]
    assert "100" in error["hint"]


async def test_delete_memories_invalid_ids_are_named() -> None:
    result = await _tool(DeleteMemoriesTool, MagicMock()).execute(node_uuids=[NODE, "e"])
    error = error_of(result, code="invalid_argument")
    assert "invalid ids" in error["message"] and "'e'" in error["message"]


# --- review_candidate ----------------------------------------------------------------------------


async def test_review_candidate_invalid_decision() -> None:
    result = await _tool(ReviewCandidateTool, MagicMock()).execute(uuid="c", decision="promote")
    error_of(result, code="invalid_argument")
    assert text_of(result).startswith("Invalid decision 'promote'")


async def test_review_candidate_blank_uuid() -> None:
    result = await _tool(ReviewCandidateTool, MagicMock()).execute(uuid=" ", decision="approve")
    error_of(result, code="invalid_argument")
    assert text_of(result) == "A candidate uuid is required."


async def test_review_candidate_outside_namespace() -> None:
    backend = MagicMock()

    async def fetch_candidate(uuid, namespace=None):
        return None if namespace else {"uuid": uuid}

    backend.fetch_candidate = fetch_candidate
    result = await _tool(ReviewCandidateTool, backend).execute(
        uuid="c", decision="reject", namespace="a"
    )
    error_of(result, code="outside_namespace")
    assert "outside namespace a" in text_of(result)


async def test_review_candidate_missing() -> None:
    backend = MagicMock()
    backend.fetch_candidate = AsyncMock(return_value=None)
    backend.fetch_memory_by_uuid = AsyncMock(return_value=None)
    backend.reject_candidate = AsyncMock(return_value=False)
    result = await _tool(ReviewCandidateTool, backend).execute(uuid="c", decision="reject")
    error_of(result, code="not_found")
    assert text_of(result) == "No candidate awaiting review with uuid=c."


async def test_review_candidate_already_approved() -> None:
    backend = MagicMock()
    backend.fetch_candidate = AsyncMock(return_value=None)
    backend.fetch_memory_by_uuid = AsyncMock(return_value={"uuid": "c"})
    backend.approve_candidate = AsyncMock(return_value={"status": "not_found"})
    result = await _tool(ReviewCandidateTool, backend).execute(uuid="c", decision="approve")
    error_of(result, code="not_found")
    assert "already approved" in text_of(result)


# --- ingest_document / ingest_project ------------------------------------------------------------


async def test_ingest_document_not_a_file() -> None:
    result = await IngestDocumentTool().execute(path="/nonexistent/dir/file.md")
    error_of(result, code="invalid_argument")
    assert text_of(result).startswith("not a file:")


async def test_ingest_document_needs_decision(tmp_path, monkeypatch) -> None:
    doc = tmp_path / "d.md"
    doc.write_text("x", encoding="utf-8")
    decision = {"status": "needs_decision", "actions": ["adopt", "new"]}
    backend = MagicMock()
    backend.ingest_document = AsyncMock(return_value=decision)
    monkeypatch.setattr(
        "menhir.mcp.tools.ingest.ingest_document.get_mcp_session",
        lambda: SimpleNamespace(session_id="s", user_id="u"),
    )
    result = await _tool(IngestDocumentTool, backend).execute(path=str(doc))
    assert result.isError is True
    assert result.structuredContent["error"]["code"] == "needs_decision"
    assert json.loads(text_of(result)) == decision or json.loads(
        result.structuredContent["error"]["message"]
    ) == decision


async def _ingest_project(monkeypatch, outcome: ProjectIngestOutcome):
    monkeypatch.setattr(
        "menhir.mcp.tools.ingest.ingest_project.get_mcp_session",
        lambda: SimpleNamespace(session_id="s", user_id="u"),
    )
    monkeypatch.setattr(
        "menhir.mcp.tools.ingest.ingest_project.execute_project_ingest",
        AsyncMock(return_value=outcome),
    )
    return await _tool(IngestProjectTool, MagicMock()).execute(path="/x")


async def test_ingest_project_error(monkeypatch) -> None:
    result = await _ingest_project(
        monkeypatch, ProjectIngestOutcome(project_name="p", error="not a directory: /x")
    )
    error_of(result, code="invalid_argument")
    assert text_of(result) == "not a directory: /x"


async def test_ingest_project_needs_decision(monkeypatch) -> None:
    result = await _ingest_project(
        monkeypatch,
        ProjectIngestOutcome(
            project_name="p", needs_decision={"reason": "ambiguous", "directory": "/x"}
        ),
    )
    error_of(result, code="needs_decision")
    assert text_of(result).startswith("NOT SCANNED -- p needs an identity decision (ambiguous).")


# --- snapshot staging ----------------------------------------------------------------------------

_STAGING_CALLS = [
    (snapshot_staging.BeginProjectSnapshotTool, {"project_key": "p", "declared_bytes": 1}),
    (
        snapshot_staging.PutProjectSnapshotChunkTool,
        {"upload_id": "u", "index": 0, "data_b64": "", "declared_len": 0, "digest": "d"},
    ),
    (snapshot_staging.GetProjectSnapshotStatusTool, {"upload_id": "u"}),
    (snapshot_staging.AbortProjectSnapshotTool, {"upload_id": "u"}),
]


@pytest.mark.parametrize(("cls", "kwargs"), _STAGING_CALLS)
async def test_snapshot_staging_disabled(monkeypatch, cls, kwargs) -> None:
    monkeypatch.delenv(SNAPSHOT_RECEIVE_MODE_ENV, raising=False)
    result = await cls().execute(**kwargs)
    error = error_of(result, code="disabled")
    assert "snapshot receive is disabled" in error["message"]


@pytest.mark.parametrize(("cls", "kwargs"), _STAGING_CALLS)
async def test_snapshot_staging_receive_error_keeps_its_code(monkeypatch, cls, kwargs) -> None:
    monkeypatch.setenv(SNAPSHOT_RECEIVE_MODE_ENV, "staging")
    monkeypatch.setattr(
        snapshot_staging, "get_mcp_session", lambda: SimpleNamespace(user_id="u", session_id="s")
    )
    boom = MagicMock(side_effect=ReceiveError("snapshot.upload.not_found", "no such upload"))
    receiver = SimpleNamespace(begin=boom, put_chunk=boom, status=boom, abort=boom)
    monkeypatch.setattr(snapshot_staging, "_receiver", lambda: receiver)
    result = await cls().execute(**kwargs)
    error = error_of(result, code="snapshot.upload.not_found")
    assert error["message"] == "no such upload"
