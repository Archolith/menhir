"""Warden execution receipts distinguish configuration from actual bounded enforcement."""
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import json

import pytest

from menhir.config import MemorySettings
from menhir.core.backend_runtime import RuntimeProvider
from menhir.domain.recall import WardenExecutionStatus
from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.mcp.tools.recall.recall_memories import RecallMemoriesTool
from menhir.services.context_builder import ContextBuilderService
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService


@pytest.fixture
def service(stub_graphiti_client, stub_memory_graph_adapter):
    stub_graphiti_client.search_scored_results = [("good", "config", .85), ("wrong", "config", .8)]
    stub_memory_graph_adapter.candidate_metadata = [
        dict(uuid=uuid, name="config", content=f"Configuration fact {uuid}", namespace=ns,
             scope="PERSISTENT", type="SEMANTIC", freshness="ACTIVE")
        for uuid, ns in (("good", "tenant"), ("wrong", "tenant"))
    ]
    stub_memory_graph_adapter.candidate_provenance_rows = [
        dict(uuid=uuid, evidence_node_kinds=["git"] if uuid == "good" else [],
             anchor_projects=[], episode_sources=["claude-code"] if uuid == "wrong" else [])
        for uuid in ("good", "wrong")
    ]
    return RecallService(graphiti_client=stub_graphiti_client, graph_adapter=stub_memory_graph_adapter,
                         scoring_service=ScoringService(), read_only=True)


@pytest.mark.asyncio
async def test_details_are_opt_in_and_cannot_change_enforcement(service):
    tuning = RetrievalTuningConfig(enable_warden_gate=True)
    ordinary = await service.recall("config", namespace="tenant", tuning=tuning)
    detailed = await service.recall("config", namespace="tenant", tuning=tuning, include_warden_status=True)
    assert ordinary.results == detailed.results and ordinary.warden_status is None
    assert ordinary.warden_notice is None
    status = detailed.warden_status
    assert status.state == "applied" and status.master_enabled
    assert status.evaluated == 2 and status.refused == 1
    assert {m.uuid for m in detailed.results} == {"good"}
    assert status.applied_guards == ("scope", "evidence_anchor", "oracle_admission")
    assert not status.metadata_gaps
    assert "authority_layers" in status.excluded_result_types
    disabled = await service.recall("config", namespace="tenant", include_warden_status=True)
    assert disabled.warden_status.state == "disabled" and disabled.warden_notice is None


@pytest.mark.asyncio
@pytest.mark.parametrize("show", [False, True])
async def test_failure_notice_is_independent_of_diagnostic_visibility(service, monkeypatch, show):
    from menhir.services.assertion_pipeline import AssertionPipeline
    async def fail(*args, **kwargs):
        raise RuntimeError("private failure details")
    monkeypatch.setattr(AssertionPipeline, "run", fail)
    result = await service.recall("config", namespace="tenant",
                                  tuning=RetrievalTuningConfig(enable_warden_gate=True),
                                  include_warden_status=show)
    assert len(result.results) == 2  # Existing exception-to-baseline policy is unchanged.
    assert "failed" in result.warden_notice and "unchecked" in result.warden_notice
    assert "private" not in str(asdict(result))
    if show:
        assert result.warden_status.state == "failed" and result.warden_status.applied_guards == ()
    else:
        assert result.warden_status is None


@pytest.mark.asyncio
async def test_optional_guard_is_computed_but_not_applied_without_master(service):
    result = await service.recall("config", namespace="tenant",
        tuning=RetrievalTuningConfig(enable_belief_gate=True), include_warden_status=True)
    assert result.warden_status.state == "computed_not_applied"
    assert "currentness" in result.warden_status.configured_guards
    assert not result.warden_status.applied_guards
    assert "master gate is off" in result.warden_notice


@pytest.mark.asyncio
@pytest.mark.parametrize("gap", ["provenance_error", "provenance_missing", "provenance_malformed", "temporal_error", "staleness_error"])
async def test_metadata_degradation_never_claims_complete_coverage(service, monkeypatch, gap):
    def fail(*args, **kwargs):
        raise RuntimeError("private source details")
    adapter = service.graph_adapter
    if gap == "provenance_error":
        monkeypatch.setattr(adapter, "fetch_candidate_provenance", fail)
    elif gap == "provenance_missing":
        adapter.candidate_provenance_rows = []
    elif gap == "provenance_malformed":
        adapter.candidate_provenance_rows = [dict(uuid="good", evidence_node_kinds=1)]
    elif gap == "temporal_error":
        monkeypatch.setattr(adapter, "fetch_temporal_facts", fail)
    else:
        monkeypatch.setattr("menhir.services.recall_pipeline._staleness_evidence_for", fail)
    result = await service.recall("config", namespace="tenant",
        tuning=RetrievalTuningConfig(enable_warden_gate=True, enable_belief_gate=True,
                                    enable_evidence_anchor=False), include_warden_status=True)
    assert result.warden_status.state == "applied"
    assert result.warden_status.metadata_gaps
    assert "incomplete" in result.warden_notice
    assert "private" not in str(asdict(result))


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_pending_bypasses_are_reported_at_both_return_paths(service, monkeypatch, empty):
    if empty:
        service.graphiti_client.search_scored_results = []
    async def pending(*args, **kwargs):
        return [dict(uuid="pending", content="Pending fact", scope="PERSISTENT")], []
    monkeypatch.setattr(service, "_wait_for_pending_episodes", pending)
    result = await service.recall("config", namespace="tenant", wait_for_pending=True,
        tuning=RetrievalTuningConfig(enable_warden_gate=True), include_warden_status=True)
    assert result.warden_status.unchecked_pending == 1
    assert "bypass" in result.warden_notice
    assert any(m.memory_type == "EPISODIC_PENDING" for m in result.results)
    assert result.warden_status.state == ("not_run" if empty else "applied")


@pytest.mark.asyncio
async def test_empty_and_missing_metadata_have_honest_receipts(service):
    service.graph_adapter.candidate_metadata = []
    result = await service.recall("config", include_warden_status=True,
                                 tuning=RetrievalTuningConfig(enable_warden_gate=True))
    assert result.results == [] and result.warden_status.state == "not_run"
    assert result.warden_status.evaluated == 0 and result.warden_status.missing_candidate_metadata == 2


@pytest.mark.asyncio
async def test_context_details_are_opt_in_and_budgeted(service, monkeypatch):
    builder = ContextBuilderService(recall_service=service,
                                   retrieval_tuning=RetrievalTuningConfig(enable_warden_gate=True))
    ordinary = await builder.build_context("config", namespace="tenant", max_tokens=3000)
    detailed = await builder.build_context("config", namespace="tenant", max_tokens=3000,
                                           include_warden_status=True)
    assert "Warden status:" not in ordinary.context and "Warden status:" in detailed.context
    assert ordinary.memory_ids == detailed.memory_ids == ["good"]
    from menhir.services.assertion_pipeline import AssertionPipeline
    async def fail(*args, **kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(AssertionPipeline, "run", fail)
    warned = await builder.build_context("config", namespace="tenant", max_tokens=3000)
    assert "Warden checks failed" in warned.context and warned.memory_count == 2
    tiny = await builder.build_context("config", namespace="tenant", max_tokens=1)
    assert tiny.memory_count == 0 and "Configuration fact" not in tiny.context
    assert tiny.truncated


@pytest.mark.asyncio
@pytest.mark.parametrize("compact", [False, True])
async def test_runtime_and_mcp_omit_details_by_default(service, compact):
    runtime = RuntimeProvider(SimpleNamespace(recall_service=service,
        settings=MemorySettings(frontier_warden_gate=True, frontier_source_memories=False)), process_session=None)
    plain = await runtime.recall("config", namespace="tenant")
    assert "warden_status" not in plain and "warden_notice" not in plain
    tool = RecallMemoriesTool()
    tool.get_backend = MagicMock(return_value=runtime)
    ordinary = json.loads(await tool.endpoint("config", namespace="tenant", compact=compact))
    detailed = json.loads(await tool.endpoint("config", namespace="tenant", compact=compact,
                                             include_warden_status=True))
    assert "warden_status" not in ordinary
    assert detailed["warden_status"]["state"] == "applied"
    assert ordinary["items"] == detailed["items"]


@pytest.mark.asyncio
async def test_runtime_context_forwards_opt_in(service):
    builder = ContextBuilderService(recall_service=service)
    runtime = RuntimeProvider(SimpleNamespace(context_builder=builder), process_session=None)
    ordinary = await runtime.build_context("config")
    detailed = await runtime.build_context("config", include_warden_status=True)
    assert "Warden status:" not in ordinary["context"]
    assert "Warden status:" in detailed["context"]


def test_context_receipt_canonical_empty_defaults():
    status = WardenExecutionStatus()
    assert status.state == "disabled" and status.notice() is None


@pytest.mark.asyncio
async def test_concurrent_calls_do_not_share_execution_receipts(service):
    import asyncio
    applied, disabled = await asyncio.gather(
        service.recall("config", namespace="tenant", include_warden_status=True,
                       tuning=RetrievalTuningConfig(enable_warden_gate=True)),
        service.recall("config", namespace="tenant", include_warden_status=True),
    )
    assert applied.warden_status.state == "applied"
    assert disabled.warden_status.state == "disabled"
    assert applied.warden_status is not disabled.warden_status


@pytest.mark.asyncio
@pytest.mark.parametrize("show", [False, True])
async def test_mcp_context_option_and_notice_propagate(show):
    from menhir.mcp.tools.recall.build_context import BuildContextTool
    backend = SimpleNamespace(build_context=AsyncMock(return_value={"context": "Memory checks: Warden checks failed."}))
    tool = BuildContextTool()
    tool.get_backend = MagicMock(return_value=backend)
    result = await tool.endpoint("config", session_id="test", include_warden_status=show)
    assert "Warden checks failed" in result
    assert backend.build_context.call_args.kwargs.get("include_warden_status", False) is show


@pytest.mark.asyncio
@pytest.mark.parametrize("show", [False, True])
async def test_bootstrap_reports_only_ranked_relevant_warden_coverage(monkeypatch, show):
    from importlib import import_module
    module = import_module("menhir.mcp.tools.recall.recall_context_memories")
    monkeypatch.setattr(module, "_has_recent_flagged_bootstrap_read", lambda *a, **k: True)
    backend = SimpleNamespace(
        fetch_flagged_memory_bootstrap_version=AsyncMock(return_value="version"),
        recall=AsyncMock(return_value={"results": [], "warden_status": {"state": "failed"},
                                      "warden_notice": "Warden checks failed."}),
        fetch_recent_memories=AsyncMock(return_value=[]), list_todos=AsyncMock(return_value=[]),
    )
    tool = module.RecallContextMemoriesTool()
    tool.get_backend = MagicMock(return_value=backend)
    result = json.loads(await tool.endpoint(query="config", include_warden_status=show))
    assert ("relevant_warden_status" in result) is show
    assert result["relevant_warden_notice"] == "Warden checks failed."
    assert backend.recall.call_args.kwargs.get("include_warden_status", False) is show


@pytest.mark.asyncio
async def test_missing_verdicts_do_not_claim_full_enforcement(service, monkeypatch):
    from menhir.services.assertion_pipeline import AssertionOutcome, AssertionPipeline
    async def empty(*args, **kwargs):
        return AssertionOutcome()
    monkeypatch.setattr(AssertionPipeline, "run", empty)
    result = await service.recall("config", namespace="tenant", include_warden_status=True,
                                 tuning=RetrievalTuningConfig(enable_warden_gate=True))
    assert result.warden_status.state == "partial" and result.warden_status.evaluated == 0
    assert result.warden_status.unassessed == 2 and result.warden_status.applied_guards == ()
    assert "incomplete" in result.warden_notice
