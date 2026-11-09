"""Oracle wiring: P3 failed-oracle receipt (#571), P4 node-level supersession, P6 role metadata."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from menhir.domain.artifact_role import ContentRole, derive_content_role
from menhir.domain.oracles import (
    CandidateMemory,
    OraclePolarity,
    OracleResult,
    OracleTarget,
    QueryContext,
)
from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.infrastructure.cypher import ENTITY_METADATA_FIELDS
from menhir.services.oracle_executor import OracleExecutor
from menhir.services.recall_service import RecallService
from menhir.services.retrieval_oracles import IntentOracle, TemporalOracle
from menhir.services.scoring_service import ScoringService


def _label(value) -> str | None:
    return None if value is None else str(getattr(value, "value", value))


# --------------------------------------------------------------------------- P3 unit


class _Raises:
    name = "boom"

    def evaluate(self, query, candidate):
        raise ValueError("nope")


class _Sleeps:
    name = "slow"

    def evaluate(self, query, candidate):
        time.sleep(0.5)
        return OracleResult(oracle=self.name, probability=1.0)


class _Fine:
    name = "fine"

    def evaluate(self, query, candidate):
        return OracleResult(oracle=self.name, probability=0.7)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_executor_exception_yields_failed_neutral_result() -> None:
    out = await OracleExecutor([_Raises()]).evaluate(QueryContext(text="q"), [CandidateMemory("c", "x")])
    (res,) = out["c"]
    assert res.failed is True
    assert res.note == "error: ValueError"
    assert res.polarity is OraclePolarity.MISSING


@pytest.mark.unit
@pytest.mark.asyncio
async def test_executor_timeout_yields_failed_neutral_result() -> None:
    ex = OracleExecutor([_Sleeps()], per_oracle_timeout_s=0.05)
    out = await ex.evaluate(QueryContext(text="q"), [CandidateMemory("c", "x")])
    (res,) = out["c"]
    assert res.failed is True
    assert res.note == "timeout"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_executor_normal_oracle_is_not_failed() -> None:
    out = await OracleExecutor([_Fine()]).evaluate(QueryContext(text="q"), [CandidateMemory("c", "x")])
    (res,) = out["c"]
    assert res.failed is False
    assert res.probability == 0.7


# --------------------------------------------------------------------------- P3 through recall


def _setup(stub_graphiti_client, stub_memory_graph_adapter, extra1=None):
    stub_graphiti_client.search_scored_results = [("entity-1", "Entity One", 0.85)]
    row = {
        "uuid": "entity-1", "name": "Entity One", "scope": "PERSISTENT", "type": "SEMANTIC",
        "content": "the deploy target", "summary": None, "last_accessed": datetime.now(timezone.utc),
        "edge_count": 1, "sharpness": 0.2, "freshness": "ACTIVE", "user_flagged": False,
    }
    row.update(extra1 or {})
    stub_memory_graph_adapter.candidate_metadata = [row]
    stub_memory_graph_adapter.candidate_provenance_rows = [
        {"uuid": "entity-1", "evidence_node_kinds": ["git"], "anchor_projects": [], "episode_sources": []},
    ]
    return RecallService(
        graphiti_client=stub_graphiti_client, graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_oracle_failure_is_admitted_but_reported_incomplete(
    stub_graphiti_client, stub_memory_graph_adapter, monkeypatch
) -> None:
    svc = _setup(stub_graphiti_client, stub_memory_graph_adapter)

    def _boom(self, query, candidate):
        raise RuntimeError("oracle down")

    monkeypatch.setattr(TemporalOracle, "evaluate", _boom)
    result = await svc.recall(
        "the deploy target", include_warden_status=True,
        tuning=RetrievalTuningConfig(enable_warden_gate=True),
    )
    assert [r.uuid for r in result.results] == ["entity-1"]  # decision stays ADMIT
    assert "oracle_failed" in result.warden_status.metadata_gaps
    assert "incomplete" in result.warden_status.notice()
    assert "incomplete" in result.warden_notice


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_without_oracle_failure_has_no_gap(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    svc = _setup(stub_graphiti_client, stub_memory_graph_adapter)
    result = await svc.recall(
        "the deploy target", include_warden_status=True,
        tuning=RetrievalTuningConfig(enable_warden_gate=True),
    )
    assert [r.uuid for r in result.results] == ["entity-1"]
    assert result.warden_status.state == "applied"
    assert "oracle_failed" not in result.warden_status.metadata_gaps
    assert not result.warden_status.metadata_gaps


# --------------------------------------------------------------------------- P4 unit


def _temporal(meta: dict, intent: str, as_of: str | None = None) -> OracleResult:
    return TemporalOracle().evaluate(
        QueryContext(text="deploy target", intent=intent, as_of_time=as_of),
        CandidateMemory("c", "x", meta),
    )


@pytest.mark.unit
@pytest.mark.parametrize("meta", [
    {"superseded_by": "other"},
    {"artifact_status": "historical"},
    {"superseded_by": "other", "artifact_status": "historical"},
])
def test_node_supersession_is_historicality_support_under_both_intents(meta) -> None:
    cur = _temporal(meta, "current")
    assert (cur.polarity, cur.target) == (OraclePolarity.SUPPORT, OracleTarget.HISTORICALITY)
    assert cur.note == "node_superseded_under_current_intent"
    his = _temporal(meta, "historical")
    assert (his.polarity, his.target) == (OraclePolarity.SUPPORT, OracleTarget.HISTORICALITY)
    assert his.note == "node_superseded_answers_historical"


@pytest.mark.unit
def test_no_node_supersession_is_unchanged() -> None:
    res = _temporal({"created_at": "2026-01-01"}, "current")
    assert res.note == "valid_at_as_of"
    assert (res.polarity, res.target) == (OraclePolarity.SUPPORT, OracleTarget.CURRENTNESS)
    assert "node_superseded" not in _temporal({}, "current").note


@pytest.mark.unit
@pytest.mark.parametrize("meta", [
    {"expired_at": "2026-02-01", "superseded_by": "other"},
    {"superseded": True, "superseded_by": "other"},
    {"expired_at": "2026-02-01", "artifact_status": "historical"},
])
def test_edge_expiry_wins_over_node_supersession_under_current(meta) -> None:
    res = _temporal(meta, "current")
    assert (res.polarity, res.target) == (OraclePolarity.CONTRADICT, OracleTarget.CURRENTNESS)
    assert res.note == "superseded_under_current_intent"


@pytest.mark.unit
def test_as_of_before_created_at_still_not_yet_known_with_node_supersession() -> None:
    res = _temporal({"created_at": "2026-06-01", "superseded_by": "other"}, "current", as_of="2026-01-01")
    assert (res.polarity, res.target) == (OraclePolarity.CONTRADICT, OracleTarget.CURRENTNESS)
    assert "anachronism" in res.note


# --------------------------------------------------------------------------- P4 through recall


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_node_superseded_current_query_flags_not_drops(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    svc = _setup(stub_graphiti_client, stub_memory_graph_adapter,
                 {"superseded_by": "x", "artifact_status": "historical"})
    result = await svc.recall("what is the deploy target",
                              tuning=RetrievalTuningConfig(enable_warden_gate=True))
    assert [r.uuid for r in result.results] == ["entity-1"]
    assert _label(result.results[0].warden_label) == "historical"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_node_superseded_historical_query_is_returned(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    svc = _setup(stub_graphiti_client, stub_memory_graph_adapter,
                 {"superseded_by": "x", "artifact_status": "historical"})
    result = await svc.recall("what did we previously use for the deploy target",
                              tuning=RetrievalTuningConfig(enable_warden_gate=True))
    assert [r.uuid for r in result.results] == ["entity-1"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recall_plain_candidate_is_unlabelled(
    stub_graphiti_client, stub_memory_graph_adapter
) -> None:
    svc = _setup(stub_graphiti_client, stub_memory_graph_adapter)
    result = await svc.recall("what is the deploy target",
                              tuning=RetrievalTuningConfig(enable_warden_gate=True))
    assert [r.uuid for r in result.results] == ["entity-1"]
    assert result.results[0].warden_label is None


# --------------------------------------------------------------------------- P6


@pytest.mark.unit
def test_entity_metadata_fields_select_artifact_type_and_anchors() -> None:
    assert "n['artifact_type'] AS artifact_type" in ENTITY_METADATA_FIELDS
    assert "n['artifact_anchors'] AS anchors" in ENTITY_METADATA_FIELDS


@pytest.mark.unit
def test_intent_oracle_rewards_decision_role_for_explain_decision_query() -> None:
    q = QueryContext(text="why did we choose neo4j")
    typed = CandidateMemory("c", "chose neo4j", {"artifact_type": "decision"})
    plain = CandidateMemory("c", "chose neo4j", {})
    assert derive_content_role(typed.metadata) == {ContentRole.DECISION}
    assert derive_content_role(plain.metadata) == {ContentRole.REFERENCE}

    r_typed = IntentOracle().evaluate(q, typed)
    r_plain = IntentOracle().evaluate(q, plain)
    assert r_typed.polarity is OraclePolarity.SUPPORT
    assert ":decision=" in r_typed.note
    assert r_plain.note.split("=")[0].endswith(":reference")
    assert r_typed.probability > r_plain.probability
