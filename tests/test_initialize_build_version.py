"""#498: MCP ``initialize`` names the build a connection was opened against."""

from __future__ import annotations

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import menhir
from menhir.api.mcp_remote import create_mcp_streamable_http_app, create_remote_tool_only_mcp
from menhir.mcp.served_metadata import served_server_version

pytestmark = pytest.mark.unit

SHA = "43e693f6b8218fb5bd3d89d0354d52e77c5ade3b"


def _stamped(factory) -> str:
    server = factory()
    if isinstance(server, tuple):
        server = server[1]
    return server._mcp_server.create_initialization_options().server_version


@pytest.mark.parametrize("factory", [create_remote_tool_only_mcp, create_mcp_streamable_http_app])
def test_release_build_reports_version_plus_commit(monkeypatch, factory) -> None:
    monkeypatch.setenv("MENHIR_RELEASE_COMMIT", SHA)
    assert _stamped(factory) == f"{menhir.__version__}+{SHA[:12]}"


@pytest.mark.parametrize("value", [None, "", "unknown", "not-a-sha", "43e693f6;rm -rf /", "ABCDEF0"])
def test_without_a_valid_commit_the_version_is_plain(monkeypatch, value) -> None:
    if value is None:
        monkeypatch.delenv("MENHIR_RELEASE_COMMIT", raising=False)
    else:
        monkeypatch.setenv("MENHIR_RELEASE_COMMIT", value)
    assert _stamped(create_remote_tool_only_mcp) == menhir.__version__


def test_stamped_version_still_parses_as_the_release_version(monkeypatch) -> None:
    # Clients may read serverInfo.version as a version number; the build part must not change it.
    monkeypatch.setenv("MENHIR_RELEASE_COMMIT", SHA)
    stamped = served_server_version("1.2.3")
    assert stamped == "1.2.3+43e693f6b821"
    # SemVer build metadata: one "+", the release version unchanged before it.
    base, build = stamped.split("+")
    assert base == "1.2.3" and build.isalnum()


def test_release_record_lists_every_snapshot_mode(monkeypatch, capsys) -> None:
    import asyncio
    import json

    from menhir.config.snapshot_mode import SNAPSHOT_RECEIVE_MODE_ENV, SnapshotReceiveMode
    from menhir.mcp import served_metadata

    monkeypatch.setenv("MENHIR_RELEASE_COMMIT", SHA)
    monkeypatch.setenv(SNAPSHOT_RECEIVE_MODE_ENV, "receive")
    assert served_metadata.main([]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["schema"] == 1
    assert record["version"] == menhir.__version__
    assert record["commit"] == SHA
    assert set(record["mcp_metadata_sha256"]) == {mode.value for mode in SnapshotReceiveMode}
    # The caller's mode is restored, and each entry is what a server in that mode serves.
    assert served_metadata.os.environ[SNAPSHOT_RECEIVE_MODE_ENV] == "receive"
    for mode, digest in record["mcp_metadata_sha256"].items():
        monkeypatch.setenv(SNAPSHOT_RECEIVE_MODE_ENV, mode)
        assert asyncio.run(served_metadata.compute_metadata()).sha256 == digest


@pytest.mark.asyncio
async def test_initialize_handshake_carries_the_build(monkeypatch) -> None:
    monkeypatch.setenv("MENHIR_RELEASE_COMMIT", SHA)
    server = create_remote_tool_only_mcp()
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        result = await client.initialize()
    assert result.serverInfo.name == "menhir"
    assert result.serverInfo.version == f"{menhir.__version__}+{SHA[:12]}"
