"""M7 Phase 1 — Replay harness self-tests.

Tests that the ReplayRunner infrastructure itself works correctly:
loading fixtures, validating structure, dispatching actions, and
evaluating expectations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from menhir.domain import IngestResult, IngestStatus, new_session
from menhir.domain.models import ProcessingState
from menhir.domain.recall import QueryPreset, RecallResult, ScoredMemory
from menhir.infrastructure.pending_actions import PendingActionStore
from menhir.services import IngestService, LifecycleService, RecallService, ScoringService
from menhir.services.lifecycle_service import ConsolidationResult, DecayResult

from tests.replay.runner import (
    FixtureError,
    ReplayResult,
    ReplayRunner,
    StepResult,
    load_fixture,
)

pytestmark = [pytest.mark.unit, pytest.mark.replay]

FIXTURE_DIR = Path(__file__).parent / "replay" / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeLLM:
    """Minimal LLM stub for replay tests."""

    compress_return: str | None = "compressed"
    merge_return: str | None = "merged"

    async def compress_content(self, content: str) -> str | None:
        return self.compress_return

    async def merge_content(self, existing: str, new: str) -> str | None:
        return self.merge_return

    async def confirm_contradiction(self, **kwargs) -> bool | None:
        return False


def _build_runner(
    stub_memory_graph_adapter,
    stub_graphiti_client,
) -> ReplayRunner:
    """Build a ReplayRunner wired to test stubs."""
    llm = _FakeLLM()
    lifecycle_svc = LifecycleService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
        llm=llm,
        pending_actions=PendingActionStore(),
    )
    ingest_svc = IngestService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        llm=llm,
        lifecycle_service=lifecycle_svc,
    )
    scoring_svc = ScoringService()
    recall_svc = RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=scoring_svc,
        lifecycle_service=lifecycle_svc,
    )
    return ReplayRunner(
        ingest_service=ingest_svc,
        lifecycle_service=lifecycle_svc,
        recall_service=recall_svc,
    )


# ===========================================================================
# Fixture loading and validation
# ===========================================================================


class TestFixtureLoading:
    """load_fixture accepts dicts and paths, and validates structure."""

    def test_load_from_dict(self):
        data = load_fixture({
            "scenario": "test",
            "steps": [{"action": "wait"}],
        })
        assert data["scenario"] == "test"
        assert len(data["steps"]) == 1

    def test_load_from_json_file(self):
        path = FIXTURE_DIR / "basic_ingest_recall.json"
        data = load_fixture(path)
        assert data["scenario"] == "basic_ingest_and_recall"
        assert len(data["steps"]) >= 2

    def test_load_from_string_path(self):
        path = str(FIXTURE_DIR / "basic_ingest_recall.json")
        data = load_fixture(path)
        assert data["scenario"] == "basic_ingest_and_recall"


class TestFixtureValidation:
    """load_fixture rejects malformed fixtures with clear errors."""

    def test_missing_scenario_key(self):
        with pytest.raises(FixtureError, match="Missing required.*scenario"):
            load_fixture({"steps": [{"action": "wait"}]})

    def test_missing_steps_key(self):
        with pytest.raises(FixtureError, match="Missing required.*steps"):
            load_fixture({"scenario": "test"})

    def test_empty_steps(self):
        with pytest.raises(FixtureError, match="non-empty list"):
            load_fixture({"scenario": "test", "steps": []})

    def test_invalid_action(self):
        with pytest.raises(FixtureError, match="invalid action"):
            load_fixture({
                "scenario": "test",
                "steps": [{"action": "teleport"}],
            })

    def test_ingest_requires_text(self):
        with pytest.raises(FixtureError, match="requires 'text'"):
            load_fixture({
                "scenario": "test",
                "steps": [{"action": "ingest"}],
            })

    def test_recall_requires_query(self):
        with pytest.raises(FixtureError, match="requires 'query'"):
            load_fixture({
                "scenario": "test",
                "steps": [{"action": "recall"}],
            })

    def test_nonexistent_file(self):
        with pytest.raises(FixtureError, match="not found"):
            load_fixture("/nonexistent/path.json")

    def test_invalid_source_type(self):
        with pytest.raises(FixtureError, match="Expected dict or path"):
            load_fixture(42)


# ===========================================================================
# Action dispatch
# ===========================================================================


class TestActionDispatch:
    """Each action type dispatches to the correct service method."""

    async def test_wait_action(self, stub_memory_graph_adapter, stub_graphiti_client):
        runner = _build_runner(stub_memory_graph_adapter, stub_graphiti_client)
        result = await runner.run({
            "scenario": "wait_only",
            "steps": [{"action": "wait"}],
        })
        assert result.passed
        assert result.steps[0].action == "wait"
        assert result.steps[0].detail.get("waited") is True

    async def test_ingest_action(self, stub_memory_graph_adapter, stub_graphiti_client):
        runner = _build_runner(stub_memory_graph_adapter, stub_graphiti_client)
        result = await runner.run({
            "scenario": "single_ingest",
            "steps": [
                {
                    "action": "ingest",
                    "text": "Test memory content",
                    "source": "test",
                    "expect": {"status": "INGESTED"},
                },
            ],
        })
        assert result.passed
        assert result.steps[0].detail["status"] == "INGESTED"
        assert result.steps[0].detail["nodes_touched"] >= 0

    async def test_consolidate_action(self, stub_memory_graph_adapter, stub_graphiti_client):
        runner = _build_runner(stub_memory_graph_adapter, stub_graphiti_client)
        result = await runner.run({
            "scenario": "consolidate",
            "steps": [{"action": "consolidate_session", "expect": {"promoted_min": 0}}],
        })
        assert result.passed
        detail = result.steps[0].detail
        assert "promoted" in detail
        assert "deleted" in detail

    async def test_decay_action(self, stub_memory_graph_adapter, stub_graphiti_client):
        runner = _build_runner(stub_memory_graph_adapter, stub_graphiti_client)
        result = await runner.run({
            "scenario": "decay",
            "steps": [{"action": "decay", "expect": {"compressed_min": 0}}],
        })
        assert result.passed
        detail = result.steps[0].detail
        assert "compressed" in detail
        assert "deleted" in detail

    async def test_recall_action(self, stub_memory_graph_adapter, stub_graphiti_client):
        runner = _build_runner(stub_memory_graph_adapter, stub_graphiti_client)
        result = await runner.run({
            "scenario": "recall",
            "steps": [
                {"action": "recall", "query": "test query", "expect": {"results_count_min": 0}},
            ],
        })
        assert result.passed
        assert "results_count" in result.steps[0].detail

    async def test_recover_orphans_action(self, stub_memory_graph_adapter, stub_graphiti_client):
        runner = _build_runner(stub_memory_graph_adapter, stub_graphiti_client)
        result = await runner.run({
            "scenario": "orphans",
            "steps": [
                {"action": "recover_orphans", "expect": {"promoted_min": 0}},
            ],
        })
        assert result.passed
        assert "promoted" in result.steps[0].detail


# ===========================================================================
# Expectation evaluation
# ===========================================================================


class TestExpectations:
    """Step assertions evaluate correctly for pass/fail."""

    async def test_min_expectation_passes(self, stub_memory_graph_adapter, stub_graphiti_client):
        runner = _build_runner(stub_memory_graph_adapter, stub_graphiti_client)
        result = await runner.run({
            "scenario": "min_pass",
            "steps": [
                {"action": "consolidate_session", "expect": {"promoted_min": 0}},
            ],
        })
        assert result.passed

    async def test_min_expectation_fails(self, stub_memory_graph_adapter, stub_graphiti_client):
        runner = _build_runner(stub_memory_graph_adapter, stub_graphiti_client)
        # No session entities → promoted=0, but we require min 5
        result = await runner.run({
            "scenario": "min_fail",
            "steps": [
                {"action": "consolidate_session", "expect": {"promoted_min": 5}},
            ],
        })
        assert not result.passed
        assert result.steps[0].status == "fail"
        assert "promoted" in result.steps[0].error

    async def test_max_expectation_passes(self, stub_memory_graph_adapter, stub_graphiti_client):
        runner = _build_runner(stub_memory_graph_adapter, stub_graphiti_client)
        result = await runner.run({
            "scenario": "max_pass",
            "steps": [
                {"action": "consolidate_session", "expect": {"deleted_max": 100}},
            ],
        })
        assert result.passed

    async def test_max_expectation_fails(self, stub_memory_graph_adapter, stub_graphiti_client):
        # Seed 3 low-value session entities → all get deleted
        stub_memory_graph_adapter.session_entities = [
            {"uuid": f"node-{i}", "name": f"n{i}", "content": f"c{i}", "user_flagged": False}
            for i in range(3)
        ]
        # search_scored returns nothing → sharpness=1.0/(1+0)=1.0 ≥ 0.5 → promoted not deleted
        # Actually with 0 similar, sharpness=1.0 which is >= SHARPNESS_PROMOTE_THRESHOLD
        # So they'll be promoted, not deleted. Let's set max promoted to 0 to force failure.
        runner = _build_runner(stub_memory_graph_adapter, stub_graphiti_client)
        result = await runner.run({
            "scenario": "max_fail",
            "steps": [
                {"action": "consolidate_session", "expect": {"promoted_max": 0}},
            ],
        })
        assert not result.passed
        assert result.steps[0].status == "fail"

    async def test_exact_match_expectation(self, stub_memory_graph_adapter, stub_graphiti_client):
        runner = _build_runner(stub_memory_graph_adapter, stub_graphiti_client)
        result = await runner.run({
            "scenario": "exact",
            "steps": [
                {"action": "wait", "expect": {"waited": True}},
            ],
        })
        assert result.passed

    async def test_exact_match_fails(self, stub_memory_graph_adapter, stub_graphiti_client):
        runner = _build_runner(stub_memory_graph_adapter, stub_graphiti_client)
        result = await runner.run({
            "scenario": "exact_fail",
            "steps": [
                {"action": "wait", "expect": {"waited": False}},
            ],
        })
        assert not result.passed
        assert result.steps[0].status == "fail"


# ===========================================================================
# Multi-step scenarios from fixtures
# ===========================================================================


class TestFixtureScenarios:
    """Run each authored fixture in stubbed mode to completion."""

    async def test_basic_ingest_recall(self, stub_memory_graph_adapter, stub_graphiti_client):
        runner = _build_runner(stub_memory_graph_adapter, stub_graphiti_client)
        result = await runner.run(FIXTURE_DIR / "basic_ingest_recall.json")
        assert result.scenario == "basic_ingest_and_recall"
        assert len(result.steps) == 4
        # All steps should complete (pass or at least not error)
        for step in result.steps:
            assert step.status in ("pass", "fail"), f"Step {step.index} {step.action}: {step.error}"

    async def test_session_consolidation(self, stub_memory_graph_adapter, stub_graphiti_client):
        runner = _build_runner(stub_memory_graph_adapter, stub_graphiti_client)
        result = await runner.run(FIXTURE_DIR / "session_consolidation.json")
        assert result.scenario == "session_consolidation"
        assert len(result.steps) == 4
        for step in result.steps:
            assert step.status in ("pass", "fail"), f"Step {step.index} {step.action}: {step.error}"

    async def test_decay_lifecycle(self, stub_memory_graph_adapter, stub_graphiti_client):
        runner = _build_runner(stub_memory_graph_adapter, stub_graphiti_client)
        result = await runner.run(FIXTURE_DIR / "decay_lifecycle.json")
        assert result.scenario == "decay_lifecycle"
        for step in result.steps:
            assert step.status in ("pass", "fail"), f"Step {step.index} {step.action}: {step.error}"

    async def test_budget_cap_requeue(self, stub_memory_graph_adapter, stub_graphiti_client):
        runner = _build_runner(stub_memory_graph_adapter, stub_graphiti_client)
        result = await runner.run(FIXTURE_DIR / "budget_cap_requeue.json")
        assert result.scenario == "budget_cap_requeue"
        for step in result.steps:
            assert step.status in ("pass", "fail"), f"Step {step.index} {step.action}: {step.error}"


# ===========================================================================
# Result structure
# ===========================================================================


class TestReplayResult:
    """ReplayResult provides correct summary and pass/fail status."""

    def test_all_pass(self):
        r = ReplayResult(
            scenario="test",
            steps=[
                StepResult(index=0, action="wait", status="pass"),
                StepResult(index=1, action="wait", status="pass"),
            ],
        )
        assert r.passed
        assert r.summary == {"pass": 2, "fail": 0, "skip": 0, "error": 0}

    def test_one_fail(self):
        r = ReplayResult(
            scenario="test",
            steps=[
                StepResult(index=0, action="wait", status="pass"),
                StepResult(index=1, action="wait", status="fail", error="bad"),
            ],
        )
        assert not r.passed
        assert r.summary["fail"] == 1

    def test_one_error(self):
        r = ReplayResult(
            scenario="test",
            steps=[
                StepResult(index=0, action="wait", status="error", error="boom"),
            ],
        )
        assert not r.passed
        assert r.summary["error"] == 1
