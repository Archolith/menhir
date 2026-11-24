"""Regression tests for #579 / #566 live oracle inputs and #659 rerank-oracle integration."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from menhir.api.routes_support import RecallRequest
from menhir.domain.artifact_role import (
    ContentRole,
    derive_content_role,
    derive_work_artifact_projection,
)
from menhir.domain.oracles import (
    CandidateMemory,
    OraclePolarity,
    OracleResult,
    QueryContext,
)
from menhir.domain.recall_visibility import memory_lifecycle_note
from menhir.domain.retrieval_tuning import RANKER_ORACLE_COMBINED, RetrievalTuningConfig
from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.services.oracle_executor import OracleExecutor
from menhir.services.recall_service import RecallService
from menhir.services.retrieval_oracles import SemanticOracle, StructureOracle
from menhir.services.scoring_service import ScoringService


def _label(value: object) -> str | None:
    return None if value is None else str(getattr(value, "value", value))


def _row(uuid: str, name: str, content: str, **extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "uuid": uuid,
        "name": name,
        "scope": "PERSISTENT",
        "type": "SEMANTIC",
        "content": content,
        "summary": content,
        "last_accessed": datetime.now(timezone.utc),
        "created_at": datetime.now(timezone.utc),
        "edge_count": 1,
        "sharpness": 0.2,
        "freshness": "ACTIVE",
        "user_flagged": False,
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# 1. Pure WorkArtifact projection policy (#579)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_work_artifact_projection_marks_historical_only_when_all_superseded() -> None:
    proj = derive_work_artifact_projection(
        [{"status": "SUPERSEDED", "artifact_type": "plan", "superseded_by": "plan-v2"}]
    )
    assert proj == {
        "artifact_status": "historical",
        "superseded_by": "plan-v2",
        "artifact_type": "plan",
    }


@pytest.mark.unit
def test_work_artifact_projection_keeps_active_entity_current_when_mixed() -> None:
    proj = derive_work_artifact_projection(
        [
            {"status": "SUPERSEDED", "artifact_type": "plan", "superseded_by": "plan-v2"},
            {"status": "APPROVED", "artifact_type": "review", "superseded_by": None},
        ]
    )
    assert "artifact_status" not in proj
    assert "superseded_by" not in proj
    assert proj["artifact_type"] == "review"


@pytest.mark.unit
def test_work_artifact_projection_multi_type_flows_into_content_roles() -> None:
    proj = derive_work_artifact_projection(
        [
            {"status": "APPROVED", "artifact_type": "plan", "superseded_by": None},
            {"status": "APPROVED", "artifact_type": "review", "superseded_by": None},
        ]
    )
    assert "artifact_type" not in proj
    assert proj["artifact_types"] == ("plan", "review")
    roles = derive_content_role(proj)
    assert roles == {ContentRole.PLAN, ContentRole.DECISION}


# ---------------------------------------------------------------------------
# 2. fetch_candidate_provenance Cypher contract (#579)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fetch_candidate_provenance_projects_work_artifacts_dynamically() -> None:
    class _FakeNeo4j:
        def __init__(self) -> None:
            self.last_query: str = ""
            self.last_params: dict[str, object] = {}

        def execute(
            self, query: str, params: dict[str, object] | None = None
        ) -> list[dict[str, object]]:
            self.last_query = query
            self.last_params = params or {}
            return []

    neo4j = _FakeNeo4j()
    MemoryQueryRepository(neo4j).fetch_candidate_provenance(["u1"])
    assert "AS work_artifacts" in neo4j.last_query
    assert "[:ABOUT]" not in neo4j.last_query
    assert "[:SUPERSEDES]" not in neo4j.last_query
    assert "type(about_rel) = $about_type" in neo4j.last_query
    assert "type(sup_rel) = $supersedes_type" in neo4j.last_query
    assert neo4j.last_params["about_type"] == "ABOUT"
    assert neo4j.last_params["supersedes_type"] == "SUPERSEDES"


# ---------------------------------------------------------------------------
# 3. End-to-end recall wiring (#579 + #566 + #659)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_projects_superseded_work_artifact_onto_entity_and_lifecycle_note(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    stub_graphiti_client.search_scored_results = [("e-old", "Old OAuth Plan", 0.85)]
    stub_memory_graph_adapter.candidate_metadata = [
        _row("e-old", "Old OAuth Plan", "Use legacy static keys only.")
    ]
    stub_memory_graph_adapter.candidate_provenance_rows = [
        {
            "uuid": "e-old",
            "evidence_node_kinds": [],
            "anchor_projects": [],
            "anchor_paths": [],
            "episode_sources": ["agent"],
            "work_artifacts": [
                {
                    "status": "SUPERSEDED",
                    "artifact_type": "plan",
                    "superseded_by": "wa-plan-v2",
                }
            ],
        }
    ]
    svc = RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
    )
    result = await svc.recall(
        "how does oauth work",
        tuning=RetrievalTuningConfig(enable_warden_gate=True),
    )
    assert len(result.results) == 1
    item = result.results[0]
    assert item.artifact_status == "historical"
    assert item.superseded_by == "wa-plan-v2"
    assert _label(item.warden_label) == "historical"
    note = memory_lifecycle_note(
        status=item.status,
        artifact_status=item.artifact_status,
        superseded_by=item.superseded_by,
    )
    assert note is not None and "superseded" in note.lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_structure_oracle_and_anchor_roles_fire_from_file_context_and_anchor_paths(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    stub_graphiti_client.search_scored_results = [
        ("e-match", "Auth Plan Note", 0.80),
        ("e-other", "Unrelated Note", 0.80),
    ]
    stub_memory_graph_adapter.candidate_metadata = [
        _row("e-match", "Auth Plan Note", "OAuth plan notes"),
        _row("e-other", "Unrelated Note", "OAuth other notes"),
    ]
    stub_memory_graph_adapter.candidate_provenance_rows = [
        {
            "uuid": "e-match",
            "evidence_node_kinds": [],
            "anchor_projects": ["menhir"],
            "anchor_paths": [".agent/plans/oauth-plan.md", "src/menhir/api/auth.py"],
            "episode_sources": ["agent"],
            "work_artifacts": [],
        },
        {
            "uuid": "e-other",
            "evidence_node_kinds": [],
            "anchor_projects": ["menhir"],
            "anchor_paths": ["src/menhir/cli/up.py"],
            "episode_sources": ["agent"],
            "work_artifacts": [],
        },
    ]
    svc = RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
    )
    result = await svc.recall(
        "what is the plan for auth",
        file_context=r".\src\menhir\api\auth.py",
        file_context_project="menhir",
        trace=True,
        tuning=RetrievalTuningConfig(enable_oracle_ranking=True, enable_warden_gate=True),
    )
    assert result.trace is not None and result.trace.ranking
    by_id = {row.candidate_id: row for row in result.trace.ranking}
    # e-match gets StructureOracle overlap (+ relevant logit) AND /plans/ anchor role via IntentOracle.
    assert by_id["e-match"].role_logits["relevant"] > by_id["e-other"].role_logits["relevant"]
    assert result.results[0].uuid == "e-match"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_oracle_combined_demotes_superseded_work_artifact_below_current_under_rerank(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch
) -> None:
    from menhir.services import rerank_oracle as ro_mod

    async def _fake_rerank_scores(query, candidates, metadata_by_uuid, **kwargs):
        # Cross-encoder slightly prefers the superseded plan over the current one.
        return {"e-old": 2.0, "e-new": 1.8}, None

    monkeypatch.setattr(ro_mod, "rerank_scores", _fake_rerank_scores)

    stub_graphiti_client.search_scored_results = [
        ("e-old", "Old Deploy Plan", 0.85),
        ("e-new", "New Deploy Plan", 0.85),
    ]
    stub_memory_graph_adapter.candidate_metadata = [
        _row("e-old", "Old Deploy Plan", "Deploy on legacy port 8000."),
        _row("e-new", "New Deploy Plan", "Deploy on current port 8100."),
    ]
    stub_memory_graph_adapter.candidate_provenance_rows = [
        {
            "uuid": "e-old",
            "evidence_node_kinds": [],
            "anchor_projects": [],
            "anchor_paths": [],
            "episode_sources": ["agent"],
            "work_artifacts": [
                {"status": "SUPERSEDED", "artifact_type": "plan", "superseded_by": "wa-new"}
            ],
        },
        {
            "uuid": "e-new",
            "evidence_node_kinds": [],
            "anchor_projects": [],
            "anchor_paths": [],
            "episode_sources": ["agent"],
            "work_artifacts": [
                {"status": "APPROVED", "artifact_type": "plan", "superseded_by": None}
            ],
        },
    ]
    svc = RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
        rerank_model_dir="fake-model-dir",
    )
    res_combined = await svc.recall(
        "what is the deploy port",
        trace=True,
        tuning=RetrievalTuningConfig(
            enable_cross_encoder_rerank=True,
            enable_warden_gate=True,
            ranking_strategy=RANKER_ORACLE_COMBINED,
            oracle_weight_rerank=1.0,
            oracle_weight_current=1.0,
            oracle_weight_historical=1.0,
        ),
    )
    # Current plan wins over slightly higher rerank logit of superseded plan; old stays flagged.
    assert [r.uuid for r in res_combined.results] == ["e-new", "e-old"]
    assert res_combined.results[1].artifact_status == "historical"
    assert res_combined.results[1].superseded_by == "wa-new"
    assert _label(res_combined.results[1].warden_label) == "historical"


# ---------------------------------------------------------------------------
# 4. REST RecallRequest file_context + Batch oracle failure parity (#566, #571)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rest_recall_request_accepts_file_context_fields() -> None:
    req = RecallRequest(
        query="auth checks",
        file_context="src/menhir/api/auth.py",
        file_context_project="menhir",
    )
    assert req.file_context == "src/menhir/api/auth.py"
    assert req.file_context_project == "menhir"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_batch_oracle_failure_marks_failed_true_for_coverage_gap_reporting() -> None:
    class _BrokenBatch:
        name = "broken_batch"

        async def evaluate_batch(
            self, query: QueryContext, candidates: list[CandidateMemory]
        ) -> dict[str, OracleResult]:
            raise RuntimeError("boom")

    ex = OracleExecutor([SemanticOracle(), StructureOracle()], batch_oracles=[_BrokenBatch()])
    grouped = await ex.evaluate(
        QueryContext(text="q"), [CandidateMemory("c1", "content")]
    )
    batch_res = [r for r in grouped["c1"] if r.oracle == "broken_batch"]
    assert len(batch_res) == 1
    assert batch_res[0].failed is True
    assert batch_res[0].polarity is OraclePolarity.MISSING
