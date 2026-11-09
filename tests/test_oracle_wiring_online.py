"""Online trace check: oracle/warden wiring through RecallService.recall on a labelled graph.

The fixture is written with raw Cypher into the throwaway test Neo4j (no embedder, no LLM).
Candidate generation is the Graphiti stub (``search_scored_results``), so reachability is fixed
by the test; everything after it (metadata fetch, oracle pipeline, warden chain, status receipt)
is the real production path against real graph rows.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from menhir.domain.recall import WardenLabel
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService

pytestmark = [pytest.mark.online, pytest.mark.asyncio]

_BASE = (
    "scope:'PERSISTENT', type:'SEMANTIC', freshness:'ACTIVE', "
    "namespace:$ns, group_id:$ns, test_tag:$tag"
)


@pytest.fixture
def graph(test_neo4j_repo, stub_graphiti_client):
    tag = f"oracle-wiring-{uuid4().hex}"
    ns = tag
    ids = {k: f"{tag}-{k}" for k in ("old", "new", "decision", "conflict", "auth")}
    p = {"ns": ns, "tag": tag, **ids}
    test_neo4j_repo.execute(
        f"""
        CREATE (:Entity {{uuid:$new, name:'deploy target is hetzner',
                content:'The deploy target is hetzner.', {_BASE}}})
        CREATE (:Entity {{uuid:$old, name:'deploy target is fly.io',
                content:'The deploy target is fly.io.', superseded_by:$new,
                artifact_status:'historical', {_BASE}}})
        CREATE (:Entity {{uuid:$decision, name:'decision: choose hetzner',
                content:'We chose hetzner because it is cheaper and EU hosted.',
                artifact_type:'decision', {_BASE}}})
        CREATE (:Entity {{uuid:$conflict, name:'api used the auth token',
                content:'The billing api used the old auth token.',
                conflict_group_id:$tag, conflict_status:'unresolved', {_BASE}}})
        CREATE (:Entity {{uuid:$auth, name:'auth tokens notes',
                content:'Auth tokens rotate every 30 days.', {_BASE}}})
        """,
        params=p,
    )
    svc = RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=MemoryGraphAdapter(neo4j=test_neo4j_repo),
        scoring_service=ScoringService(),
    )
    try:
        yield svc, stub_graphiti_client, ns, ids
    finally:
        test_neo4j_repo.execute(
            "MATCH (n) WHERE n.test_tag = $tag DETACH DELETE n", params={"tag": tag}
        )


def _assert_healthy(result) -> None:
    assert result.search_error is None
    status = result.warden_status
    assert status is not None
    assert status.state != "failed"
    assert "oracle_failed" not in status.metadata_gaps


async def _recall(svc, stub, ns, query, *uuids):
    stub.search_scored_results = [(u, u, 0.85) for u in uuids]
    return await svc.recall(
        query, namespace=ns, include_warden_status=True, trace=True, update_access=False
    )


async def test_current_query_returns_current_and_does_not_refuse_superseded(graph) -> None:
    svc, stub, ns, ids = graph
    result = await _recall(svc, stub, ns, "what is the deploy target", ids["old"], ids["new"])
    _assert_healthy(result)
    by_uuid = {m.uuid: m for m in result.results}
    assert ids["new"] in by_uuid
    # Spec: a superseded artifact is labelled historical, never silently refused.
    assert ids["old"] in by_uuid, "superseded artifact was refused under a current query"
    assert by_uuid[ids["old"]].warden_label == WardenLabel.HISTORICAL
    assert by_uuid[ids["new"]].warden_label is None
    if result.trace is not None and result.trace.assertion_shadow is not None:
        assert result.trace.assertion_shadow.intent == "current"


async def test_historical_query_does_not_refuse_superseded(graph) -> None:
    svc, stub, ns, ids = graph
    result = await _recall(
        svc, stub, ns, "what was the deploy target previously", ids["old"], ids["new"]
    )
    _assert_healthy(result)
    assert ids["old"] in {m.uuid for m in result.results}
    if result.trace is not None and result.trace.assertion_shadow is not None:
        assert result.trace.assertion_shadow.intent == "historical"


async def test_decision_artifact_is_returned_for_why_query(graph) -> None:
    # NOTE: the recall result/trace does not expose the IntentOracle role (the shadow trace
    # carries only intent/decision/score/label), so only presence + health is asserted here.
    svc, stub, ns, ids = graph
    result = await _recall(svc, stub, ns, "why did we choose hetzner", ids["decision"], ids["new"])
    _assert_healthy(result)
    assert ids["decision"] in {m.uuid for m in result.results}


@pytest.mark.parametrize(
    "query", ["which api used the old auth token", "distill the notes on auth tokens"]
)
async def test_false_positive_cue_queries_do_not_degrade(graph, query) -> None:
    svc, stub, ns, ids = graph
    result = await _recall(svc, stub, ns, query, ids["auth"], ids["conflict"])
    _assert_healthy(result)
    assert ids["auth"] in {m.uuid for m in result.results}


async def test_unresolved_conflict_is_refused_for_old_auth_token_query(graph) -> None:
    svc, stub, ns, ids = graph
    result = await _recall(
        svc, stub, ns, "which api used the old auth token", ids["auth"], ids["conflict"]
    )
    _assert_healthy(result)
    assert ids["conflict"] not in {m.uuid for m in result.results}
    assert result.warden_status.refused >= 1
