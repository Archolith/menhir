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
from menhir.domain.temporal import derive_edge_temporal_projection
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
    assert ":WorkArtifact" not in neo4j.last_query
    assert "[:ABOUT]" not in neo4j.last_query
    assert "[:SUPERSEDES]" not in neo4j.last_query
    assert "$wa_label IN labels(wa)" in neo4j.last_query
    assert "type(about_rel) = $about_type" in neo4j.last_query
    assert "type(sup_rel) = $supersedes_type" in neo4j.last_query
    assert neo4j.last_params["wa_label"] == "WorkArtifact"
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


# ---------------------------------------------------------------------------
# 5. Edge-level temporal projection & QueryContext.as_of_time (#579 Items 2-3)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_edge_temporal_projection_invariants_respect_350_safety() -> None:
    assert derive_edge_temporal_projection(None) == {}
    assert derive_edge_temporal_projection({"total": 0, "active": 0, "expired": 0}) == {}

    # All same-tenant fact edges belief-expired -> all_facts_expired (never raw expired_at).
    all_exp = derive_edge_temporal_projection(
        {"total": 2, "active": 0, "expired": 2, "valid_ats": [], "invalid_ats": []}
    )
    assert all_exp == {"all_facts_expired": True}

    # Mixed active + expired edges (#350 Cause 1) -> entity stays current; open-ended active
    # edge omits invalid_at; earliest valid_at wins using timezone-aware comparison.
    mixed = derive_edge_temporal_projection(
        {
            "total": 3,
            "active": 2,
            "expired": 1,
            "valid_ats": ["2026-01-05T02:00:00+04:00", "2026-01-05T00:00:00Z[UTC]"],
            "invalid_ats": ["2026-02-01T00:00:00Z"],  # only 1 of 2 active edges ended
        }
    )
    assert "all_facts_expired" not in mixed
    assert "invalid_at" not in mixed
    assert mixed["valid_at"] == "2026-01-05T02:00:00+04:00"  # 2026-01-04T22:00Z < 2026-01-05T00:00Z

    # All active edges bounded on both ends -> earliest valid_at and latest invalid_at.
    bounded = derive_edge_temporal_projection(
        {
            "total": 2,
            "active": 2,
            "expired": 0,
            "valid_ats": ["2026-01-10T00:00:00Z", "2026-01-01T00:00:00Z"],
            "invalid_ats": ["2026-02-01T00:00:00Z", "2026-03-15T00:00:00Z"],
        }
    )
    assert bounded == {
        "valid_at": "2026-01-01T00:00:00Z",
        "invalid_at": "2026-03-15T00:00:00Z",
    }

    # Malformed timestamp fails open (omits that bound).
    bad_ts = derive_edge_temporal_projection(
        {"total": 1, "active": 1, "expired": 0, "valid_ats": ["not-a-date"], "invalid_ats": []}
    )
    assert bad_ts == {}


@pytest.mark.unit
def test_fetch_candidate_provenance_projects_fact_temporal_dynamically() -> None:
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
    assert "AS fact_temporal" in neo4j.last_query
    assert "[:RELATES_TO]" not in neo4j.last_query
    assert "type(r) = $relates_to_type" in neo4j.last_query
    assert "r[$rederive_pending_key]" in neo4j.last_query
    assert "coalesce(r.namespace, r.group_id, '')" in neo4j.last_query
    assert neo4j.last_params["relates_to_type"] == "RELATES_TO"
    assert neo4j.last_params["expired_at_key"] == "expired_at"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_expired_edges_flag_demote_under_current_and_boost_under_historical(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch
) -> None:
    from menhir.services import rerank_oracle as ro_mod

    async def _fake_rerank_scores(query, candidates, metadata_by_uuid, **kwargs):
        # Cross-encoder slightly prefers e-exp on current query, and slightly prefers e-cur on historical.
        if "did i buy" in query.lower():
            return {"e-cur": 2.0, "e-exp": 1.8, "e-mix": 1.5}, None
        return {"e-exp": 2.0, "e-cur": 1.8, "e-mix": 1.5}, None

    monkeypatch.setattr(ro_mod, "rerank_scores", _fake_rerank_scores)

    stub_graphiti_client.search_scored_results = [
        ("e-exp", "Old Bike", 0.85),
        ("e-cur", "Current Bike", 0.85),
        ("e-mix", "Mixed Hub Bike", 0.85),
    ]
    stub_memory_graph_adapter.candidate_metadata = [
        _row("e-exp", "Old Bike", "Used to ride a Trek road bike."),
        _row("e-cur", "Current Bike", "Currently rides a Canyon gravel bike."),
        _row("e-mix", "Mixed Hub Bike", "Bike shop hub with old and current entries."),
    ]
    stub_memory_graph_adapter.candidate_provenance_rows = [
        {
            "uuid": "e-exp",
            "evidence_node_kinds": [],
            "anchor_projects": [],
            "anchor_paths": [],
            "episode_sources": ["agent"],
            "work_artifacts": [],
            "fact_temporal": {"total": 2, "active": 0, "expired": 2, "valid_ats": [], "invalid_ats": []},
        },
        {
            "uuid": "e-cur",
            "evidence_node_kinds": [],
            "anchor_projects": [],
            "anchor_paths": [],
            "episode_sources": ["agent"],
            "work_artifacts": [],
            "fact_temporal": {"total": 1, "active": 1, "expired": 0, "valid_ats": [], "invalid_ats": []},
        },
        {
            "uuid": "e-mix",
            "evidence_node_kinds": [],
            "anchor_projects": [],
            "anchor_paths": [],
            "episode_sources": ["agent"],
            "work_artifacts": [],
            "fact_temporal": {"total": 3, "active": 1, "expired": 2, "valid_ats": [], "invalid_ats": []},
        },
    ]
    svc = RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
        rerank_model_dir="fake-model-dir",
    )

    # Current intent: both active entities (e-cur=1.8+0.64, e-mix=1.5+0.64) beat all-expired
    # e-exp (2.0+0.0); e-exp is flagged historical, NOT dropped; e-mix stays current (#350 Cause 1).
    res_cur = await svc.recall(
        "what bike do i ride now",
        trace=True,
        tuning=RetrievalTuningConfig(
            enable_cross_encoder_rerank=True,
            enable_warden_gate=True,
            enable_intent_lens=True,
            ranking_strategy=RANKER_ORACLE_COMBINED,
        ),
    )
    assert [r.uuid for r in res_cur.results] == ["e-cur", "e-mix", "e-exp"]
    by_uuid = {r.uuid: r for r in res_cur.results}
    assert by_uuid["e-cur"].warden_label is None
    assert _label(by_uuid["e-exp"].warden_label) == "historical"
    assert by_uuid["e-mix"].warden_label is None

    # Historical intent ("which bike did i buy" -> RECALL_PERSONAL_HISTORY -> historical lens):
    # e-exp gets +0.90 historical boost and beats e-cur despite lower rerank logit!
    res_hist = await svc.recall(
        "which bike did i buy first",
        trace=True,
        tuning=RetrievalTuningConfig(
            enable_cross_encoder_rerank=True,
            enable_warden_gate=True,
            enable_intent_lens=True,
            ranking_strategy=RANKER_ORACLE_COMBINED,
        ),
    )
    assert [r.uuid for r in res_hist.results] == ["e-exp", "e-cur", "e-mix"]
    assert all(r.warden_label is None for r in res_hist.results)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_extracts_as_of_time_and_enforces_edge_world_validity(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    created = datetime(2025, 11, 1, tzinfo=timezone.utc)
    stub_graphiti_client.search_scored_results = [
        ("e-valid", "Valid At Pivot", 0.85),
        ("e-future", "Not Yet Valid At Pivot", 0.85),
        ("e-ended", "Already Ended Before Pivot", 0.85),
    ]
    stub_memory_graph_adapter.candidate_metadata = [
        _row("e-valid", "Valid At Pivot", "Port was 8080.", created_at=created),
        _row("e-future", "Not Yet Valid At Pivot", "Port became 9090 in March.", created_at=created),
        _row("e-ended", "Already Ended Before Pivot", "Port was 7070 in December.", created_at=created),
    ]
    stub_memory_graph_adapter.candidate_provenance_rows = [
        {
            "uuid": "e-valid",
            "evidence_node_kinds": [],
            "anchor_projects": [],
            "anchor_paths": [],
            "episode_sources": ["agent"],
            "work_artifacts": [],
            "fact_temporal": {
                "total": 1, "active": 1, "expired": 0,
                "valid_ats": ["2025-12-01T00:00:00Z"],
                "invalid_ats": ["2026-02-01T00:00:00Z"],
            },
        },
        {
            "uuid": "e-future",
            "evidence_node_kinds": [],
            "anchor_projects": [],
            "anchor_paths": [],
            "episode_sources": ["agent"],
            "work_artifacts": [],
            "fact_temporal": {
                "total": 1, "active": 1, "expired": 0,
                "valid_ats": ["2026-03-01T00:00:00Z"],
                "invalid_ats": [],
            },
        },
        {
            "uuid": "e-ended",
            "evidence_node_kinds": [],
            "anchor_projects": [],
            "anchor_paths": [],
            "episode_sources": ["agent"],
            "work_artifacts": [],
            "fact_temporal": {
                "total": 1, "active": 1, "expired": 0,
                "valid_ats": ["2025-12-01T00:00:00Z"],
                "invalid_ats": ["2026-01-10T00:00:00Z"],
            },
        },
    ]
    svc = RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
    )
    result = await svc.recall(
        "what was the deploy port as of 2026-01-15",
        tuning=RetrievalTuningConfig(enable_warden_gate=True),
    )
    assert [r.uuid for r in result.results] == ["e-valid"]
