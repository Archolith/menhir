"""Namespace (silo) isolation tests for the memory namespace primitive.

Covers the contract helpers and the recall read path (graphiti group_ids filter +
menhir defense-in-depth candidate filter). Write-path stamping/group_id propagation is
exercised by the ingest pipeline tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from menhir.core.backend_impl import RuntimeProvider
from menhir.domain.namespace import (
    namespace_to_group_id,
    namespace_to_group_ids,
    stamped_namespace,
)
from menhir.services.lifecycle_service import LifecycleService
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService


@pytest.mark.unit
def test_namespace_to_group_id_write_mapping() -> None:
    # None and the reserved default both map to graphiti's default group ("").
    assert namespace_to_group_id(None) == ""
    assert namespace_to_group_id("default") == ""
    assert namespace_to_group_id("alpha") == "alpha"


@pytest.mark.unit
def test_namespace_to_group_ids_read_filter() -> None:
    # None preserves legacy global behavior (no filter); explicit values scope reads.
    assert namespace_to_group_ids(None) is None
    assert namespace_to_group_ids("default") == [""]
    assert namespace_to_group_ids("alpha") == ["alpha"]


@pytest.mark.unit
def test_stamped_namespace_normalizes_default() -> None:
    assert stamped_namespace(None) == "default"
    assert stamped_namespace("default") == "default"
    assert stamped_namespace("alpha") == "alpha"


def _meta(uuid: str, namespace: str | None) -> dict[str, object]:
    row: dict[str, object] = {
        "uuid": uuid,
        "name": uuid,
        "scope": "PERSISTENT",
        "type": "SEMANTIC",
        "content": f"content {uuid}",
        "summary": None,
        "last_accessed": datetime.now(timezone.utc) - timedelta(hours=1),
        "edge_count": 3,
        "sharpness": 0.5,
        "freshness": "ACTIVE",
        "user_flagged": False,
    }
    if namespace is not None:
        row["namespace"] = namespace
    return row


def _svc(stub_graphiti_client, stub_memory_graph_adapter) -> RecallService:
    return RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_passes_group_ids_for_explicit_namespace(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    stub_graphiti_client.search_scored_results = [("n-a", "n-a", 0.9)]
    stub_memory_graph_adapter.candidate_metadata = [_meta("n-a", "alpha")]

    await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall("q", namespace="alpha")

    assert stub_graphiti_client.search_scored_calls[0]["group_ids"] == ["alpha"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_without_namespace_does_not_filter_groups(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    stub_graphiti_client.search_scored_results = [("n-a", "n-a", 0.9)]
    stub_memory_graph_adapter.candidate_metadata = [_meta("n-a", "alpha")]

    await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall("q")

    assert stub_graphiti_client.search_scored_calls[0]["group_ids"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_defense_in_depth_drops_foreign_namespace(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    # Even if search surfaced a foreign-silo node, the menhir filter must drop it.
    stub_graphiti_client.search_scored_results = [("n-a", "n-a", 0.9), ("n-b", "n-b", 0.85)]
    stub_memory_graph_adapter.candidate_metadata = [_meta("n-a", "alpha"), _meta("n-b", "beta")]

    result = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall("q", namespace="alpha")

    assert {r.uuid for r in result.results} == {"n-a"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_scopes_adjacency_computation_to_namespace(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    # SSOT-04 regression: candidate fetch was already namespace-scoped, but the
    # adjacency-scoring pass that followed it dropped namespace entirely, so a
    # foreign-namespace node's edges could still influence ranking.
    stub_graphiti_client.search_scored_results = [("n-a", "n-a", 0.9)]
    stub_memory_graph_adapter.candidate_metadata = [_meta("n-a", "alpha")]

    await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall("q", namespace="alpha")

    assert stub_memory_graph_adapter.adjacency_calls[0]["namespace"] == "alpha"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_without_namespace_does_not_scope_adjacency(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    stub_graphiti_client.search_scored_results = [("n-a", "n-a", 0.9)]
    stub_memory_graph_adapter.candidate_metadata = [_meta("n-a", "alpha")]

    await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall("q")

    assert stub_memory_graph_adapter.adjacency_calls[0]["namespace"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_default_namespace_includes_legacy_unstamped_nodes(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    # Legacy nodes lack a namespace property; they belong to the default silo.
    stub_graphiti_client.search_scored_results = [("legacy", "legacy", 0.9)]
    stub_memory_graph_adapter.candidate_metadata = [_meta("legacy", None)]

    result = await _svc(stub_graphiti_client, stub_memory_graph_adapter).recall("q", namespace="default")

    assert {r.uuid for r in result.results} == {"legacy"}


# --- namespace deletion guard ---

def _backend_with_adapter(delete_count: int = 5, node_count: int | None = None):
    adapter = SimpleNamespace(
        delete_namespace=MagicMock(return_value=delete_count),
        count_namespace=MagicMock(return_value=node_count if node_count is not None else delete_count),
        # Required by the erasure saga, which captures membership before destroying the
        # partition. Omitting it used to be invisible: the coordinator swallowed the
        # AttributeError into an empty member set and erased anyway, so these tests asserted a
        # successful deletion over a subject set that was never actually collected (CF-165).
        capture_namespace_uuids=MagicMock(return_value=[]),
    )
    backend = RuntimeProvider(built=SimpleNamespace(graph_adapter=adapter), process_session=None)
    return backend, adapter


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["default", "", "   ", None])
async def test_delete_namespace_refuses_default_and_blank(blocked) -> None:
    backend, adapter = _backend_with_adapter()
    with pytest.raises(ValueError):
        await backend.delete_namespace(blocked)
    adapter.delete_namespace.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_namespace_deletes_explicit_silo() -> None:
    backend, adapter = _backend_with_adapter(delete_count=7, node_count=7)

    result = await backend.delete_namespace("alpha")

    adapter.delete_namespace.assert_called_once_with("alpha", namespace="alpha")
    adapter.count_namespace.assert_called_once_with("alpha", namespace="alpha")
    # Completeness is part of the answer now, not a log line: an operator must be able to see
    # that a content class was left behind without reading the server log (CF-165).
    assert result["namespace"] == "alpha"
    assert result["deleted"] == 7
    assert result["complete"] is True
    assert result["not_covered"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_namespace_refuses_over_max_nodes_without_force() -> None:
    backend, adapter = _backend_with_adapter(node_count=500)

    with pytest.raises(ValueError, match="500"):
        await backend.delete_namespace("alpha", max_nodes=200)

    adapter.delete_namespace.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_namespace_force_bypasses_max_nodes_gate() -> None:
    backend, adapter = _backend_with_adapter(delete_count=500, node_count=500)

    result = await backend.delete_namespace("alpha", max_nodes=200, force=True)

    adapter.delete_namespace.assert_called_once_with("alpha", namespace="alpha")
    assert result["namespace"] == "alpha"
    assert result["deleted"] == 500


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_namespace_dry_run_never_deletes() -> None:
    backend, adapter = _backend_with_adapter(node_count=500)

    result = await backend.delete_namespace("alpha", max_nodes=200, dry_run=True)

    adapter.delete_namespace.assert_not_called()
    assert result == {
        "namespace": "alpha",
        "node_count": 500,
        "max_nodes": 200,
        "would_delete": False,
        "dry_run": True,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_namespace_dry_run_under_limit_reports_would_delete() -> None:
    backend, adapter = _backend_with_adapter(node_count=5)

    result = await backend.delete_namespace("alpha", max_nodes=200, dry_run=True)

    adapter.delete_namespace.assert_not_called()
    assert result["would_delete"] is True


# --- conflict scan namespace scoping (Phase 4) ---

@pytest.mark.unit
@pytest.mark.asyncio
async def test_contradiction_search_is_namespace_scoped(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    # The per-node contradiction search must be scoped to that node's namespace so
    # conflicts/correlations/merges never cross silos.
    stub_graphiti_client.search_scored_results = []  # no neighbours -> no conflict writes
    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
    )

    await svc._check_contradictions_batch(
        [{"uuid": "n-1", "content": "Acme launched Zephyr", "namespace": "alpha"}]
    )

    assert stub_graphiti_client.search_scored_calls[0]["group_ids"] == ["alpha"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_contradiction_search_defaults_to_default_namespace(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    # A candidate without a namespace property is scoped to the default silo (group "").
    stub_graphiti_client.search_scored_results = []
    svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
    )

    await svc._check_contradictions_batch([{"uuid": "n-1", "content": "legacy node"}])

    assert stub_graphiti_client.search_scored_calls[0]["group_ids"] == [""]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_namespace_surfaces_an_incomplete_erasure():
    """The coordinator's "I could not cover this" must reach the caller, not just the log.

    Regression for the CF-165 closure residual: delete_namespace collapsed every outcome to
    {namespace, deleted}, so erased_incomplete, not_covered and the residual-content quarantine
    were all indistinguishable from a clean deletion at the API boundary.
    """
    from menhir.services.erasure_coordinator import ERASED_INCOMPLETE

    backend, adapter = _backend_with_adapter(delete_count=3, node_count=3)
    with patch.object(
        backend, "_erasure",
        return_value=SimpleNamespace(
            erase_namespace=MagicMock(
                return_value={
                    "reason": ERASED_INCOMPLETE,
                    "op_id": "op-1",
                    "graph_deleted": 3,
                    "purged": {},
                    "unaddressable": ["mcp_events.payload_preview"],
                    "not_covered": ["recall_receipts.reason"],
                }
            )
        ),
    ):
        result = await backend.delete_namespace("alpha")

    assert result["deleted"] == 3
    assert result["complete"] is False
    assert result["reason"] == ERASED_INCOMPLETE
    assert result["not_covered"] == ["recall_receipts.reason"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_namespace_refuses_to_report_a_quarantined_erasure():
    """residual_content_after_purge means the purge did not do its job and the saga is held
    for review. Reporting a row count for it would call a quarantined operation a success."""
    from menhir.services.erasure_coordinator import RESIDUAL_CONTENT

    backend, adapter = _backend_with_adapter(delete_count=3, node_count=3)
    with patch.object(
        backend, "_erasure",
        return_value=SimpleNamespace(
            erase_namespace=MagicMock(
                return_value={
                    "reason": RESIDUAL_CONTENT,
                    "op_id": "op-2",
                    "residual": {"memory_revisions.old_value": 4},
                }
            )
        ),
    ):
        with pytest.raises(RuntimeError, match="quarantined"):
            await backend.delete_namespace("alpha")
