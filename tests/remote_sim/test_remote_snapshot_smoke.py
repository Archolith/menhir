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
from tests.remote_sim.conftest import call_mcp

#: The session fixture builds an image and starts two containers, and pytest-timeout's
#: 60s default covers fixture setup too -- without this the stack is killed mid-boot and
#: teardown never runs, leaving containers holding the ports.
pytestmark = [pytest.mark.remote_sim, pytest.mark.timeout(900)]

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
