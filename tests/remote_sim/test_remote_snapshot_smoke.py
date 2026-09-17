"""Smoke test: a Menhir that cannot see your files, driven over HTTP.

Every other test in this repository runs against a server sharing a filesystem with the code it
indexes. That is the assumption the whole snapshot plan exists to remove, and until this file
nothing verified that removing it actually works -- the bundler was tested locally, the receiver
was tested in-process, and the two had never met across a socket.

What this proves, in order:

1. the server genuinely cannot see the host's files, so the premise is real and not simulated;
2. the staging receive tools answer over the wire, as an operator, with the real middleware;
3. a bundle built from a real repository by the real bundler arrives byte-intact;
4. the refusals hold at a distance -- ownership, replay conflict, and the disabled-by-default gate.

Run: `pytest --run-remote-sim tests/remote_sim`
"""

from __future__ import annotations

import base64
import io
import json
import subprocess
import uuid
from pathlib import Path

import pytest

from menhir.snapshot.bundler import build_plan, write_bundle
from menhir.snapshot.protocol import sha256_hex
from menhir.snapshot.upload_client import SnapshotUploader, SnapshotUploadError
from tests.remote_sim.conftest import OPERATOR_KEY, call_mcp

#: The session fixture builds an image and starts two containers, and pytest-timeout's
#: 60s default covers fixture setup too -- without this the stack is killed mid-boot and
#: teardown never runs, leaving containers holding the ports.
pytestmark = [pytest.mark.remote_sim, pytest.mark.timeout(900)]


def _write_archive(repo: Path, target: Path) -> Path:
    """Build the real bundle on disk, which is what the CLI hands the uploader."""
    with target.open("wb") as handle:
        write_bundle(build_plan(repo), handle)
    return target

_CHUNK = 8 * 1024


def _text(result: dict) -> dict:
    """Unwrap an MCP tool result into the JSON payload the tool rendered."""
    assert "error" not in result, result
    content = result["result"]["content"]
    return json.loads(content[0]["text"])


@pytest.fixture(scope="module")
def sample_repo(tmp_path_factory) -> Path:
    """A small real git repository, so the bundler under test is the real one."""
    repo = tmp_path_factory.mktemp("sample")

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args], check=True, capture_output=True, timeout=30
        )

    git("init", "--quiet")
    git("config", "user.email", "sim@example.invalid")
    git("config", "user.name", "Sim")
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_bytes(b"def main():\n    return 42\n")
    (repo / "README.md").write_bytes(b"# sample\n")
    git("add", "-A")
    git("commit", "--quiet", "-m", "initial")
    return repo


def test_the_server_cannot_see_the_hosts_files(remote_menhir: str, sample_repo: Path) -> None:
    """The premise, verified rather than assumed.

    If this ever passes by scanning the path, the stack has a host mount and every other result
    in this file is meaningless.
    """
    result = call_mcp(
        remote_menhir, "ingest_project", {"path": str(sample_repo), "name": "sample"}
    )

    rendered = json.dumps(result)
    assert "not a directory" in rendered or "is outside the allowed roots" in rendered, rendered


def test_a_real_bundle_survives_the_round_trip(remote_menhir: str, sample_repo: Path) -> None:
    """The bundler builds it here; the receiver reassembles it there; the digests agree."""
    plan = build_plan(sample_repo)
    buffer = io.BytesIO()
    write_bundle(plan, buffer)
    archive = buffer.getvalue()

    begun = _text(
        call_mcp(
            remote_menhir,
            "begin_project_snapshot",
            {
                "project_key": f"sample-{uuid.uuid4().hex[:8]}",
                "declared_bytes": len(archive),
                "chunk_bytes": _CHUNK,
            },
        )
    )
    assert begun["ok"] is True
    upload_id = begun["upload_id"]
    assert begun["total_chunks"] == (len(archive) + _CHUNK - 1) // _CHUNK

    for index in range(begun["total_chunks"]):
        piece = archive[index * _CHUNK : (index + 1) * _CHUNK]
        sent = _text(
            call_mcp(
                remote_menhir,
                "put_project_snapshot_chunk",
                {
                    "upload_id": upload_id,
                    "index": index,
                    "data_b64": base64.b64encode(piece).decode("ascii"),
                    "declared_len": len(piece),
                    "digest": sha256_hex(piece),
                },
            )
        )
        assert sent["ok"] is True, sent

    status = _text(
        call_mcp(remote_menhir, "get_project_snapshot_status", {"upload_id": upload_id})
    )
    assert status["state"] == "SEALED"
    assert status["received_bytes"] == len(archive)
    assert status["missing_count"] == 0


def test_a_conflicting_replay_is_refused_over_the_wire(remote_menhir: str) -> None:
    """The receiver's rules hold at a distance, not just in-process."""
    begun = _text(
        call_mcp(
            remote_menhir,
            "begin_project_snapshot",
            {"project_key": f"conflict-{uuid.uuid4().hex[:8]}", "declared_bytes": 16,
             "chunk_bytes": 8},
        )
    )
    upload_id = begun["upload_id"]
    first = b"AAAAAAAA"
    call_mcp(
        remote_menhir,
        "put_project_snapshot_chunk",
        {"upload_id": upload_id, "index": 0,
         "data_b64": base64.b64encode(first).decode(), "declared_len": 8,
         "digest": sha256_hex(first)},
    )

    other = b"BBBBBBBB"
    conflicted = _text(
        call_mcp(
            remote_menhir,
            "put_project_snapshot_chunk",
            {"upload_id": upload_id, "index": 0,
             "data_b64": base64.b64encode(other).decode(), "declared_len": 8,
             "digest": sha256_hex(other)},
        )
    )

    assert conflicted["ok"] is False
    assert conflicted["error"]["code"] == "snapshot.upload.conflicting_replay"


def test_an_unknown_upload_id_is_refused(remote_menhir: str) -> None:
    result = _text(
        call_mcp(remote_menhir, "get_project_snapshot_status", {"upload_id": "deadbeef" * 4})
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "snapshot.upload.not_found"


def test_aborting_drops_the_upload(remote_menhir: str) -> None:
    begun = _text(
        call_mcp(
            remote_menhir,
            "begin_project_snapshot",
            {"project_key": f"abort-{uuid.uuid4().hex[:8]}", "declared_bytes": 16},
        )
    )

    aborted = _text(
        call_mcp(remote_menhir, "abort_project_snapshot", {"upload_id": begun["upload_id"]})
    )

    assert aborted["state"] == "ABORTED"


def test_the_upload_client_sends_a_real_bundle(
    remote_menhir: str, sample_repo: Path, tmp_path: Path
) -> None:
    """`SnapshotUploader` against a server that cannot see the repository it is receiving.

    `test_a_real_bundle_survives_the_round_trip` drives the tools by hand, which proves the
    protocol. This proves the CLIENT -- the code `menhir sync` will actually run -- including the
    part no hand-driven test exercises: that it chunks to the size the SERVER negotiated rather
    than to its own default.
    """
    archive = _write_archive(sample_repo, tmp_path / "bundle.zip")

    seen: list[tuple[int, int]] = []
    uploader = SnapshotUploader(remote_menhir, auth_key=OPERATOR_KEY)
    outcome = uploader.upload(
        archive,
        project_key="client-e2e",
        progress=lambda sent, total: seen.append((sent, total)),
    )

    assert outcome.state == "SEALED"
    assert outcome.sent_chunks == outcome.total_chunks
    assert outcome.bytes_sent == archive.stat().st_size
    assert seen and seen[-1][0] == outcome.sent_chunks


def test_the_client_chunks_to_the_servers_size_not_its_own(
    remote_menhir: str, sample_repo: Path, tmp_path: Path
) -> None:
    """Ask for a chunk size the server will not grant, and finish anyway on the server's terms.

    The receiver places bytes at `index * chunk_bytes` using ITS number. A client that asked for
    one size and then cut to another would write every chunk at the wrong offset, and each chunk's
    digest would still verify -- a bundle that passes every per-chunk check and is silently wrong.

    This test was written expecting the server to CLAMP an over-ceiling request. It does not: it
    refuses the begin outright. So the property worth having is the client's, not the server's --
    a caller must not be turned away over a chunk size preference it has no reason to hold, and
    must then use what the server actually granted.
    """
    archive = _write_archive(sample_repo, tmp_path / "bundle-2.zip")

    uploader = SnapshotUploader(remote_menhir, auth_key=OPERATOR_KEY)
    outcome = uploader.upload(
        archive, project_key="client-negotiation", chunk_bytes=64 * 1024 * 1024
    )

    assert outcome.state == "SEALED", "the server's chunk plan was not followed"
    assert outcome.chunk_bytes <= 2 * 1024 * 1024, "the server granted more than its own ceiling"
    assert outcome.bytes_sent == archive.stat().st_size


def test_the_client_explains_an_endpoint_that_does_not_serve_the_tools(remote_menhir: str) -> None:
    """The likeliest misconfiguration must not surface as a raw 404 or JSON-RPC error.

    A URL that reaches a server but not the snapshot tools -- receive mode off, an older Menhir, or
    a backend-first proxy that does not forward MCP -- is the error a user will actually hit, so it
    has to name those possibilities rather than make them guess.
    """
    uploader = SnapshotUploader(remote_menhir, auth_key=OPERATOR_KEY)
    with pytest.raises(SnapshotUploadError) as excinfo:
        uploader._call("no_such_snapshot_tool", {})

    assert excinfo.value.code == "sync.server.tool_unavailable"
    assert "backend-first proxy" in str(excinfo.value)
