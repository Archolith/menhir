"""Upload-client guards that no integration test can reach.

A sampled mutation run against the Docker lane settled a question I had asserted rather than
proven: of four `upload_client.py` mutations, the lane killed two and **two survived it**. So the
"its tests live in the Docker lane" caveat was half right. These are the other half.

Both survivors guard against a reply a REAL Menhir never sends -- an empty body, or a payload that
is not an object. `tests/remote_sim` talks to an actual server, so it cannot produce either, and no
amount of integration testing will reach them. They need a fake transport, which is the honest
division of labour: the lane proves the client works against Menhir, these prove it behaves when
the thing on the other end is not one.

That matters because the client's URL is operator-supplied. Pointed at a proxy, a load balancer
error page, or a different service entirely, these are the paths that run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Self

import pytest

from menhir.snapshot.upload_client import SnapshotUploader, SnapshotUploadError


def _uploader() -> SnapshotUploader:
    return SnapshotUploader("https://example.invalid", auth_key="k")


class _FakeResponse:
    """The minimum `urlopen` contract `_call` uses: a context manager with `.read()`."""

    def __init__(self, raw: str) -> None:
        self._raw = raw
        self.status = 200

    def read(self) -> bytes:
        return self._raw.encode("utf-8")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _reply(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """Make the client's `urlopen` return `raw` as a 200 body.

    Patched at `urlopen` rather than at a seam on the class, because `_call` opens the URL
    directly -- there is no injection point. That is worth noting: the client is testable here only
    because `urllib` is patchable, and a seam would be better if this file grows.
    """
    monkeypatch.setattr(
        "menhir.snapshot.upload_client.urllib.request.urlopen",
        lambda _request, timeout=None: _FakeResponse(raw),
    )


def test_an_empty_body_is_refused_rather_than_read_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 200 with nothing in it.

    A proxy that terminates a connection early, or a service that answers health checks and
    nothing else, produces exactly this. Returning it as a parsed result would make an upload
    appear to progress against a server that said nothing at all.
    """
    uploader = _uploader()
    _reply(monkeypatch, "")

    with pytest.raises(SnapshotUploadError) as excinfo:
        uploader._call("begin_project_snapshot", {})

    assert excinfo.value.code == "sync.server.unreadable_reply"


def test_a_body_that_is_not_json_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """An HTML error page is the common case, and it arrives with a 200 more often than it should."""
    uploader = _uploader()
    _reply(monkeypatch, "<html><body>502 Bad Gateway</body></html>")

    with pytest.raises(SnapshotUploadError) as excinfo:
        uploader._call("begin_project_snapshot", {})

    assert excinfo.value.code == "sync.server.unreadable_reply"


def test_a_payload_that_is_not_an_object_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid JSON, valid MCP envelope, and the tool's own body is a bare string.

    This is the guard the Docker lane cannot reach: Menhir's tools always render an object. A
    different MCP server, or a future tool that returns text, would land here -- and the client
    must refuse rather than call `.get()` on a string and fail with an AttributeError the caller
    cannot interpret.
    """
    uploader = _uploader()
    envelope = {"result": {"content": [{"text": json.dumps("just a string")}]}}
    _reply(monkeypatch, json.dumps(envelope))

    with pytest.raises(SnapshotUploadError) as excinfo:
        uploader._call("begin_project_snapshot", {})

    assert excinfo.value.code == "sync.server.unreadable_reply"


def test_a_structured_refusal_still_carries_its_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative control: these guards must not be swallowing legitimate replies.

    A test that only asserts malformed bodies are refused would also pass if every reply were
    refused, which would break the client entirely while looking like good coverage.
    """
    uploader = _uploader()
    body = {
        "ok": False,
        "error": {"code": "snapshot.upload.not_found", "message": "gone"},
    }
    envelope = {"result": {"content": [{"text": json.dumps(body)}]}}
    _reply(monkeypatch, json.dumps(envelope))

    with pytest.raises(SnapshotUploadError) as excinfo:
        uploader._call("get_project_snapshot_status", {"upload_id": "x"})

    assert excinfo.value.code == "snapshot.upload.not_found"


def test_a_well_formed_reply_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the control: a good reply must come back intact."""
    uploader = _uploader()
    body = {"ok": True, "upload_id": "abc", "chunk_bytes": 1024, "total_chunks": 2}
    envelope = {"result": {"content": [{"text": json.dumps(body)}]}}
    _reply(monkeypatch, json.dumps(envelope))

    payload = uploader._call("begin_project_snapshot", {})

    assert payload["upload_id"] == "abc"
    assert payload["chunk_bytes"] == 1024


def test_upload_explicitly_commits_and_returns_the_terminal_server_receipt(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "bundle.zip"
    archive.write_bytes(b"abcdefgh")
    uploader = _uploader()
    calls: list[str] = []

    def call(tool: str, arguments: dict, **_kwargs):
        calls.append(tool)
        if tool == "begin_project_snapshot":
            return {
                "upload_id": "abc",
                "chunk_bytes": 4,
                "total_chunks": 2,
                "commit_required": True,
            }
        if tool == "get_project_snapshot_status":
            return {"state": "SEALED"}
        if tool == "commit_project_snapshot":
            return {
                "state": "READY",
                "project_id": "project-1",
                "snapshot_id": "snapshot-1",
                "result": {"stage": "published", "generation": 2},
            }
        return {"state": "RECEIVING"}

    uploader._call = call  # type: ignore[method-assign]
    result = uploader.upload(archive, project_key="project")

    assert calls == [
        "begin_project_snapshot",
        "put_project_snapshot_chunk",
        "put_project_snapshot_chunk",
        "get_project_snapshot_status",
        "commit_project_snapshot",
    ]
    assert result.state == "READY"
    assert result.result == {"stage": "published", "generation": 2}


def test_precommit_server_is_refused_before_source_bytes_are_sent(tmp_path: Path) -> None:
    archive = tmp_path / "bundle.zip"
    archive.write_bytes(b"abcdefgh")
    uploader = _uploader()
    calls: list[str] = []

    def call(tool: str, arguments: dict, **_kwargs):
        calls.append(tool)
        if tool == "begin_project_snapshot":
            return {"upload_id": "old", "chunk_bytes": 4, "total_chunks": 2}
        if tool == "abort_project_snapshot":
            return {"state": "ABORTED"}
        raise AssertionError(f"unexpected tool call: {tool}")

    uploader._call = call  # type: ignore[method-assign]

    with pytest.raises(SnapshotUploadError) as excinfo:
        uploader.upload(archive, project_key="project")

    assert excinfo.value.code == "sync.server.commit_unavailable"
    assert calls == ["begin_project_snapshot", "abort_project_snapshot"]
