"""#671: one namespace check for writes, and a note on reads of a namespace that holds nothing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.mcp.namespace_note import namespace_note
from menhir.mcp.tools.ingest.add_candidate import AddCandidateTool
from menhir.mcp.tools.ingest.add_memory import AddMemoryTool
from menhir.mcp.tools.ingest.add_memory_and_track import AddMemoryAndTrackTool
from menhir.mcp.tools.ingest.ingest_document import IngestDocumentTool
from menhir.mcp.tools.ingest.ingest_project import IngestProjectTool
from tests.mcp.error_contract import error_of, text_of

pytestmark = [pytest.mark.unit]

_WRITERS = [
    (AddMemoryTool, {"text": "t"}, "Cannot store memory:"),
    (AddMemoryAndTrackTool, {"text": "t"}, "Cannot store memory:"),
    (AddCandidateTool, {"content": "c", "source": "s", "cluster_id": "k", "label": "l"}, "Cannot stage candidate:"),
    (IngestDocumentTool, {"path": "README.md"}, "Cannot ingest document:"),
    (IngestProjectTool, {"path": "."}, "Cannot ingest project:"),
]


def _tool(cls):
    backend = MagicMock()
    tool = cls()
    tool.get_backend = lambda: backend  # type: ignore[method-assign]
    return tool, backend


@pytest.mark.asyncio
@pytest.mark.parametrize(("cls", "kwargs", "prefix"), _WRITERS, ids=lambda v: getattr(v, "name", ""))
async def test_every_writer_refuses_a_trailing_space_before_touching_the_backend(cls, kwargs, prefix) -> None:
    """add_memory_and_track queued `pfcc ` and failed later in the worker; now every writer refuses."""
    tool, backend = _tool(cls)
    out = await tool.execute(**kwargs, namespace="pfcc ")
    error = error_of(out, code="invalid_argument")
    assert text_of(out).startswith(prefix)
    assert "surrounding whitespace" in error["message"] and "'pfcc'" in error["message"]
    assert not backend.mock_calls


@pytest.mark.asyncio
@pytest.mark.parametrize(("cls", "kwargs", "prefix"), _WRITERS, ids=lambda v: getattr(v, "name", ""))
async def test_every_writer_refuses_a_namespace_that_cannot_be_a_group_id(cls, kwargs, prefix) -> None:
    tool, backend = _tool(cls)
    out = await tool.execute(**kwargs, namespace="workspace:menhir")
    error_of(out, code="invalid_argument")
    assert text_of(out).startswith(prefix)
    assert not backend.mock_calls


@pytest.mark.parametrize("namespace", ["", "default", "pfcc", "PFCC", "a-b_9"])
def test_valid_spellings_pass_the_write_check(namespace: str) -> None:
    """A case variant is a valid, separate namespace on write; only reads get a note about it."""
    from menhir.domain.namespace import namespace_group_id_error

    assert namespace_group_id_error(namespace) is None


def _backend(status: dict | Exception) -> MagicMock:
    backend = MagicMock()
    if isinstance(status, Exception):
        backend.get_namespace_status = AsyncMock(side_effect=status)
    else:
        backend.get_namespace_status = AsyncMock(return_value=status)
    return backend


@pytest.mark.asyncio
@pytest.mark.parametrize("namespace", ["", "default", None])
async def test_the_default_namespace_never_gets_a_note_or_a_probe(namespace) -> None:
    backend = _backend({"known": False, "similar": []})
    assert await namespace_note(backend, namespace) is None
    backend.get_namespace_status.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("namespace", ["PFCC", "pfcc "])
async def test_a_case_or_whitespace_variant_gets_a_note_naming_the_stored_spelling(namespace) -> None:
    note = await namespace_note(_backend({"known": False, "similar": ["pfcc"]}), namespace)
    assert note is not None
    assert repr(namespace) in note and "not evidence of no history" in note
    assert "Did you mean 'pfcc'?" in note


@pytest.mark.asyncio
async def test_a_known_namespace_gets_no_note() -> None:
    assert await namespace_note(_backend({"known": True, "similar": []}), "pfcc") is None


@pytest.mark.asyncio
async def test_an_unknown_namespace_without_similar_spellings_still_gets_a_note() -> None:
    note = await namespace_note(_backend({"known": False, "similar": []}), "nosuch")
    assert note is not None and "Did you mean" not in note


@pytest.mark.asyncio
async def test_an_older_server_without_the_probe_means_no_note_not_a_failure() -> None:
    assert await namespace_note(_backend(RuntimeError("404 unknown op")), "pfcc") is None


@pytest.mark.asyncio
async def test_the_note_names_the_namespace_the_server_applied() -> None:
    """A pinned client asking about 'foo' is answered about its pin; the note must say so."""
    note = await namespace_note(_backend({"namespace": "bar", "known": False, "similar": []}), "foo")
    assert note is not None and "'bar'" in note and "'foo'" not in note


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pin", "expected_ns", "expected_similar"),
    [("", "PFCC", True), ("tenant-a", "tenant-a", False)],
    ids=["unpinned", "pinned"],
)
async def test_get_namespace_status_forces_the_pin_and_hides_other_names(
    pin, expected_ns, expected_similar
) -> None:
    """Removing `pin or` or `include_similar=not pin` would let a pinned client probe and list
    other silos' names."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from menhir.core.backend_runtime import RuntimeProvider
    from menhir.domain.session import new_session

    adapter = MagicMock()
    adapter.namespace_status = MagicMock(return_value={"known": False, "similar": []})
    runtime = RuntimeProvider(SimpleNamespace(graph_adapter=adapter), new_session("u"))
    with patch("menhir.core.backend_runtime_data_ops.pinned_namespace", return_value=pin):
        await runtime.get_namespace_status("PFCC")
    adapter.namespace_status.assert_called_once_with(
        expected_ns, include_similar=expected_similar
    )
