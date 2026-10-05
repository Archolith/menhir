"""Verifier drift reaches ranked recall and context without rewriting or promoting prose."""

from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.infrastructure.verifier_repository import VerifierRepository
from menhir.mcp.formatters import _compact_scored_item
from menhir.services.context_builder import ContextBuilderService
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService
from menhir.services.verifier_sync import VerifierContext, sync_verifiers


def _binding(namespace: str = "conversation") -> dict:
    return dict(uuid="v1", verifier_kind="env_key", verifier_params={"key": "MENHIR_TEST_DRIFT"},
                register_subject="config", register_counter="enabled", namespace=namespace)


def test_candidate_query_projects_persisted_review_metadata() -> None:
    calls = []
    class Neo4j:
        def execute(self, query, params):
            calls.append(query)
            return []
    MemoryQueryRepository(Neo4j()).fetch_candidate_metadata(["belief"])
    assert "AS needs_review" in calls[0]
    assert "AS review_reason" in calls[0]
    assert "AS review_flagged_at" in calls[0]


class _RegisterStore:
    def __init__(self) -> None:
        self.values = {"conversation": 0.0, "foreign": 0.0}

    def fetch_counter(self, *, subject, counter, namespace):
        return {"value": self.values[namespace]}

    def record_counter(self, *, namespace, value, **kwargs):
        self.values[namespace] = value
        return {"uuid": "reg-" + namespace}


class _LinkedRepo:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.stamps = []

    def list_verifiers(self):
        return [_binding()]

    def flag_referencing_beliefs(self, *, verifier_uuid, new_value, display, at):
        self.rows[0].update(needs_review=True, review_reason="verifier value changed to " + display,
                            review_flagged_at=at)
        return 1

    def ensure_verified_edge(self, **kwargs):
        pass

    def stamp_verifier(self, **kwargs):
        self.stamps.append(kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("observation", ["true", "false", None, "not-a-bool"])
async def test_sync_drift_survives_restart_and_reaches_recall_context(
    observation, monkeypatch, stub_graphiti_client, stub_memory_graph_adapter,
) -> None:
    rows = [dict(uuid="belief", name="old configuration", content="The job is disabled.",
                 scope="PERSISTENT", type="SEMANTIC", freshness="ACTIVE", namespace="conversation")]
    stub_memory_graph_adapter.candidate_metadata = rows
    stub_graphiti_client.search_scored_results = [("belief", "old configuration", .85)]
    store = _RegisterStore()
    if observation is None:
        monkeypatch.delenv("MENHIR_TEST_DRIFT", raising=False)
    else:
        monkeypatch.setenv("MENHIR_TEST_DRIFT", observation)
    repo = _LinkedRepo(rows)
    first = sync_verifiers(repo=repo, graph_adapter=store, context=VerifierContext())
    changed = observation == "true"
    assert store.values["conversation"] == (1.0 if changed else 0.0)
    assert store.values["foreign"] == 0.0
    assert first[0]["status"] == ("refreshed" if observation in ("true", "false") else "source_unavailable")
    if observation in ("true", "false"):
        assert first[0]["changed"] is changed
    else:
        assert not repo.stamps
    # A new service/repository instance sees persisted flags; an unchanged probe cannot clear them.
    restarted = _LinkedRepo(rows)
    second = sync_verifiers(repo=restarted, graph_adapter=store, context=VerifierContext())
    assert not second[0].get("changed", False)
    recall = RecallService(graphiti_client=stub_graphiti_client, graph_adapter=stub_memory_graph_adapter,
                           scoring_service=ScoringService())
    result = await recall.recall("configuration", namespace="conversation")
    memory = result.results[0]
    assert memory.needs_review is changed
    assert memory.content == "The job is disabled."
    for compact in (False, True):
        item = _compact_scored_item(SimpleNamespace(**asdict(memory)), compact=compact)
        assert item.get("needs_review", False) is changed
        if changed:
            assert item["review_reason"] == "verifier value changed to true"
            assert item["review_flagged_at"]
            assert "current truth" in item["review_advisory"]
    context = await ContextBuilderService(recall_service=recall).build_context(
        "configuration", namespace="conversation", max_tokens=1000,
    )
    assert ("Review required:" in context.context) is changed
    if changed:
        assert "Verify the source before asserting this memory as current truth." in context.context


@pytest.mark.online
@pytest.mark.asyncio
async def test_real_graph_verifier_namespace_and_review_projection(
    test_neo4j_repo, monkeypatch, stub_graphiti_client,
) -> None:
    """Exercise real binding MERGEs, edge fences, flag traversal and candidate projection."""
    neo4j = test_neo4j_repo
    repo = VerifierRepository(neo4j)
    adapter = MemoryGraphAdapter(neo4j=neo4j)
    tag = "verifier-" + uuid4().hex
    ns, foreign = tag, tag + "-foreign"
    uuids = []
    try:
        args = dict(kind="env_key", params={"key": "MENHIR_TEST_DRIFT"},
                    register_subject=tag, register_counter="enabled")
        vid = repo.upsert_verifier(**args, namespace=ns)
        foreign_vid = repo.upsert_verifier(**args, namespace=foreign)
        uuids.extend([vid, foreign_vid])
        assert vid != foreign_vid
        assert repo.upsert_verifier(**args, namespace=ns) == vid
        register = adapter.record_counter(subject=tag, counter="enabled", value=0, namespace=ns)
        repo.ensure_verified_edge(register_uuid=register["uuid"], verifier_uuid=vid)
        belief, other = tag + "-belief", tag + "-other"
        uuids.extend([belief, other])
        for uid, silo in ((belief, ns), (other, foreign)):
            neo4j.execute("CREATE (:Entity {uuid:$u, name:$u, content:'The job is disabled.', "
                          "scope:'PERSISTENT', type:'SEMANTIC', freshness:'ACTIVE', namespace:$ns, group_id:$ns})",
                          {"u": uid, "ns": silo})
        assert repo.link_reference(belief_uuid=belief, register_uuid=register["uuid"])
        # A register referencing itself cannot acquire a belief-review flag.
        assert repo.link_reference(belief_uuid=register["uuid"], register_uuid=register["uuid"])
        assert not repo.link_reference(belief_uuid=other, register_uuid=register["uuid"])
        repo.ensure_verified_edge(register_uuid=register["uuid"], verifier_uuid=foreign_vid)
        edges = neo4j.execute("MATCH (r:Entity {uuid:$r})-[:VERIFIED_BY]->(v) RETURN v.uuid AS uuid",
                              {"r": register["uuid"]})
        assert {v["uuid"] for v in edges} == {vid}
        # Legacy poisoned edges must also be fenced on the final flag write.
        neo4j.execute("MATCH (b:Entity {uuid:$b}), (r:Entity {uuid:$r}) CREATE (b)-[:REFERENCES]->(r)",
                      {"b": other, "r": register["uuid"]})
        assert repo.flag_referencing_beliefs(verifier_uuid=vid, new_value=1, display="true",
                                            at="2026-10-05T00:00:00Z") == 1
        rows = {r["uuid"]: r for r in adapter.fetch_candidate_metadata([belief, other, register["uuid"]])}
        assert rows[belief]["needs_review"] is True
        assert rows[belief]["review_flagged_at"]
        assert rows[other]["needs_review"] is not True
        assert rows[register["uuid"]]["needs_review"] is not True
        # Reloaded binding carries its own namespace even when the sync fallback differs.
        binding = next(v for v in VerifierRepository(neo4j).list_verifiers() if v["uuid"] == vid)
        assert binding["namespace"] == ns
        monkeypatch.setenv("MENHIR_TEST_DRIFT", "true")
        class ScopedRepo:
            list_verifiers = lambda self: [binding]
            flag_referencing_beliefs = repo.flag_referencing_beliefs
            ensure_verified_edge = repo.ensure_verified_edge
            stamp_verifier = repo.stamp_verifier
        out = sync_verifiers(repo=ScopedRepo(), graph_adapter=adapter, context=VerifierContext())
        assert out[0]["changed"] is True
        assert adapter.fetch_counter(subject=tag, counter="enabled", namespace=ns)["value"] == 1
        assert adapter.fetch_counter(subject=tag, counter="enabled", namespace=foreign) is None
        stub_graphiti_client.search_scored_results = [(belief, tag, .85), (other, tag, .85)]
        recall = RecallService(graphiti_client=stub_graphiti_client, graph_adapter=adapter,
                               scoring_service=ScoringService())
        result = await recall.recall(tag, namespace=ns)
        assert {m.uuid for m in result.results} == {belief}
        assert result.results[0].needs_review
        context = await ContextBuilderService(recall_service=recall).build_context(tag, namespace=ns)
        assert "Review required:" in context.context
    finally:
        neo4j.execute("MATCH (n) WHERE n.uuid IN $uuids OR n.namespace IN $namespaces DETACH DELETE n",
                      {"uuids": uuids, "namespaces": [ns, foreign]})


@pytest.mark.online
@pytest.mark.parametrize("spelling", [None, "", "default"])
def test_review_edges_accept_legacy_default_silo(test_neo4j_repo, spelling) -> None:
    neo4j = test_neo4j_repo
    repo = VerifierRepository(neo4j)
    tag = "verifier-default-" + uuid4().hex
    ids = [tag + "-belief", tag + "-register"]
    try:
        vid = repo.upsert_verifier(kind="env_key", params={"key": tag}, register_subject=tag,
                                  register_counter="enabled", namespace="default")
        ids.append(vid)
        neo4j.execute("CREATE (:Entity {uuid:$b, namespace:$ns}), "
                      "(:Entity {uuid:$r, namespace:'default', is_view:true})",
                      {"b": ids[0], "r": ids[1], "ns": spelling})
        assert repo.link_reference(belief_uuid=ids[0], register_uuid=ids[1])
        repo.ensure_verified_edge(register_uuid=ids[1], verifier_uuid=vid)
        assert repo.flag_referencing_beliefs(verifier_uuid=vid, new_value=1, display="true",
                                            at="2026-10-05T00:00:00Z") == 1
    finally:
        neo4j.execute("MATCH (n) WHERE n.uuid IN $ids DETACH DELETE n", {"ids": ids})
