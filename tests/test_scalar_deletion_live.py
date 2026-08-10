"""G15/G20 deletion parity against the isolated test Neo4j (:7688 only)."""

from __future__ import annotations

import os
import uuid as uuidlib
from datetime import datetime, timezone

import pytest

from menhir.domain.typed_assertion import TypedAssertion
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.turn_evidence_repository import TurnEvidenceRepository
from menhir.infrastructure.typed_assertion_repository import TypedAssertionRepository


def _seed_scalar(repo, *, namespace: str) -> dict[str, str]:
    ids = {
        "subject": f"delete-subject-{uuidlib.uuid4().hex}",
        "episode": f"delete-source-episode-{uuidlib.uuid4().hex}",
    }
    repo.execute(
        """
        CREATE (s:Entity {uuid: $subject, name: 'my coins', group_id: $namespace,
                          test_tag: $namespace})
        CREATE (e:Episodic {uuid: $episode, name: 'episode', group_id: $namespace,
                            namespace: $namespace, processing_state: 'COMPLETED',
                            test_tag: $namespace})
        """,
        params={**ids, "namespace": namespace},
    )
    turn_id = TurnEvidenceRepository(repo).record_turn_evidence(
        text="I own 37 coins", role="user", declarant="user", namespace=namespace,
        source_kind="claude_code_hook", session_id=f"{ids['subject']}-session",
        prompt_id=f"{ids['subject']}-prompt",
    )["turn_id"]
    repo.execute(
        "MATCH (e:Episodic {uuid:$episode}), (t:TurnEvidence {turn_id:$turn}) "
        "MERGE (e)-[:ADMITTED_ON]->(t)",
        {"episode": ids["episode"], "turn": turn_id},
    )
    rec = TypedAssertionRepository(repo).record_assertion(TypedAssertion(
        subject_uuid=ids["subject"], subject_display="my coins", attribute="owned", scope="",
        value_kind="count", unit="", operation="absolute", value=37,
        stated_span="I own 37 coins", span_start=0, span_end=len("I own 37 coins"), episode_uuid=turn_id,
        valid_at="2026-07-20T00:00:00+00:00", learned_at="2026-07-20T00:00:00+00:00",
        evidence_tier="user", perceiver_version="v1", namespace=namespace,
    ))
    assert rec["binding_pending"] is False
    ids["assertion"] = rec["assertion_id"]
    ids["turn"] = turn_id
    adapter = MemoryGraphAdapter(neo4j=repo)
    rebuilt = adapter.scalar_state_service(scalar_history_enabled=True).rebuild_scalar_projections(
        ids["subject"], namespace=namespace, history_enabled=True)
    assert rebuilt["complete"] is True
    assert rebuilt["state"]["written"] == 1
    assert rebuilt["history"]["written"] == 1
    return ids


def _assert_history_deleted(repo, *, namespace: str, ids: dict[str, str]) -> None:
    row = repo.execute(
        """
        OPTIONAL MATCH (a:TypedAssertion {assertion_id: $assertion})
        OPTIONAL MATCH (v:Entity {
            view_kind: 'scalar_history', view_subject_uuid: $subject, group_id: $namespace
        })
        WHERE coalesce(v.view_current, true)
        WITH a, collect(DISTINCT v) AS current_history
        RETURN count(DISTINCT a) AS assertions, size(current_history) AS current_history_views
        """,
        params={**ids, "namespace": namespace},
    )[0]
    assert row["assertions"] == 0
    assert row["current_history_views"] == 0
    edge_count = repo.execute(
        """
        OPTIONAL MATCH (v:Entity {view_kind:'scalar_history', group_id:$namespace})
                       -[:HISTORY_ENTRY]->(a:TypedAssertion {assertion_id:$assertion})
        WHERE coalesce(v.view_current, true)
        RETURN count(a) AS current_edges
        """,
        params={"namespace": namespace, "assertion": ids["assertion"]},
    )[0]["current_edges"]
    assert edge_count == 0
    statuses = repo.execute(
        "MATCH (rr:ScalarProjectionRepair {subject_uuid:$subject, namespace:$namespace}) "
        "RETURN collect(DISTINCT rr.status) AS statuses",
        params={"subject": ids["subject"], "namespace": namespace},
    )[0]["statuses"]
    assert statuses == ["complete"]


@pytest.fixture
def scalar_delete_case(test_neo4j_repo):
    namespace = f"test-scalar-delete-{uuidlib.uuid4().hex}"
    ids = _seed_scalar(test_neo4j_repo, namespace=namespace)
    yield test_neo4j_repo, namespace, ids
    test_neo4j_repo.execute(
        "MATCH (n) WHERE n.test_tag = $namespace OR n.namespace = $namespace "
        "OR n.group_id = $namespace DETACH DELETE n",
        params={"namespace": namespace},
    )


@pytest.mark.online
def test_deleting_surfaced_assertion_refolds_and_retires_view(scalar_delete_case):
    repo, namespace, ids = scalar_delete_case
    adapter = MemoryGraphAdapter(neo4j=repo)

    assert adapter.delete_memory(ids["assertion"])

    rows = repo.execute(
        """
        OPTIONAL MATCH (a:TypedAssertion {assertion_id: $assertion})
        OPTIONAL MATCH (v:Entity {view_kind: 'scalar_state', group_id: $namespace})
        WHERE coalesce(v.view_current, true)
        MATCH (rr:ScalarProjectionRepair {subject_uuid: $subject, namespace: $namespace})
        RETURN count(a) AS assertions, count(v) AS current_views,
               collect(DISTINCT rr.status) AS repair_statuses
        """,
        params={**ids, "namespace": namespace},
    )[0]
    assert rows["assertions"] == 0
    assert rows["current_views"] == 0
    assert rows["repair_statuses"] == ["complete"]
    _assert_history_deleted(repo, namespace=namespace, ids=ids)


@pytest.mark.online
def test_structured_authority_provenance_is_relation_labeled_and_expandable(scalar_delete_case):
    repo, namespace, ids = scalar_delete_case
    adapter = MemoryGraphAdapter(neo4j=repo)
    view = adapter.fetch_current_scalar_view_for_slot(
        subject_uuid=ids["subject"], attribute="owned", scope="", value_kind="count",
        unit="", namespace=namespace)
    assert view is not None

    first = adapter.fetch_scalar_authority_contributors(
        view_uuid=view["uuid"], namespace=namespace, limit=1, offset=0)
    exhausted = adapter.fetch_scalar_authority_contributors(
        view_uuid=view["uuid"], namespace=namespace, limit=1, offset=1)

    assert first["contributors"][0]["relation"] == "CURRENT_ANCHOR"
    assert first["contributors"][0]["assertion_id"] == ids["assertion"]
    assert first["total"] == 1 and first["next_offset"] is None
    assert exhausted == {"contributors": [], "total": 1, "next_offset": None}


@pytest.mark.online
def test_deleting_episode_cascades_grounded_assertion_and_retires_view(scalar_delete_case):
    repo, namespace, ids = scalar_delete_case
    adapter = MemoryGraphAdapter(neo4j=repo)

    assert adapter.delete_memory(ids["episode"])

    row = repo.execute(
        """
        OPTIONAL MATCH (e:Episodic {uuid: $episode})
        OPTIONAL MATCH (a:TypedAssertion {assertion_id: $assertion})
        OPTIONAL MATCH (v:Entity {view_kind: 'scalar_state', group_id: $namespace})
        WHERE coalesce(v.view_current, true)
        RETURN count(e) AS episodes, count(a) AS assertions, count(v) AS current_views
        """,
        params={**ids, "namespace": namespace},
    )[0]
    assert row == {"episodes": 0, "assertions": 0, "current_views": 0}
    _assert_history_deleted(repo, namespace=namespace, ids=ids)


@pytest.mark.online
@pytest.mark.skipif(
    not os.getenv("MENHIR_TEST_NEO4J_URI"),
    reason="requires an explicit MENHIR_TEST_NEO4J_URI test instance",
)
def test_deleting_subject_cascades_history_and_completes_repair(scalar_delete_case):
    repo, namespace, ids = scalar_delete_case
    adapter = MemoryGraphAdapter(neo4j=repo)

    assert adapter.delete_memory(ids["subject"])
    assert repo.execute(
        "MATCH (n:Entity {uuid:$subject}) RETURN count(n) AS entities",
        {"subject": ids["subject"]},
    )[0]["entities"] == 0
    _assert_history_deleted(repo, namespace=namespace, ids=ids)


@pytest.mark.online
def test_namespace_delete_includes_scalar_log_and_projection(scalar_delete_case):
    repo, namespace, ids = scalar_delete_case
    adapter = MemoryGraphAdapter(neo4j=repo)

    before = adapter.count_namespace(namespace, namespace=namespace)
    deleted = adapter.delete_namespace(namespace, namespace=namespace)

    assert before >= 5
    assert deleted >= 5
    remaining = repo.execute(
        """
        MATCH (n)
        WHERE n.group_id = $namespace
           OR (n:TypedAssertion AND n.namespace = $namespace)
           OR (n:TypedAssertionHead AND n.namespace = $namespace)
           OR (n:ScalarConsolidationWatermark AND n.namespace = $namespace)
        RETURN count(n) AS remaining
        """,
        params={"namespace": namespace},
    )[0]["remaining"]
    assert remaining == 0
    _assert_history_deleted(repo, namespace=namespace, ids=ids)


@pytest.mark.online
@pytest.mark.skipif(
    not os.getenv("MENHIR_TEST_NEO4J_URI"),
    reason="requires an explicit MENHIR_TEST_NEO4J_URI test instance",
)
def test_scalar_history_namespace_isolation_and_delete_scope(test_neo4j_repo):
    namespace_a = f"test-history-a-{uuidlib.uuid4().hex}"
    namespace_b = f"test-history-b-{uuidlib.uuid4().hex}"
    ids_a = _seed_scalar(test_neo4j_repo, namespace=namespace_a)
    ids_b = _seed_scalar(test_neo4j_repo, namespace=namespace_b)
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)

    histories_a = adapter.list_scalar_history_views_for_namespace(namespace=namespace_a)
    histories_b = adapter.list_scalar_history_views_for_namespace(namespace=namespace_b)
    assert {view["subject_uuid"] for view in histories_a} == {ids_a["subject"]}
    assert {view["subject_uuid"] for view in histories_b} == {ids_b["subject"]}
    assert adapter.fetch_scalar_history(
        subject_uuid=ids_b["subject"], attribute="owned", scope="", value_kind="count",
        unit="", namespace=namespace_a,
    ) is None

    assert adapter.delete_namespace(namespace_a, namespace=namespace_a) >= 1
    assert test_neo4j_repo.execute(
        "MATCH (v:Entity {view_kind:'scalar_history', group_id:$namespace}) "
        "WHERE coalesce(v.view_current,true) RETURN count(v) AS current_views",
        {"namespace": namespace_a},
    )[0]["current_views"] == 0
    assert adapter.fetch_scalar_history(
        subject_uuid=ids_b["subject"], attribute="owned", scope="", value_kind="count",
        unit="", namespace=namespace_b,
    ) is not None
    _assert_history_deleted(test_neo4j_repo, namespace=namespace_a, ids=ids_a)


@pytest.mark.online
def test_due_future_assertion_wakes_and_rebuilds_without_new_ingest(scalar_delete_case):
    repo, namespace, ids = scalar_delete_case
    future_id = f"future-{uuidlib.uuid4().hex}"
    future_source = f"future-source-{uuidlib.uuid4().hex}"
    repo.execute(
        """
        MATCH (s:Entity {uuid: $subject})
        CREATE (h:TypedAssertionHead {source_key: $source_key, namespace: $namespace,
                                      subject_uuid: $subject, test_tag: $namespace})
        CREATE (a:TypedAssertion {
            assertion_id: $assertion, source_key: $source_key, subject_uuid: $subject,
            subject_display: 'my coins', attribute: 'owned', scope: '', value_kind: 'count',
            unit: '', operation: 'absolute', value: 50, value_json: '50',
            stated_span: 'Starting Friday I own 50 coins', episode_uuid: $episode,
            valid_at: datetime('2026-07-27T00:00:00Z'),
            learned_at: datetime('2026-07-22T00:00:00Z'), recorded_at: datetime('2026-07-22T00:00:00Z'),
            evidence_tier: 'user', superseded: false, binding_pending: false,
            activation_pending: true, namespace: $namespace, test_tag: $namespace
        })
        CREATE (h)-[:HAS_VERSION]->(a)
        CREATE (h)-[:CURRENT]->(a)
        CREATE (s)-[:HAS_ASSERTION]->(a)
        """,
        params={**ids, "assertion": future_id, "source_key": future_source,
                "namespace": namespace},
    )
    adapter = MemoryGraphAdapter(neo4j=repo)
    svc = adapter.scalar_state_service()
    before = svc.rebuild_scalar_state(
        ids["subject"], namespace=namespace,
        as_of=datetime(2026, 7, 22, tzinfo=timezone.utc))
    assert before["results"][0]["value"] == 37

    activated = svc.activate_due_assertions(
        as_of=datetime(2026, 7, 27, tzinfo=timezone.utc), namespaces=[namespace])

    assert activated["claimed"] == 1 and not activated["failed"]
    current = adapter.fetch_current_scalar_view_for_slot(
        subject_uuid=ids["subject"], attribute="owned", scope="", value_kind="count",
        unit="", namespace=namespace)
    assert current is not None and str(current["value"]) == "50"
    status = repo.execute(
        "MATCH (rr:ScalarProjectionRepair {operation_kind:'TIME_ACTIVATION', operation_id:$id}) "
        "RETURN rr.status AS status",
        params={"id": future_id},
    )[0]["status"]
    assert status == "complete"
