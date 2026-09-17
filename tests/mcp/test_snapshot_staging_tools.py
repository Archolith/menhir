"""P2A staging tools: off by default, operator-only, and telemetry-silent about content.

Plan: `.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md` (P2A).

The gate is "adversarial bundles can consume only configured resources; no archive is extracted
and no graph operation is reachable". The receiver's own tests cover resource bounds; these cover
the surface: that nobody can reach it by accident, that turning it on is an operator act, and
that using it does not leak source content into telemetry.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from menhir.config.snapshot_mode import (
    SnapshotReceiveMode,
    resolve_snapshot_receive_mode,
)
from menhir.mcp.contracts import assert_tool_scopes_declared, validate_tool_metadata
from menhir.mcp.tools import ALL_TOOLS, registered_tools
from menhir.mcp.tools.ingest.snapshot_staging import (
    SNAPSHOT_RECEIVE_MODE_ENV,
    SNAPSHOT_STAGING_TOOLS,
    BeginProjectSnapshotTool,
    GetProjectSnapshotStatusTool,
    PutProjectSnapshotChunkTool,
    staging_enabled,
)
from menhir.snapshot.protocol import sha256_hex

pytestmark = pytest.mark.unit


@pytest.fixture
def staging_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(SNAPSHOT_RECEIVE_MODE_ENV, "staging")
    monkeypatch.setenv("MENHIR_STATE_DIR", str(tmp_path / "state"))


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch):
    class _Session:
        user_id = "operator-1"
        session_id = "s1"

    monkeypatch.setattr(
        "menhir.mcp.tools.ingest.snapshot_staging.get_mcp_session", lambda: _Session()
    )
    return _Session()


# --- off by default -----------------------------------------------------------------------------


def test_the_mode_is_off_unless_it_names_a_real_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """P2B widened this contract on purpose.

    In P2A only the literal `staging` enabled the tools, and this test asserted that `receive`
    did NOT. `receive` is now a real mode, so that assertion had to change -- the behaviour it
    pinned was the phase's, not a safety property. What it must still pin is the safety property:
    anything that does not name a mode resolves to OFF rather than failing open.
    """
    monkeypatch.delenv(SNAPSHOT_RECEIVE_MODE_ENV, raising=False)
    assert staging_enabled() is False

    enabling = {"receive", "shadow", "write", "staging", "  Receive ", "WRITE"}
    for value in ("", "on", "yes", "true", "1", "off", "recieve", "receive-all", *enabling):
        monkeypatch.setenv(SNAPSHOT_RECEIVE_MODE_ENV, value)
        expected = value in enabling
        assert staging_enabled() is expected, f"{value!r} resolved the wrong way"


def test_every_mode_above_off_accepts_uploads_and_only_write_reaches_the_graph() -> None:
    """The ladder, pinned where it is declared rather than at each call site.

    `extracts_archives` and `writes_graph` have no implementation yet -- P3 and P4 own those. They
    are asserted now because the guards that will consult them get written against this contract,
    and a mode whose capabilities are decided later is how `receive` acquires one by accident.
    """
    assert SnapshotReceiveMode.OFF.accepts_uploads is False
    assert all(
        m.accepts_uploads
        for m in (SnapshotReceiveMode.RECEIVE, SnapshotReceiveMode.SHADOW, SnapshotReceiveMode.WRITE)
    )

    assert SnapshotReceiveMode.RECEIVE.extracts_archives is False
    assert SnapshotReceiveMode.SHADOW.extracts_archives is True
    assert SnapshotReceiveMode.WRITE.extracts_archives is True

    assert [m for m in SnapshotReceiveMode if m.writes_graph] == [SnapshotReceiveMode.WRITE]


def test_the_legacy_staging_spelling_still_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """P2A's value must not silently become OFF.

    An operator instance or a compose file written against P2A sets `staging`. Resolving that to
    OFF would look like the tools vanishing after an upgrade, with nothing in the config obviously
    wrong -- the worst kind of failure to diagnose.
    """
    assert resolve_snapshot_receive_mode("staging") is SnapshotReceiveMode.RECEIVE
    monkeypatch.setenv(SNAPSHOT_RECEIVE_MODE_ENV, "staging")
    assert staging_enabled() is True


def test_the_tools_are_not_registered_while_the_mode_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither advertised nor invocable: the cheapest way to guarantee both is not to register."""
    monkeypatch.delenv(SNAPSHOT_RECEIVE_MODE_ENV, raising=False)

    names = {cls.name for cls in registered_tools()}

    for cls in SNAPSHOT_STAGING_TOOLS:
        assert cls.name not in names
    assert all(cls not in ALL_TOOLS for cls in SNAPSHOT_STAGING_TOOLS)


def test_the_tools_are_registered_when_staging_is_on(staging_on: None) -> None:
    names = {cls.name for cls in registered_tools()}
    for cls in SNAPSHOT_STAGING_TOOLS:
        assert cls.name in names


async def test_an_endpoint_refuses_at_call_time_even_holding_an_instance(
    monkeypatch: pytest.MonkeyPatch, session
) -> None:
    """Registration is a process-start fact; this is a runtime one. Both must refuse."""
    monkeypatch.delenv(SNAPSHOT_RECEIVE_MODE_ENV, raising=False)

    with pytest.raises(RuntimeError) as excinfo:
        await BeginProjectSnapshotTool().endpoint(project_key="p", declared_bytes=16)

    assert "disabled" in str(excinfo.value)


# --- posture ------------------------------------------------------------------------------------


def test_every_staging_tool_is_operator_tier() -> None:
    """Turning the mode on is an operator decision, and so is using it."""
    for cls in SNAPSHOT_STAGING_TOOLS:
        assert cls.required_tier == "operator"
        assert cls.oauth_scopes == ("menhir:admin",)


def test_staging_tools_pass_the_same_metadata_and_tenancy_validation(staging_on: None) -> None:
    """Validated unconditionally so a broken tool cannot hide behind the feature flag."""
    assert_tool_scopes_declared(SNAPSHOT_STAGING_TOOLS)
    validate_tool_metadata(SNAPSHOT_STAGING_TOOLS)


def test_no_staging_tool_reaches_the_graph_or_extracts(staging_on: None) -> None:
    """P2A's hard boundary, asserted from the module's imports and calls.

    Scanning the source text for words was the first attempt and it was the wrong instrument:
    "extracted" in a docstring matched "extract". What matters is what the module can *reach*, so
    this reads the AST -- imports and called names -- and ignores prose entirely.
    """
    import ast

    import menhir.mcp.tools.ingest.snapshot_staging as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                called.add(target.id)
            elif isinstance(target, ast.Attribute):
                called.add(target.attr)

    forbidden_modules = {"zipfile", "tarfile", "neo4j", "graphiti_core"}
    assert not (imported & forbidden_modules), f"P2A must not import {imported & forbidden_modules}"
    assert not any("graph" in name or "neo4j" in name for name in imported), imported
    forbidden_calls = {"get_backend", "scan_and_write_project", "write_project", "extractall"}
    assert not (called & forbidden_calls), f"P2A must not call {called & forbidden_calls}"


# --- telemetry redaction ------------------------------------------------------------------------


def test_chunk_telemetry_carries_no_content() -> None:
    """Invariant 5. The default payload records the arguments, and one of them is the base64 of
    somebody's source file -- silently, at volume, into a store nobody reads until they need it."""
    secret = b"SUPER-SECRET-SOURCE"
    encoded = base64.b64encode(secret).decode()

    payload = PutProjectSnapshotChunkTool().call_payload(
        upload_id="abc123", index=2, data_b64=encoded,
        declared_len=len(secret), digest=sha256_hex(secret),
    )

    serialized = json.dumps(payload)
    assert encoded not in serialized
    assert "SUPER" not in serialized
    assert set(payload) == {"upload_id", "index", "declared_len", "digest"}
    assert payload["index"] == 2


# --- a real round trip through the endpoints ----------------------------------------------------


async def test_begin_chunk_status_abort_round_trip(staging_on: None, session) -> None:
    body = b"0123456789abcdef"

    begun = json.loads(
        await BeginProjectSnapshotTool().endpoint(
            project_key="proj", declared_bytes=len(body), chunk_bytes=8
        )
    )
    assert begun["ok"] is True
    assert begun["total_chunks"] == 2
    upload_id = begun["upload_id"]

    first = json.loads(
        await PutProjectSnapshotChunkTool().endpoint(
            upload_id=upload_id, index=0,
            data_b64=base64.b64encode(body[:8]).decode(),
            declared_len=8, digest=sha256_hex(body[:8]),
        )
    )
    assert first["state"] == "RECEIVING"
    assert first["missing_chunks"] == [1]

    second = json.loads(
        await PutProjectSnapshotChunkTool().endpoint(
            upload_id=upload_id, index=1,
            data_b64=base64.b64encode(body[8:]).decode(),
            declared_len=8, digest=sha256_hex(body[8:]),
        )
    )
    assert second["state"] == "SEALED"

    status = json.loads(await GetProjectSnapshotStatusTool().endpoint(upload_id=upload_id))
    assert status["state"] == "SEALED"
    assert status["received_bytes"] == 16


async def test_a_refusal_comes_back_as_a_stable_code_not_an_exception(
    staging_on: None, session
) -> None:
    result = json.loads(await GetProjectSnapshotStatusTool().endpoint(upload_id="deadbeef" * 4))

    assert result["ok"] is False
    assert result["error"]["code"] == "snapshot.upload.not_found"


async def test_one_principal_cannot_read_anothers_upload(
    staging_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ownership comes from the session, never from an argument (invariant 2)."""
    class _Alice:
        user_id = "alice"
        session_id = "s1"

    class _Mallory:
        user_id = "mallory"
        session_id = "s2"

    monkeypatch.setattr(
        "menhir.mcp.tools.ingest.snapshot_staging.get_mcp_session", lambda: _Alice()
    )
    begun = json.loads(
        await BeginProjectSnapshotTool().endpoint(project_key="proj", declared_bytes=16)
    )

    monkeypatch.setattr(
        "menhir.mcp.tools.ingest.snapshot_staging.get_mcp_session", lambda: _Mallory()
    )
    result = json.loads(
        await GetProjectSnapshotStatusTool().endpoint(upload_id=begun["upload_id"])
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "snapshot.upload.not_found"


def test_telemetry_keeps_the_upload_id_and_still_masks_the_content() -> None:
    """Owner decision (2026-09-16): `upload_id` is allowlisted, its siblings are not.

    Without it, a failed upload writes one telemetry row per chunk with nothing to group them by,
    which is the single question an upload incident asks. The id is server-minted hex the receiver
    refuses unless it is hex, so it cannot carry caller prose.

    `digest` is deliberately still redacted even though it is equally identifier-shaped: it is
    derived from user bytes, so retaining it would let telemetry confirm whether a particular file
    had been uploaded. That distinction is the whole point of the decision and is asserted here
    rather than left to the allowlist's ordering.
    """
    from menhir.infrastructure.telemetry.helpers import _preview_of

    payload = PutProjectSnapshotChunkTool().call_payload(
        upload_id="a3f19c0b4d5e6f708192a3b4c5d6e7f8",
        index=7,
        declared_len=1048576,
        digest="9" * 64,
    )
    rendered = _preview_of(payload)

    assert "a3f19c0b4d5e6f708192a3b4c5d6e7f8" in rendered, "an upload's rows cannot be joined"
    assert "1048576" in rendered and '"index": 7' in rendered
    assert "9" * 64 not in rendered, "a content-derived digest must stay redacted"


def test_a_chunk_of_real_content_never_reaches_telemetry() -> None:
    """The invariant the allowlist must not have widened.

    `call_payload` drops `data_b64` by construction, so this asserts the belt as well as the
    braces: even handed the argument dict a caller actually sent, the preview carries no payload.
    """
    import base64

    from menhir.infrastructure.telemetry.helpers import _preview_of

    secret = base64.b64encode(b"SECRET-SOURCE-CODE" * 8).decode()
    rendered = _preview_of(
        {"upload_id": "b" * 32, "index": 0, "declared_len": 144, "data_b64": secret}
    )

    assert secret not in rendered
    assert "SECRET" not in rendered
    assert "b" * 32 in rendered
