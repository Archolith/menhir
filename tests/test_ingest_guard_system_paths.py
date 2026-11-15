"""Host system trees are refused for every ingest tier, operator included.

The operator bypass in `ensure_ingest_path_allowed` covers roots, not secrets: before this,
`/proc/self/environ` and `/run/secrets/*` passed the name checks (which look only at the last two
path components for operator), and ingest_document returned the first 4,000 characters.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from menhir.core.ingest_guard import (
    IngestPathNotAllowedError,
    _is_system_path,
    ensure_ingest_path_allowed,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("path", [
    "/proc/self/environ",
    "/proc/1/environ",
    "/run/secrets/db_password",
    "/var/run/secrets/kubernetes.io/serviceaccount/token",
    "/etc/passwd",
    "/sys/kernel/notes",
    "/dev/zero",
    "/private/etc/hosts",  # macOS resolves /etc here
    "/private/var/run/x",
])
def test_system_paths_are_recognised(path: str) -> None:
    assert _is_system_path(PurePosixPath(path).parts)


@pytest.mark.parametrize("path", [
    "/home/u/project/README.md",
    "/srv/run/notes.md",
    "/tmp/etc/notes.md",
    "/var/lib/menhir/doc.md",
    "/",
    "/private",
])
def test_ordinary_paths_are_not_system_paths(path: str) -> None:
    assert not _is_system_path(PurePosixPath(path).parts)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX system paths")
@pytest.mark.parametrize("tier", ["operator", None, "", "agent"])
def test_proc_environ_is_refused_for_every_tier(tier, monkeypatch) -> None:
    monkeypatch.setenv("MENHIR_INGEST_ALLOWED_ROOTS", "/")
    with pytest.raises(IngestPathNotAllowedError, match="system directory"):
        ensure_ingest_path_allowed("/proc/self/environ", tier=tier)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_symlink_into_a_system_tree_is_refused(tmp_path: Path) -> None:
    link = tmp_path / "notes.md"
    os.symlink("/proc/self/environ", link)
    with pytest.raises(IngestPathNotAllowedError):
        ensure_ingest_path_allowed(str(link), tier="operator")


def test_ordinary_operator_path_still_passes(tmp_path: Path) -> None:
    doc = tmp_path / "notes.md"
    doc.write_text("hello", encoding="utf-8")
    assert ensure_ingest_path_allowed(str(doc), tier="operator") == doc.resolve()


@pytest.mark.asyncio
async def test_ingest_document_refuses_a_path_that_is_not_a_regular_file(tmp_path: Path) -> None:
    from menhir.core.backend_runtime_data_ops import RuntimeProviderDataOpsMixin
    from menhir.mcp.service_access import bind_request_tier, reset_request_tier

    ops = RuntimeProviderDataOpsMixin()
    ops.built = SimpleNamespace(graph_adapter=SimpleNamespace())  # never reached
    token = bind_request_tier("operator")
    try:
        with pytest.raises(IngestPathNotAllowedError, match="not a regular file"):
            await ops.ingest_document(
                str(tmp_path), project="p", session_id="s", user_id="u"
            )
    finally:
        reset_request_tier(token)
