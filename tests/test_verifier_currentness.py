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
from menhir.domain.recall import parse_verifier_evidence


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
    assert "AS verifier_evidence" in calls[0]


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


class _FreshnessRepo(_LinkedRepo):
    def __init__(self, rows: list[dict], *, kind="env_key") -> None:
        super().__init__(rows)
        self.kind = kind
        if not rows[0].get("verifier_evidence"):
            rows[0]["verifier_evidence"] = [dict(verifier_uuid="v1", register_subject="config",
                                                register_counter="enabled")]

    def list_verifiers(self):
        return [{**_binding(), "verifier_kind": self.kind}]

    def stamp_probe(self, *, verifier_uuid, status, at):
        self.rows[0]["verifier_evidence"][0].update(last_probe_status=status, last_probe_at=at)

    def stamp_verifier(self, *, verifier_uuid, value, display, at):
        self.stamps.append(dict(value=value, display=display, at=at))
        self.rows[0]["verifier_evidence"][0].update(
            value=value, display=display, last_verified_at=at, last_probe_at=at, last_probe_status="refreshed",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unavailable", "invalid", "unknown", "executor", "register", "write"])
async def test_last_success_survives_later_probe_failure_and_restart(
    failure, monkeypatch, stub_graphiti_client, stub_memory_graph_adapter,
) -> None:
    from menhir.services import verifier_sync
    time = ["2026-10-05T00:00:00+00:00"]
    monkeypatch.setattr(verifier_sync, "_utc_now_iso", lambda: time[0])
    rows = [dict(uuid="belief", name="configuration", content="The job is disabled.",
                 scope="PERSISTENT", type="SEMANTIC", freshness="ACTIVE", namespace="conversation")]
    store = _RegisterStore()
    monkeypatch.setenv("MENHIR_TEST_DRIFT", "true")
    sync_verifiers(repo=_FreshnessRepo(rows), graph_adapter=store, context=VerifierContext())
    # Same value is successfully re-observed after a restart without clearing the prose review flag.
    time[0] = "2026-10-05T00:01:00+00:00"
    unchanged = sync_verifiers(repo=_FreshnessRepo(rows), graph_adapter=store, context=VerifierContext())
    assert not unchanged[0]["changed"]
    assert rows[0]["needs_review"]
    time[0] = "2026-10-05T00:02:00+00:00"
    repo = _FreshnessRepo(rows, kind="untrusted" if failure == "unknown" else "env_key")
    def boom(*args, **kwargs):
        raise RuntimeError("private probe details must not reach recall")
    kwargs = {}
    if failure == "unavailable":
        monkeypatch.delenv("MENHIR_TEST_DRIFT")
    elif failure == "invalid":
        monkeypatch.setenv("MENHIR_TEST_DRIFT", "invalid")
    elif failure == "executor":
        kwargs["executors"] = {"env_key": boom}
    elif failure == "register":
        store.fetch_counter = boom
    elif failure == "write":
        store.record_counter = boom
    if failure == "write":
        with pytest.raises(RuntimeError):
            sync_verifiers(repo=repo, graph_adapter=store, context=VerifierContext(), **kwargs)
    else:
        sync_verifiers(repo=repo, graph_adapter=store, context=VerifierContext(), **kwargs)
    expected_status = dict(unavailable="source_unavailable", invalid="source_unavailable",
                           unknown="skipped_unknown_kind", executor="error", register="error", write="pending")[failure]
    stub_graphiti_client.search_scored_results = [("belief", "configuration", .85)]
    stub_memory_graph_adapter.candidate_metadata = rows
    recall = RecallService(graphiti_client=stub_graphiti_client, graph_adapter=stub_memory_graph_adapter,
                           scoring_service=ScoringService())
    result = await recall.recall("config", namespace="conversation")
    evidence = result.results[0].verifier_evidence[0]
    assert evidence.value == 1.0 and evidence.display == "true"
    assert evidence.last_verified_at == "2026-10-05T00:01:00+00:00"
    assert evidence.last_probe_at == time[0] and evidence.last_probe_status == expected_status
    for compact in (True, False):
        item = _compact_scored_item(SimpleNamespace(**asdict(result.results[0])), compact=compact)
        assert item["verifier_evidence"][0]["last_probe_status"] == expected_status
        assert "private probe details" not in str(item)
    builder = ContextBuilderService(recall_service=recall)
    context = await builder.build_context("config", namespace="conversation", max_tokens=2000)
    assert "config.enabled = true" in context.context
    assert "last successful verification: 2026-10-05T00:01:00+00:00" in context.context
    assert f"latest probe: {expected_status}" in context.context
    assert "does not verify the memory's prose" in context.context
    for budget in (1, 25):
        small = await builder.build_context("config", namespace="conversation", max_tokens=budget)
        assert ("The job is disabled." in small.context) == ("Register observation:" in small.context)


def test_first_unavailable_probe_never_creates_success(monkeypatch) -> None:
    monkeypatch.delenv("MENHIR_TEST_DRIFT", raising=False)
    rows = [{}]
    repo = _FreshnessRepo(rows)
    sync_verifiers(repo=repo, graph_adapter=_RegisterStore(), context=VerifierContext())
    evidence = parse_verifier_evidence(rows[0]["verifier_evidence"])[0]
    assert evidence.value is None and evidence.last_verified_at is None
    assert "unverified" in evidence.context_line() and "verification: never" in evidence.context_line()
    assert evidence.last_probe_status == "source_unavailable"


def test_duplicate_binding_and_legacy_unknown_probe_evidence() -> None:
    row = dict(verifier_uuid="v1", register_subject="config", register_counter="enabled",
               value=0.0, display="false", last_verified_at="2026-10-05T00:00:00Z")
    evidence = parse_verifier_evidence([row, row])
    assert len(evidence) == 1 and evidence[0].value == 0.0
    assert "latest probe: unknown at unknown" in evidence[0].context_line()
    assert parse_verifier_evidence(None) == ()


@pytest.mark.parametrize("failure", ["missing_register", "refused_edge"])
def test_unconfirmed_register_link_cannot_stamp_success(monkeypatch, failure) -> None:
    monkeypatch.setenv("MENHIR_TEST_DRIFT", "true")
    rows = [{}]
    repo, store = _FreshnessRepo(rows), _RegisterStore()
    if failure == "missing_register":
        store.record_counter = lambda **kwargs: {}
    else:
        repo.ensure_verified_edge = lambda **kwargs: False
    result = sync_verifiers(repo=repo, graph_adapter=store, context=VerifierContext())
    assert result[0]["status"] == "error" and not repo.stamps
    evidence = parse_verifier_evidence(rows[0]["verifier_evidence"])[0]
    assert evidence.last_verified_at is None and evidence.last_probe_status == "error"


def test_probe_stamp_cannot_claim_success() -> None:
    class Neo4j:
        def execute(self, *args, **kwargs):
            pytest.fail("invalid status must not reach the datastore")
    with pytest.raises(ValueError):
        VerifierRepository(Neo4j()).stamp_probe(verifier_uuid="v1", status="refreshed", at="2026-10-05T00:00:00Z")


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
            stamp_probe = repo.stamp_probe
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
        evidence = result.results[0].verifier_evidence
        assert len(evidence) == 1 and evidence[0].value == 1.0
        assert evidence[0].last_probe_status == "refreshed" and evidence[0].last_verified_at
        # Duplicate paths via old/current register versions must yield one binding observation.
        current = adapter.fetch_counter(subject=tag, counter="enabled", namespace=ns)
        assert repo.link_reference(belief_uuid=belief, register_uuid=current["uuid"])
        assert len((await recall.recall(tag, namespace=ns)).results[0].verifier_evidence) == 1
        monkeypatch.delenv("MENHIR_TEST_DRIFT")
        assert sync_verifiers(repo=ScopedRepo(), graph_adapter=adapter, context=VerifierContext())[0]["status"] == "source_unavailable"
        failed = (await recall.recall(tag, namespace=ns)).results[0].verifier_evidence[0]
        assert failed.last_verified_at == evidence[0].last_verified_at and failed.value == 1.0
        assert failed.last_probe_status == "source_unavailable"
        foreign_row = adapter.fetch_candidate_metadata([other])[0]
        assert foreign_row["verifier_evidence"] == []
        assert adapter.fetch_candidate_metadata([current["uuid"]])[0]["verifier_evidence"] == []
        context = await ContextBuilderService(recall_service=recall).build_context(tag, namespace=ns)
        assert "Review required:" in context.context
        assert "latest probe: source_unavailable" in context.context
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
