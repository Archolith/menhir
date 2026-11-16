"""#669: every failure path in src/menhir/mcp/tools/ops raises McpToolRefusal with a code."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.mcp.tools.ops.acknowledge_warning import AcknowledgeWarningTool
from menhir.mcp.tools.ops.close_todo import CloseTodoTool
from menhir.mcp.tools.ops.delete_namespace import DeleteNamespaceTool
from menhir.mcp.tools.ops.force_reenrich import ForceReenrichTool
from menhir.mcp.tools.ops.force_release_lease import ForceReleaseEnrichmentLeaseTool
from menhir.mcp.tools.ops.get_artifact import GetArtifactTool
from menhir.mcp.tools.ops.get_artifact_relationships import ArtifactRelationshipsTool
from menhir.mcp.tools.ops.get_beacon_evidence import GetBeaconEvidenceTool
from menhir.mcp.tools.ops.get_enrichment_status import GetEnrichmentStatusTool
from menhir.mcp.tools.ops.get_episode_trace import GetEpisodeTraceTool
from menhir.mcp.tools.ops.get_provenance import GetProvenanceTool
from menhir.mcp.tools.ops.get_todo import GetTodoTool
from menhir.mcp.tools.ops.link_artifacts import LinkArtifactsTool
from menhir.mcp.tools.ops.link_memory_to_todo import LinkMemoryToTodoTool
from menhir.mcp.tools.ops.list_clients import ListClientsTool
from menhir.mcp.tools.ops.list_enrichment_queue import ListEnrichmentQueueTool
from menhir.mcp.tools.ops.list_namespace_memories import ListNamespaceMemoriesTool
from menhir.mcp.tools.ops.mint_client import MintClientTool
from menhir.mcp.tools.ops.rate_recall import RateRecallTool
from menhir.mcp.tools.ops.relocate_artifact_source import RelocateArtifactSourceTool
from menhir.mcp.tools.ops.reopen_todo import ReopenTodoTool
from menhir.mcp.tools.ops.resolve_todo import ResolveTodoTool
from menhir.mcp.tools.ops.revoke_client import RevokeClientTool
from menhir.mcp.tools.ops.supersede_artifact import SupersedeArtifactTool
from menhir.mcp.tools.ops.supersede_todo import SupersedeTodoTool
from menhir.mcp.tools.ops.transition_artifact import TransitionArtifactTool
from menhir.mcp.tools.ops.watch_enrichment import WatchEnrichmentTool
from tests.mcp.error_contract import error_of, text_of

EPISODE = "11111111-2222-3333-4444-555555555555"
NS = "tenant-a"


async def _scoped_only_unscoped_hits(uuid, namespace=None, **_kw):
    """Lookup that finds the object only without a namespace: it lives in another silo."""
    return None if namespace else {"uuid": uuid}


def _backend(**attrs):
    backend = MagicMock()
    for name, value in attrs.items():
        setattr(backend, name, value)
    return backend


async def _run(tool, backend, **kwargs):
    tool.get_backend = lambda: backend  # type: ignore[method-assign]
    return await tool.execute(**kwargs)


def _foreign_backend():
    return _backend(
        get_todo=AsyncMock(side_effect=_scoped_only_unscoped_hits),
        get_artifact=AsyncMock(side_effect=_scoped_only_unscoped_hits),
        fetch_memory_by_uuid=AsyncMock(side_effect=_scoped_only_unscoped_hits),
    )


# --- acknowledge_warning -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acknowledge_warning_unknown_kind() -> None:
    backend = _backend(acknowledge_operator_warning=AsyncMock())
    out = await _run(AcknowledgeWarningTool(), backend, kind="nope")
    error_of(out, code="invalid_argument")
    assert text_of(out).startswith("unknown warning kind 'nope'")
    backend.acknowledge_operator_warning.assert_not_awaited()


@pytest.mark.asyncio
async def test_acknowledge_warning_changed_while_acknowledging() -> None:
    backend = _backend(
        acknowledge_operator_warning=AsyncMock(
            return_value={"active": True, "acknowledged": [], "already_acknowledged": []}
        )
    )
    out = await _run(AcknowledgeWarningTool(), backend, kind="provider_auth")
    error_of(out, code="conflict")
    assert "changed while acknowledging; retry" in text_of(out)


# --- todos ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_todo_outside_namespace() -> None:
    out = await _run(CloseTodoTool(), _foreign_backend(), uuid="t1", namespace=NS)
    error_of(out, code="outside_namespace")
    assert "outside namespace tenant-a" in text_of(out)


@pytest.mark.asyncio
async def test_close_todo_not_found() -> None:
    backend = _backend(close_todo=AsyncMock(return_value=False))
    out = await _run(CloseTodoTool(), backend, uuid="t1")
    error_of(out, code="not_found")
    assert text_of(out) == "TODO t1 not found or already closed"


@pytest.mark.asyncio
async def test_get_todo_not_found() -> None:
    backend = _backend(get_todo=AsyncMock(return_value=None))
    out = await _run(GetTodoTool(), backend, uuid="t1", namespace=NS)
    error_of(out, code="not_found")
    assert text_of(out) == "TODO t1 not found in namespace tenant-a"


@pytest.mark.asyncio
async def test_link_memory_to_todo_unsupported_relation() -> None:
    out = await _run(
        LinkMemoryToTodoTool(), _backend(), memory_uuid="m", todo_uuid="t", relation="bogus"
    )
    error_of(out, code="invalid_argument")
    assert "unsupported relation 'bogus'" in text_of(out)


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["todo", "memory"])
async def test_link_memory_to_todo_outside_namespace(which: str) -> None:
    backend = _foreign_backend()
    if which == "memory":
        backend.get_todo = AsyncMock(return_value={"uuid": "t"})
    out = await _run(
        LinkMemoryToTodoTool(), backend,
        memory_uuid="m", todo_uuid="t", relation="mentions", namespace=NS,
    )
    error_of(out, code="outside_namespace")
    assert f"Refused: {which}" in text_of(out)


@pytest.mark.asyncio
async def test_link_memory_to_todo_not_written() -> None:
    backend = _backend(link_memory_to_todo=AsyncMock(return_value={"linked": False, "reason": "x"}))
    out = await _run(
        LinkMemoryToTodoTool(), backend, memory_uuid="m", todo_uuid="t", relation="mentions"
    )
    error_of(out, code="refused")
    assert "no link written (x)" in text_of(out)


@pytest.mark.asyncio
async def test_reopen_todo_outside_namespace_and_not_applied() -> None:
    out = await _run(
        ReopenTodoTool(), _foreign_backend(), todo_uuid="t", memory_uuid="m", namespace=NS
    )
    error_of(out, code="outside_namespace")
    backend = _backend(reopen_todo=AsyncMock(return_value={"applied": False}))
    out = await _run(ReopenTodoTool(), backend, todo_uuid="t", memory_uuid="m")
    error_of(out, code="refused")
    assert "t was not reopened" in text_of(out)


@pytest.mark.asyncio
async def test_resolve_todo_outside_namespace_and_not_applied() -> None:
    out = await _run(
        ResolveTodoTool(), _foreign_backend(), todo_uuid="t", memory_uuid="m", namespace=NS
    )
    error_of(out, code="outside_namespace")
    backend = _backend(resolve_todo=AsyncMock(return_value={"applied": False}))
    out = await _run(ResolveTodoTool(), backend, todo_uuid="t", memory_uuid="m")
    error_of(out, code="refused")
    assert "t was not resolved" in text_of(out)


@pytest.mark.asyncio
async def test_supersede_todo_outside_namespace_and_not_applied() -> None:
    out = await _run(
        SupersedeTodoTool(), _foreign_backend(), old_uuid="o", new_uuid="n", namespace=NS
    )
    error_of(out, code="outside_namespace")
    backend = _backend(supersede_todo=AsyncMock(return_value={"applied": False, "reason": "r"}))
    out = await _run(SupersedeTodoTool(), backend, old_uuid="o", new_uuid="n")
    error_of(out, code="refused")
    assert "o was not superseded (r)" in text_of(out)


# --- artifacts -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_artifact_not_found() -> None:
    backend = _backend(get_artifact=AsyncMock(return_value=None))
    out = await _run(GetArtifactTool(), backend, artifact_uuid="a1", namespace=NS)
    error_of(out, code="not_found")
    assert text_of(out) == "Artifact a1 not found in namespace tenant-a"


@pytest.mark.asyncio
async def test_get_artifact_relationships_outside_namespace_and_not_found() -> None:
    out = await _run(
        ArtifactRelationshipsTool(), _foreign_backend(), artifact_uuid="a1", namespace=NS
    )
    error_of(out, code="outside_namespace")
    backend = _backend(get_artifact_relationships=AsyncMock(return_value=None))
    out = await _run(ArtifactRelationshipsTool(), backend, artifact_uuid="a1")
    error_of(out, code="not_found")
    assert text_of(out) == "Artifact a1 not found"


@pytest.mark.asyncio
async def test_link_artifacts_failures() -> None:
    out = await _run(
        LinkArtifactsTool(), _foreign_backend(),
        source_uuid="s", target_uuid="t", relation="reviews", namespace=NS,
    )
    error_of(out, code="outside_namespace")

    # Source is the caller's own, target is foreign: the second guard must also raise.
    backend = _backend(
        get_artifact=AsyncMock(
            side_effect=lambda uuid, namespace=None: (
                {"uuid": uuid} if (uuid == "s" or not namespace) else None
            )
        )
    )
    out = await _run(
        LinkArtifactsTool(), backend,
        source_uuid="s", target_uuid="t", relation="reviews", namespace=NS,
    )
    error_of(out, code="outside_namespace")
    assert "artifact t" in text_of(out)

    backend = _backend(link_artifacts=AsyncMock(return_value={"linked": False, "reason": "artifact_not_found"}))
    out = await _run(
        LinkArtifactsTool(), backend, source_uuid="s", target_uuid="t", relation="reviews"
    )
    error_of(out, code="refused")
    assert text_of(out) == "Refused: one or both artifacts do not exist"

    out = await _run(
        LinkArtifactsTool(), backend, source_uuid="s", target_uuid="t", relation="supersedes"
    )
    error_of(out, code="refused")
    assert "supersedes is not a declarable relation" in text_of(out)


@pytest.mark.asyncio
async def test_supersede_artifact_failures() -> None:
    out = await _run(
        SupersedeArtifactTool(), _foreign_backend(), new_uuid="n", old_uuid="o", namespace=NS
    )
    error_of(out, code="outside_namespace")
    backend = _backend(
        get_artifact=AsyncMock(
            side_effect=lambda uuid, namespace=None: (
                {"uuid": uuid} if (uuid == "n" or not namespace) else None
            )
        )
    )
    out = await _run(
        SupersedeArtifactTool(), backend, new_uuid="n", old_uuid="o", namespace=NS
    )
    error_of(out, code="outside_namespace")
    backend = _backend(supersede_artifact=AsyncMock(return_value={"applied": False}))
    out = await _run(SupersedeArtifactTool(), backend, new_uuid="n", old_uuid="o")
    error_of(out, code="refused")
    assert "o was not superseded" in text_of(out)


@pytest.mark.asyncio
async def test_relocate_artifact_source_failures() -> None:
    out = await _run(
        RelocateArtifactSourceTool(), _foreign_backend(),
        artifact_uuid="a", old_path="x", new_path="y", namespace=NS,
    )
    error_of(out, code="outside_namespace")
    backend = _backend(
        relocate_artifact_source=AsyncMock(
            return_value={"applied": False, "reason": "locator_not_found"}
        )
    )
    out = await _run(
        RelocateArtifactSourceTool(), backend, artifact_uuid="a", old_path="x", new_path="y"
    )
    error_of(out, code="refused")
    assert text_of(out) == "Refused (locator_not_found): No source is recorded at x."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "code", "fragment"),
    [
        ("artifact_not_found", "not_found", "Artifact a not found"),
        ("supersession_requires_replacement", "refused", "SUPERSEDED requires a replacement"),
        ("stale_transition", "conflict", "changed during this transition"),
        ("illegal", "refused", "is not legal for a plan"),
    ],
)
async def test_transition_artifact_failures(reason: str, code: str, fragment: str) -> None:
    backend = _backend(
        transition_artifact_status=AsyncMock(
            return_value={
                "applied": False,
                "reason": reason,
                "from_status": "A",
                "artifact_type": "plan",
                "valid_transitions": ["B"],
            }
        )
    )
    out = await _run(TransitionArtifactTool(), backend, artifact_uuid="a", to_status="Z")
    error_of(out, code=code)
    assert fragment in text_of(out)


# --- episodes / enrichment -----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_cls", "kwargs"),
    [
        (ForceReenrichTool, {}),
        (ForceReleaseEnrichmentLeaseTool, {}),
        (GetEnrichmentStatusTool, {}),
        (GetEpisodeTraceTool, {}),
        (WatchEnrichmentTool, {}),
    ],
)
async def test_bad_episode_uuid_is_invalid_argument(tool_cls, kwargs) -> None:
    out = await _run(tool_cls(), _backend(), episode_uuid="not-a-uuid", **kwargs)
    error_of(out, code="invalid_argument")
    assert "Invalid episode UUID" in text_of(out)
    assert "ValueError" not in text_of(out)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_cls",
    [
        ForceReenrichTool,
        ForceReleaseEnrichmentLeaseTool,
        GetEnrichmentStatusTool,
        GetEpisodeTraceTool,
        WatchEnrichmentTool,
    ],
)
async def test_episode_outside_namespace(tool_cls) -> None:
    out = await _run(tool_cls(), _foreign_backend(), episode_uuid=EPISODE, namespace=NS)
    error_of(out, code="outside_namespace")
    assert f"Refused: episode {EPISODE} exists but is outside namespace tenant-a." in text_of(out)


@pytest.mark.asyncio
async def test_force_reenrich_not_found() -> None:
    backend = _backend(force_reset_failed_episode=AsyncMock(return_value=False))
    out = await _run(ForceReenrichTool(), backend, episode_uuid=EPISODE)
    error_of(out, code="not_found")
    assert "NOT_FOUND" in text_of(out)


@pytest.mark.asyncio
async def test_force_release_lease_not_found() -> None:
    backend = _backend(
        force_release_episode_lease=AsyncMock(return_value=False),
        get_max_enrichment_attempts=AsyncMock(return_value=3),
    )
    out = await _run(ForceReleaseEnrichmentLeaseTool(), backend, episode_uuid=EPISODE)
    error_of(out, code="not_found")
    assert "no ENRICHING episode with that UUID" in text_of(out)


@pytest.mark.asyncio
async def test_list_enrichment_queue_invalid_state() -> None:
    backend = _backend(list_episode_processing=AsyncMock())
    out = await _run(ListEnrichmentQueueTool(), backend, state="bogus")
    error_of(out, code="invalid_argument")
    assert text_of(out).startswith("Invalid state.")
    backend.list_episode_processing.assert_not_awaited()


# --- provenance / rate_recall --------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_provenance_outside_namespace_and_not_found() -> None:
    out = await _run(GetProvenanceTool(), _foreign_backend(), node_uuid="n1", namespace=NS)
    error_of(out, code="outside_namespace", tool="get_provenance")
    assert "Refused: node n1 exists but is outside namespace tenant-a." in text_of(out)
    backend = _backend(fetch_node_receipts=AsyncMock(return_value=None))
    out = await _run(GetProvenanceTool(), backend, node_uuid="n1")
    error_of(out, code="not_found", tool="get_provenance")
    assert "No node found with uuid=n1." in text_of(out)


@pytest.mark.asyncio
async def test_rate_recall_invalid_score() -> None:
    out = await _run(RateRecallTool(), _backend(), score="great")
    error_of(out, code="invalid_argument", tool="rate_recall")
    assert "Invalid score 'great'" in text_of(out)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recall_id", "fragment"),
    [
        ("r-1", "no recall found for recall_id 'r-1'"),
        (None, "no unrated recall found for this session"),
    ],
)
async def test_rate_recall_no_recall_found(monkeypatch, recall_id, fragment) -> None:
    from menhir.mcp.telemetry import telemetry_store

    monkeypatch.setattr(telemetry_store, "record_recall_feedback", lambda **_kw: None)
    out = await _run(RateRecallTool(), _backend(), score="useful", recall_id=recall_id)
    error_of(out, code="not_found", tool="rate_recall")
    assert fragment in text_of(out)


# --- namespace operations ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_namespace_refused() -> None:
    backend = _backend(
        delete_namespace=AsyncMock(side_effect=ValueError("namespace has 500 nodes (max 200)"))
    )
    out = await _run(DeleteNamespaceTool(), backend, namespace="eval-x")
    error_of(out, code="refused", tool="delete_namespace")
    assert "namespace has 500 nodes (max 200)" in text_of(out)
    assert "eval-x" in text_of(out)


@pytest.mark.asyncio
async def test_list_namespace_memories_unknown_kind() -> None:
    backend = _backend(list_namespace_records=AsyncMock())
    out = await _run(ListNamespaceMemoriesTool(), backend, kind="bogus")
    error_of(out, code="invalid_argument", tool="list_namespace_memories")
    assert "unknown kind 'bogus'" in text_of(out)
    backend.list_namespace_records.assert_not_awaited()


# --- client tokens -------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_cls", "kwargs"),
    [
        (ListClientsTool, {}),
        (MintClientTool, {"client_name": "c"}),
        (RevokeClientTool, {"client_id": "id"}),
    ],
)
async def test_client_token_tools_disabled(monkeypatch, tool_cls, kwargs) -> None:
    monkeypatch.setattr("menhir.api.client_token_store.get_client_token_store", lambda: None)
    out = await _run(tool_cls(), _backend(), **kwargs)
    error_of(out, code="disabled")
    assert text_of(out) == (
        "per-client token tier is not enabled (set MENHIR_CLIENT_TOKENS_ENABLED=1)"
    )


@pytest.mark.asyncio
async def test_mint_client_bad_tier_and_blank_name(monkeypatch) -> None:
    store = MagicMock()
    monkeypatch.setattr("menhir.api.client_token_store.get_client_token_store", lambda: store)
    out = await _run(MintClientTool(), _backend(), client_name="c", tier="root")
    error_of(out, code="invalid_argument")
    assert text_of(out) == "tier must be operator, agent, or readonly"
    out = await _run(MintClientTool(), _backend(), client_name="  ", tier="agent")
    error_of(out, code="invalid_argument")
    assert text_of(out) == "client_name is required"
    store.mint.assert_not_called()


@pytest.mark.asyncio
async def test_mint_client_undeclared_client_refused(monkeypatch) -> None:
    store = MagicMock()
    monkeypatch.setattr("menhir.api.client_token_store.get_client_token_store", lambda: store)
    monkeypatch.setattr("menhir.mcp.service_access.client_restrictions_configured", lambda *a, **k: True)
    monkeypatch.setattr(
        "menhir.mcp.service_access.declared_client_names", lambda *a, **k: {"other"}
    )
    out = await _run(MintClientTool(), _backend(), client_name="Rogue", tier="agent")
    error_of(out, code="refused")
    assert "refusing to mint undeclared client 'Rogue'" in text_of(out)
    store.mint.assert_not_called()


# --- get_beacon_evidence -------------------------------------------------------------------


def _beacon_backend(*results):
    return _backend(query_structure=AsyncMock(side_effect=list(results)))


@pytest.mark.asyncio
async def test_beacon_evidence_needs_exactly_one_argument() -> None:
    out = await _run(GetBeaconEvidenceTool(), _backend())
    error_of(out, code="invalid_argument")
    assert text_of(out) == "name exactly one of project_id or repository"
    out = await _run(GetBeaconEvidenceTool(), _backend(), project_id="p", repository="r")
    error_of(out, code="invalid_argument")


@pytest.mark.asyncio
async def test_beacon_evidence_repository_lookup_failures() -> None:
    out = await _run(
        GetBeaconEvidenceTool(), _beacon_backend({"error": "boom"}), repository="git@x:y.git"
    )
    error_of(out, code="refused")
    assert text_of(out) == "boom"

    out = await _run(
        GetBeaconEvidenceTool(), _beacon_backend({"projects": []}), repository="git@x:y.git"
    )
    error_of(out, code="not_found")
    assert "no indexed project recorded repository git@x:y.git" in text_of(out)

    projects = [
        {"project_id": "a", "name": "A", "root_path": "/a"},
        {"project_id": "b", "name": "B", "root_path": "/b"},
    ]
    out = await _run(
        GetBeaconEvidenceTool(), _beacon_backend({"projects": projects}), repository="git@x:y.git"
    )
    error_of(out, code="invalid_argument")
    assert "2 indexed projects recorded repository" in text_of(out)
    assert "project_id=a name=A root=/a" in text_of(out)


@pytest.mark.asyncio
async def test_beacon_evidence_backend_refusal() -> None:
    out = await _run(
        GetBeaconEvidenceTool(), _beacon_backend({"error": "index is partial"}), project_id="p"
    )
    error_of(out, code="refused")
    assert text_of(out) == "index is partial"
