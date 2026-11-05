"""Normal Menhir pipeline on disposable Falkor storage; model outputs are controlled."""

import contextlib

import pytest


@pytest.mark.online
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_normal_ingest_enrichment_and_recall(tmp_path, monkeypatch, caplog):
    pytest.importorskip("redislite.falkordb_client")
    import httpx
    from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
    from graphiti_core.embedder.openai import OpenAIEmbedder

    from menhir.config import MemorySettings
    from menhir.core.bootstrap import build_memory_services, prepare_memory_runtime
    from menhir.domain import IngestStatus, new_session
    from menhir.infrastructure.falkor import FalkorLiteRepository
    from menhir.infrastructure.graphiti_llm_adapter import MenhirOpenAIGenericClient

    model_calls = []

    async def no_http(*args, **kwargs):
        raise AssertionError(
            "external HTTP is forbidden in the controlled pipeline test"
        )

    async def model_response(self, messages, response_model=None, **kwargs):
        name = getattr(response_model, "__name__", None)
        model_calls.append(name)
        entities = [
            {"name": "Atlas", "entity_type_id": 0},
            {"name": "Python", "entity_type_id": 0},
        ]
        responses = {
            "CombinedExtraction": {
                "extracted_entities": entities,
                "edges": [
                    {
                        "source_entity_name": "Atlas",
                        "target_entity_name": "Python",
                        "relation_type": "USES",
                        "fact": "Atlas uses Python.",
                        "episode_indices": [0],
                    }
                ],
            },
            "ExtractedEntities": {"extracted_entities": entities},
            "EntitySummary": {"summary": "Atlas uses Python."},
            "BatchEdgeTimestamps": {
                "timestamps": [{"valid_at": "2026-11-05T00:00:00Z", "invalid_at": None}]
            },
            "EdgeTimestamps": {"valid_at": "2026-11-05T00:00:00Z", "invalid_at": None},
        }
        assert name in responses, (
            f"unhandled controlled model response: {name}; {kwargs}"
        )
        return responses[name]

    async def embedding(self, input_data):
        if isinstance(input_data, list) and len(input_data) == 1:
            input_data = input_data[0]
        return (
            [0.0, 1.0, 0.0] if str(input_data).strip() == "Python" else [1.0, 0.0, 0.0]
        )

    async def embeddings(self, input_data_list):
        return [await embedding(self, value) for value in input_data_list]

    async def rank(self, query, passages):
        return [(passage, 1.0) for passage in passages]

    monkeypatch.setattr(httpx.AsyncClient, "send", no_http)
    monkeypatch.setattr(MenhirOpenAIGenericClient, "_generate_response", model_response)
    monkeypatch.setattr(OpenAIEmbedder, "create", embedding)
    monkeypatch.setattr(OpenAIEmbedder, "create_batch", embeddings)
    monkeypatch.setattr(OpenAIRerankerClient, "rank", rank)
    repo = FalkorLiteRepository(tmp_path / "pipeline.rdb")
    built = None
    try:
        built = build_memory_services(
            MemorySettings(local_llm_embed_model="controlled"), graph_repository=repo
        )
        built.ingest_service._max_enrichment_attempts = 1
        await prepare_memory_runtime(built)
        result = await built.ingest_service.ingest_episode(
            "Atlas uses Python.",
            new_session("test-user", session_id="falkor-pipeline"),
            "integration-test",
            namespace="tenant-a",
        )
        assert result.status is IngestStatus.INGESTED, (
            result,
            model_calls,
            repo.execute(
                "MATCH (n) RETURN labels(n) AS labels,n.uuid AS uuid,n.namespace AS namespace,n.group_id AS group_id,n.source AS source"
            ),
        )
        assert result.nodes_touched >= 2 and result.edges_touched >= 1
        assert model_calls, "the test bypassed model-backed enrichment"
        recalled = await built.recall_service.recall(
            "What does Atlas use?",
            namespace="tenant-a",
            include_session=True,
            session_id="falkor-pipeline",
        )
        assert recalled.results and recalled.search_error is None
        foreign = await built.recall_service.recall(
            "What does Atlas use?",
            namespace="tenant-b",
            include_session=True,
            session_id="falkor-pipeline",
        )
        assert not foreign.results and foreign.search_error is None
        assert await built.ingest_service.shutdown() == 0
        assert not [
            record
            for record in caplog.records
            if record.exc_info or record.levelno >= 40
        ]
        assert repo._get_db().client.execute_command("GRAPH.LIST") == ["menhir"]
    finally:
        if built is not None:
            with contextlib.suppress(Exception):
                await built.ingest_service.shutdown()
            await built.graphiti_client.close()
        elif repo._driver is not None:
            await repo._driver.close()
        repo.close()


@pytest.fixture
def native_repo(tmp_path):
    pytest.importorskip("redislite.falkordb_client")
    from menhir.infrastructure.falkor import FalkorLiteRepository

    repo = FalkorLiteRepository(tmp_path / "contracts.rdb")
    try:
        yield repo
    finally:
        repo.close()


@pytest.mark.online
def test_claim_time_hydration_and_stale_attempt_fences(native_repo):
    from datetime import UTC, datetime, timedelta

    from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository
    from menhir.infrastructure.episode_stamping import EpisodeStampingRepository

    lifecycle = EpisodeLifecycleRepository()
    lifecycle.neo4j = native_repo
    stamping = EpisodeStampingRepository()
    stamping.neo4j = native_repo
    event_time = datetime(2025, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
    lifecycle.create_pending_episode(
        episode_uuid="queued",
        name="event",
        content="Atlas uses Python.",
        session_id="s",
        user_id="u",
        source="test",
        source_confidence=1.0,
        namespace="tenant-a",
        reference_time=event_time,
    )
    claim = lifecycle.claim_pending_episode(
        "queued", max_attempts=3, worker_id="worker", lease_seconds=30
    )
    assert claim["reference_time"] == event_time
    instant = claim["processing_started_at"]
    assert isinstance(instant, datetime) and instant.tzinfo == UTC
    assert claim["processing_lease_expires_at"] > instant
    stale = instant - timedelta(microseconds=1)
    assert not lifecycle.mark_episode_pending(
        "queued",
        worker_id="worker",
        transient_requeue=True,
        claim_started_at=stale,
    )
    assert not stamping.touch_episode_processing_heartbeat(
        "queued",
        worker_id="worker",
        claim_started_at=stale,
        lease_seconds=60,
        max_enrichment_seconds=120,
    )
    assert stamping.touch_episode_processing_heartbeat(
        "queued",
        worker_id="worker",
        claim_started_at=instant,
        lease_seconds=60,
        max_enrichment_seconds=120,
    )
    old = instant - timedelta(seconds=200)
    native_repo.execute(
        "MATCH (n:Episodic {uuid:'queued'}) SET n.processing_started_at=$old",
        {"old": old.isoformat()},
    )
    before = native_repo.execute(
        "MATCH (n:Episodic {uuid:'queued'}) RETURN n.processing_lease_expires_at AS lease"
    )[0]
    assert stamping.touch_episode_processing_heartbeat(
        "queued",
        worker_id="worker",
        claim_started_at=old,
        lease_seconds=300,
        max_enrichment_seconds=120,
    )
    assert (
        native_repo.execute(
            "MATCH (n:Episodic {uuid:'queued'}) RETURN n.processing_lease_expires_at AS lease"
        )[0]
        == before
    )
    native_repo.execute(
        "MATCH (n:Episodic {uuid:'queued'}) SET n.processing_started_at=$instant",
        {"instant": instant.isoformat()},
    )
    assert lifecycle.mark_episode_pending(
        "queued",
        worker_id="worker",
        transient_requeue=True,
        claim_started_at=instant,
        retry_after_s=60,
    )
    row = native_repo.execute(
        "MATCH (n:Episodic {uuid:'queued'}) RETURN n.processing_attempts AS attempts, "
        "n.transient_retries AS retries, n.retry_after AS retry_after"
    )[0]
    assert row["attempts"] == 0 and row["retries"] == 1
    assert row["retry_after"] > instant.isoformat()
    assert (
        lifecycle.claim_pending_episode(
            "queued", max_attempts=3, worker_id="worker", lease_seconds=30
        )
        is None
    )


@pytest.mark.online
def test_native_view_receipts_fail_closed(native_repo):
    from menhir.domain.recall_visibility import default_recall_visibility_cypher
    from menhir.infrastructure.memory_queries import MemoryQueryRepository

    native_repo.execute("""
        CREATE (v:Entity {uuid:'view', group_id:'tenant-a', is_view:true, view_class:'FACT',
                          view_audience:'RECALL', view_current:true, episode_uuids:['e']})
        CREATE (:Episodic {uuid:'e', group_id:'tenant-a'})-[:MENTIONS]->(v)
    """)
    predicate = default_recall_visibility_cypher("n", incoming_evidence="evidence")

    def visible():
        return native_repo.execute(
            "MATCH (n:Entity {uuid:'view'}) CALL { WITH n OPTIONAL MATCH (e)-[:MENTIONS]->(n) "
            "RETURN collect(e) AS evidence } WITH n,evidence WHERE "
            + predicate
            + " RETURN n.uuid AS uuid"
        )

    assert visible()
    assert MemoryQueryRepository(native_repo).fetch_candidate_metadata(["view"])[0][
        "view_provenance_live"
    ]
    native_repo.execute("MATCH (v:Entity {uuid:'view'}) SET v.episode_uuids=['e','e']")
    assert not visible()
    native_repo.execute(
        "MATCH (v:Entity {uuid:'view'}) SET v.episode_uuids=['missing']"
    )
    assert not visible()
    native_repo.execute("MATCH (v:Entity {uuid:'view'}) SET v.episode_uuids=['e']")
    native_repo.execute("MATCH (e:Episodic {uuid:'e'}) SET e.namespace='tenant-b'")
    assert not visible()
    native_repo.execute(
        "MATCH (e:Episodic {uuid:'e'}) SET e.namespace='tenant-a' REMOVE e:Episodic"
    )
    assert not visible()
    native_repo.execute("MATCH (e {uuid:'e'}) SET e:Episodic")
    assert visible()
    native_repo.execute("MATCH (e {uuid:'e'})-[r:MENTIONS]->() DELETE r")
    assert not visible()
    native_repo.execute(
        "MATCH (v:Entity {uuid:'view'}) CREATE (:Episodic {uuid:'e',namespace:'tenant-a'})-[:MENTIONS]->(v), "
        "(:TurnEvidence {turn_id:'extra',namespace:'tenant-a'})-[:MENTIONS]->(v)"
    )
    assert not visible()


@pytest.mark.online
def test_native_provenance_entities_facts_and_receipts(native_repo):
    from menhir.infrastructure.memory_queries import MemoryQueryRepository

    native_repo.execute("""
        CREATE (n:Entity {uuid:'n',group_id:'tenant-a'}), (other:Entity {uuid:'other',group_id:'tenant-a'}),
               (e:Episodic {uuid:'e',group_id:'tenant-a',source:'message',valid_at:'2026-02-01T00:00:00+00:00'}),
               (a:Episodic {uuid:'receipt',namespace:'tenant-a',source:'user',resolved_episode_uuid:'e',
                            reference_time:'2026-01-01T00:00:00+00:00'}),
               (t:TurnEvidence {turn_id:'turn',namespace:'tenant-a'}),
               (e)-[:MENTIONS]->(n), (a)-[:ADMITTED_ON]->(t),
               (n)-[:RELATES_TO {uuid:'fact',group_id:'tenant-a',episodes:['e','e']}]->(other),
               (:Episodic {uuid:'no-receipt',group_id:'tenant-a',source:'agent',
                            valid_at:'2025-01-01T00:00:00+00:00'})-[:MENTIONS]->(n)
    """)
    queries = MemoryQueryRepository(native_repo)
    rows = {
        r["uuid"]: r
        for r in queries.fetch_result_provenance(
            ["n", "fact", "receipt", "unknown"], "tenant-a"
        )
    }
    assert set(rows) == {"n", "fact", "receipt"}
    assert set(rows["n"]["sources"]) == {"agent", "user"}
    assert rows["n"]["first_seen_at"].startswith("2025-01-01")
    for row in rows.values():
        assert row["turn_evidence_ids"] == ["turn"], rows
    assert queries.fetch_result_provenance(["n", "fact", "receipt"], "tenant-b") == []


@pytest.fixture
def binding_refresh(native_repo):
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
    from menhir.infrastructure.structure_write_fence import IdentityClaim

    claim = IdentityClaim(
        project_id="project", root_key="root", generation=1, host="host"
    )
    native_repo.execute("""
        CREATE (:ProjectIdentity {project_id:'project', state:'bound', bound_host:'host',
                                 root_key:'root', claim_generation:1}),
               (:Entity {uuid:'project-node', structure_project:'example', structure_role:'project',
                         structure_project_id:'project', scan_fingerprint:'same', indexed_commit:'old'}),
               (:Entity {uuid:'other-node', structure_project:'example', structure_role:'project',
                         structure_project_id:'other', scan_fingerprint:'same', indexed_commit:'other'})
    """)
    adapter = MemoryGraphAdapter(native_repo)
    token = adapter.begin_structure_scan(claim)
    return adapter, claim, token


def _binding_rows(repo):
    return repo.execute(
        "MATCH (n:Entity) RETURN n.uuid AS uuid, n.indexed_commit AS commit, "
        "n.indexed_repository AS repository, n.indexed_dirty AS dirty ORDER BY uuid"
    )


def _assert_binding_slots_released(repo):
    assert repo.execute(
        "MATCH (p:ProjectIdentity {project_id:'project'}), (f:StructureWriteFence {id:'singleton'}) "
        "RETURN coalesce(p.active_writers,[]) AS identity_slots, f.writers AS fence_slots"
    ) == [{"identity_slots": [], "fence_slots": []}]


@pytest.mark.online
def test_native_binding_refresh_cas_and_identity_isolation(
    native_repo, binding_refresh
):
    adapter, claim, token = binding_refresh
    assert adapter.refresh_indexed_binding(
        "example",
        "same",
        "new",
        "origin",
        False,
        claim=claim,
        scan_generation=token,
    )
    assert not adapter.refresh_indexed_binding(
        "example",
        "same",
        "new",
        "origin",
        False,
        claim=claim,
        scan_generation=token,
    )
    before = _binding_rows(native_repo)
    assert before == [
        {"uuid": "other-node", "commit": "other", "repository": None, "dirty": None},
        {
            "uuid": "project-node",
            "commit": "new",
            "repository": "origin",
            "dirty": False,
        },
    ]
    assert not adapter.refresh_indexed_binding(
        "example",
        "replaced",
        "stale",
        "origin",
        True,
        claim=claim,
        scan_generation=token,
    )
    assert _binding_rows(native_repo) == before
    _assert_binding_slots_released(native_repo)


@pytest.mark.online
@pytest.mark.parametrize(
    "change",
    [
        "p.claim_generation=2",
        "p.root_key='elsewhere'",
        "p.bound_host='elsewhere'",
        "p.state='superseded'",
        "p.structure_scan_generation=2",
    ],
)
def test_native_binding_refresh_revalidates_after_admission(
    native_repo, binding_refresh, monkeypatch, change
):
    from menhir.infrastructure.structure_write_fence import StaleStructureScan

    adapter, claim, token = binding_refresh
    original = native_repo.execute
    before = _binding_rows(native_repo)

    def transferred(query, params=None, **kwargs):
        if "CALL { WITH p" in query:
            original("MATCH (p:ProjectIdentity {project_id:'project'}) SET " + change)
        return original(query, params, **kwargs)

    monkeypatch.setattr(native_repo, "execute", transferred)
    with pytest.raises(StaleStructureScan):
        adapter.refresh_indexed_binding(
            "example",
            "same",
            "unauthorized",
            "origin",
            False,
            claim=claim,
            scan_generation=token,
        )
    assert _binding_rows(native_repo) == before
    _assert_binding_slots_released(native_repo)


@pytest.mark.online
def test_native_binding_refresh_freeze_and_scan_order(native_repo, binding_refresh):
    from menhir.infrastructure.structure_write_fence import (
        StaleStructureScan,
        StructureWritesFrozen,
        fence_status,
        lower_fence,
        raise_fence,
    )

    adapter, claim, old = binding_refresh
    token = adapter.begin_structure_scan(claim)
    before = _binding_rows(native_repo)
    with pytest.raises(StaleStructureScan):
        adapter.refresh_indexed_binding(
            "example",
            "same",
            "old",
            "origin",
            False,
            claim=claim,
            scan_generation=old,
        )
    with pytest.raises(StaleStructureScan):
        adapter.refresh_indexed_binding(
            "example",
            "same",
            "invalid",
            "origin",
            False,
            claim=claim,
            scan_generation=0,
        )
    raise_fence(native_repo, reason="binding refresh test")
    assert fence_status(native_repo)["frozen"]
    with pytest.raises(StructureWritesFrozen):
        adapter.refresh_indexed_binding(
            "example",
            "same",
            "frozen",
            "origin",
            False,
            claim=claim,
            scan_generation=token,
        )
    assert _binding_rows(native_repo) == before
    _assert_binding_slots_released(native_repo)

    lower_fence(native_repo)
    assert not fence_status(native_repo)["frozen"]
    assert adapter.refresh_indexed_binding(
        "example",
        "same",
        "unfrozen",
        "origin",
        False,
        claim=claim,
        scan_generation=token,
    )
    _assert_binding_slots_released(native_repo)


@pytest.mark.online
def test_native_binding_refresh_late_failure_rolls_back(
    native_repo, binding_refresh, monkeypatch
):
    from redis.exceptions import ResponseError

    adapter, claim, token = binding_refresh
    before = _binding_rows(native_repo)
    original = native_repo.execute
    dispatched = []

    def fail_after_refresh(query, params=None, **kwargs):
        if "CALL { WITH p" in query:
            dispatched.append(query)
            # The zero step depends on the count returned AFTER SET, rather than a
            # parse-time or constant-folded error that could precede the mutation.
            query = (
                query.removesuffix("RETURN updated")
                + "WITH updated RETURN range(0,1,updated - 1) AS injected_failure"
            )
        return original(query, params, **kwargs)

    monkeypatch.setattr(native_repo, "execute", fail_after_refresh)
    with pytest.raises(ResponseError, match="(?i)step|increment"):
        adapter.refresh_indexed_binding(
            "example",
            "same",
            "must-rollback",
            "origin",
            True,
            claim=claim,
            scan_generation=token,
        )
    assert len(dispatched) == 1
    assert _binding_rows(native_repo) == before
    _assert_binding_slots_released(native_repo)


@pytest.mark.parametrize(
    "target,has_claim,generation",
    [
        ("project", False, 1),
        ("other", True, 1),
        ("project", True, None),
        ("project", True, 0),
    ],
)
def test_native_binding_refresh_requires_guard_before_dispatch(
    tmp_path, monkeypatch, target, has_claim, generation
):
    from menhir.infrastructure.falkor import FalkorLiteRepository
    from menhir.infrastructure.structure_queries import StructureGraphWriter
    from menhir.infrastructure.structure_write_fence import (
        IdentityClaim,
        StaleStructureScan,
    )

    repo = FalkorLiteRepository(tmp_path / "never-opened.rdb")
    claim = (
        IdentityClaim(project_id="project", root_key="root", generation=1, host="host")
        if has_claim
        else None
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("an unguarded native mutation was dispatched")

    monkeypatch.setattr(repo, "execute", forbidden)
    with pytest.raises(StaleStructureScan):
        StructureGraphWriter(repo).refresh_indexed_binding(
            "example",
            "same",
            "new",
            "origin",
            False,
            project_id=target,
            claim=claim,
            scan_generation=generation,
        )
    repo.close()


@pytest.fixture
def document_publication(native_repo):
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
    from menhir.infrastructure.project_identity_binding import (
        binding_host,
        root_key_for,
    )

    root = "/disposable/documents"
    native_repo.execute(
        "CREATE (:ProjectIdentity {project_id:'project', state:'bound', bound_host:$host, "
        "root_key:$root, claim_generation:1})",
        {"host": binding_host(), "root": root_key_for(root)},
    )
    adapter = MemoryGraphAdapter(native_repo)

    def publish(content):
        adapter.write_document(
            root + "/guide.md",
            content,
            project="example",
            structure_path="docs/guide.md",
            project_id="project",
            identity_generation=1,
            identity_root=root,
            session_id="session",
            user_id="user",
            document_type="reference_article",
        )

    return publish


def _document_rows(repo):
    return repo.execute(
        "MATCH (n:Entity {structure_role:'document'}) RETURN properties(n) AS props ORDER BY n.uuid"
    )


@pytest.mark.online
def test_native_document_publication_create_update_and_scan_independence(
    native_repo, document_publication
):
    document_publication("first")
    before = _document_rows(native_repo)[0]["props"]
    assert before["structure_project_id"] == "project"
    assert before["structure_path"] == "docs/guide.md"
    assert before["source"] == "document-ingest"
    assert before["document_type"] == "reference_article"
    assert before["content"] == "first"
    native_repo.execute(
        "MATCH (n:Entity {uuid:$uuid}) SET n.user_flagged=true, n.summary='retained', n.group_id='tenant'",
        {"uuid": before["uuid"]},
    )
    # Documents hold the same identity lock but do not require a filesystem-scan token.
    native_repo.execute("MATCH (p:ProjectIdentity) SET p.structure_scan_generation=99")
    document_publication("updated")
    rows = _document_rows(native_repo)
    assert len(rows) == 1
    after = rows[0]["props"]
    assert after["content"] == "updated" and after["uuid"] == before["uuid"]
    assert after["created_at"] == before["created_at"]
    assert (
        after["user_flagged"]
        and after["summary"] == "retained"
        and after["group_id"] == "tenant"
    )
    assert after["root_path"] == "/disposable/documents/guide.md"
    _assert_binding_slots_released(native_repo)


@pytest.mark.online
@pytest.mark.parametrize(
    "change",
    [
        "p.claim_generation=2",
        "p.root_key='elsewhere'",
        "p.bound_host='elsewhere'",
        "p.state='superseded'",
    ],
)
def test_native_document_publication_revalidates_claim(
    native_repo, document_publication, monkeypatch, change
):
    from menhir.infrastructure.structure_write_fence import StaleStructureScan

    document_publication("original")
    before = _document_rows(native_repo)
    original = native_repo.execute

    def transferred(query, params=None, **kwargs):
        if "last_publication_probe" in query:
            original("MATCH (p:ProjectIdentity) SET " + change)
        return original(query, params, **kwargs)

    monkeypatch.setattr(native_repo, "execute", transferred)
    with pytest.raises(StaleStructureScan):
        document_publication("unauthorized")
    assert _document_rows(native_repo) == before
    _assert_binding_slots_released(native_repo)


@pytest.mark.online
@pytest.mark.parametrize("existing", [False, True])
def test_native_document_publication_late_failure_rolls_back(
    native_repo, document_publication, monkeypatch, existing
):
    from redis.exceptions import ResponseError

    if existing:
        document_publication("original")
    before = _document_rows(native_repo)
    original = native_repo.execute
    dispatched = []

    def fail_after_merge(query, params=None, **kwargs):
        if "last_publication_probe" in query:
            dispatched.append(query)
            assert query.endswith("RETURN n.uuid AS uuid")
            query = query.removesuffix("RETURN n.uuid AS uuid") + (
                "WITH n RETURN range(0,1,size(n.content)-size($content)) AS injected_failure"
            )
        return original(query, params, **kwargs)

    monkeypatch.setattr(native_repo, "execute", fail_after_merge)
    with pytest.raises(ResponseError, match="(?i)step|increment"):
        document_publication("must-rollback")
    assert len(dispatched) == 1
    assert _document_rows(native_repo) == before
    _assert_binding_slots_released(native_repo)


@pytest.mark.online
def test_native_document_publication_respects_migration_freeze(
    native_repo, document_publication
):
    from menhir.infrastructure.structure_write_fence import (
        StructureWritesFrozen,
        lower_fence,
        raise_fence,
    )

    raise_fence(native_repo, reason="document test")
    with pytest.raises(StructureWritesFrozen):
        document_publication("frozen")
    assert not _document_rows(native_repo)
    _assert_binding_slots_released(native_repo)
    lower_fence(native_repo)
    document_publication("allowed")
    assert len(_document_rows(native_repo)) == 1
    _assert_binding_slots_released(native_repo)


@pytest.mark.parametrize("target,has_claim", [("project", False), ("other", True)])
def test_native_document_publication_requires_claim_before_dispatch(
    tmp_path, monkeypatch, target, has_claim
):
    from menhir.infrastructure.falkor import FalkorLiteRepository
    from menhir.infrastructure.structure_queries import StructureGraphWriter
    from menhir.infrastructure.structure_write_fence import (
        IdentityClaim,
        StaleStructureScan,
    )

    repo = FalkorLiteRepository(tmp_path / "never-opened.rdb")
    claim = (
        IdentityClaim(project_id="project", root_key="root", generation=1, host="host")
        if has_claim
        else None
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("unguarded native document write dispatched")

    monkeypatch.setattr(repo, "execute", forbidden)
    with pytest.raises(StaleStructureScan):
        StructureGraphWriter(repo).write_document(
            "/doc.md",
            "content",
            project="example",
            structure_path="doc.md",
            structure_project_id=target,
            session_id="s",
            user_id="u",
            claim=claim,
        )
    repo.close()


@pytest.mark.online
def test_native_scalar_view_create_refresh_supersede_and_retire(native_repo):
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
    from menhir.infrastructure.view_repository import ViewRepository

    adapter = MemoryGraphAdapter(native_repo)
    assert not adapter.scalar_state_schema_ready()
    adapter.activate_scalar_state()
    assert adapter.scalar_state_schema_ready()

    native_repo.execute("""
        CREATE (:TurnEvidence {turn_id:'turn1',namespace:'tenant-a',evidence_finalized:true,evidence_generation:0}),
               (:TurnEvidence {turn_id:'turn2',namespace:'tenant-a',evidence_finalized:true,evidence_generation:0})
    """)
    views = ViewRepository(native_repo)
    slot = {
        "subject": "Atlas",
        "subject_uuid": "subject",
        "attribute": "owned",
        "scope": "",
        "value_kind": "count",
        "unit": "",
        "namespace": "tenant-a",
    }
    first = views.record_scalar_state(
        **slot,
        value=20,
        valid_at="2026-01-01T00:00:00Z",
        episode_uuids=["turn1"],
        name_embedding=[1.0, 0.0],
    )
    assert first["created"]
    assert native_repo.execute(
        "MATCH (n:Entity {uuid:$uuid}) RETURN vec.cosineDistance(n.name_embedding,vecf32([1.0,0.0])) AS distance",
        {"uuid": first["uuid"]},
    ) == [{"distance": 0.0}]
    refreshed = views.record_scalar_state(
        **slot,
        value=20,
        valid_at="2026-01-01T00:00:00Z",
        episode_uuids=["turn1", "turn2"],
    )
    assert refreshed["uuid"] == first["uuid"]
    current = views.fetch_scalar_state(
        **{k: v for k, v in slot.items() if k != "subject"}
    )
    assert current["recall_eligible"]
    assert native_repo.execute(
        "MATCH (n:Entity {uuid:$uuid}) RETURN n.episode_uuids AS episodes",
        {"uuid": first["uuid"]},
    ) == [{"episodes": ["turn1", "turn2"]}]
    replacement = views.record_scalar_state(
        **slot, value=37, valid_at="2026-01-02T00:00:00Z", episode_uuids=["turn2"]
    )
    assert replacement["superseded"] and replacement["uuid"] != first["uuid"]
    assert views.retire_scalar_state(view_key=replacement["view_key"])
    assert (
        views.fetch_scalar_state(**{k: v for k, v in slot.items() if k != "subject"})
        is None
    )
    assert native_repo.execute(
        "MATCH (n:Entity {view_kind:'scalar_state'}) RETURN count(n) AS versions, "
        "sum(CASE WHEN n.ss_view_key_current IS NOT NULL THEN 1 ELSE 0 END) AS current_keys"
    ) == [{"versions": 2, "current_keys": 0}]


@pytest.mark.online
def test_native_scalar_assertion_fold_and_rebuild(native_repo):
    from menhir.domain.typed_assertion import TypedAssertion
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    adapter = MemoryGraphAdapter(native_repo)
    adapter.activate_scalar_state()
    native_repo.execute("""
        CREATE (:Entity {uuid:'subject',name:'Atlas',group_id:'tenant-a'}),
               (:Episodic {uuid:'episode',namespace:'tenant-a'})-[:ADMITTED_ON]->(:TurnEvidence {turn_id:'turn',uuid:'turn',namespace:'tenant-a',role:'user',declarant:'user',
                               evidence_finalized:true,evidence_generation:0})
    """)
    assertion = TypedAssertion(
        subject_uuid="subject",
        subject_display="Atlas",
        attribute="owned",
        scope="",
        value_kind="count",
        unit="",
        operation="absolute",
        value=20,
        stated_span="Atlas owns 20.",
        episode_uuid="turn",
        valid_at="2026-01-01T00:00:00Z",
        learned_at="2026-01-01T00:00:00Z",
        namespace="tenant-a",
        evidence_tier="user",
        name_embedding=[1.0, 0.0],
        embed_version="controlled",
    )
    result = adapter.record_typed_assertion(assertion)
    assert result["created"] and not result["binding_pending"]
    observations = adapter._memory_queries.search_assertion_embeddings(
        [1.0, 0.0], namespaces=["tenant-a"]
    )
    assert [r["assertion_id"] for r in observations] == [result["assertion_id"]]
    rebuilt = adapter.scalar_state_service().rebuild_scalar_state(
        "subject", namespace="tenant-a"
    )
    assert rebuilt is not None
    rows = native_repo.execute(
        "MATCH (n:Entity {view_kind:'scalar_state',view_subject_uuid:'subject'}) "
        "WHERE n.view_current RETURN n.ss_value AS value,n.episode_uuids AS evidence"
    )
    assert rows == [{"value": "20", "evidence": ["turn"]}]
    assert adapter.delete_memory("turn")
    assert native_repo.execute(
        "MATCH (n:Entity {view_kind:'scalar_state'}) RETURN count(n.ss_view_key_current) AS current"
    ) == [{"current": 0}]
    assert native_repo.execute(
        "MATCH (r:ScalarProjectionRepair) RETURN r.status AS status"
    ) == [{"status": "complete"}]


@pytest.mark.online
def test_native_scalar_activation_refuses_legacy_and_write_refuses_stale_evidence(
    native_repo,
):
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
    from menhir.infrastructure.typed_assertion_repository import (
        ScalarStateActivationError,
    )
    from menhir.infrastructure.view_repository import ViewRepository

    adapter = MemoryGraphAdapter(native_repo)
    native_repo.execute("CREATE (:TypedAssertion {assertion_id:'legacy'})")
    with pytest.raises(ScalarStateActivationError):
        adapter.activate_scalar_state()
    assert not native_repo.execute("CALL db.constraints()")
    native_repo.execute("MATCH (a:TypedAssertion) DELETE a")
    adapter.activate_scalar_state()
    adapter.activate_scalar_state()
    native_repo.execute(
        "CREATE (:TurnEvidence {turn_id:'e',namespace:'tenant-a',evidence_finalized:true,evidence_generation:0})"
    )
    views = ViewRepository(native_repo)
    slot = {
        "subject": "Atlas",
        "subject_uuid": "subject",
        "attribute": "owned",
        "scope": "",
        "value_kind": "count",
        "unit": "",
        "namespace": "tenant-a",
    }
    first = views.record_scalar_state(**slot, value=20, episode_uuids=["e"])
    for change in (
        "e.evidence_quarantined=true",
        "e.namespace='tenant-b'",
        "e.evidence_generation=1",
    ):
        native_repo.execute("MATCH (e:TurnEvidence) SET " + change)
        with pytest.raises(ValueError):
            views.record_scalar_state(**slot, value=37, episode_uuids=["e"])
        assert native_repo.execute(
            "MATCH (n:Entity {view_kind:'scalar_state'}) RETURN count(n) AS versions"
        ) == [{"versions": 1}]
        native_repo.execute(
            "MATCH (e:TurnEvidence) SET e.namespace='tenant-a',e.evidence_quarantined=false,e.evidence_generation=0"
        )
    assert (
        views.fetch_scalar_state(**{k: v for k, v in slot.items() if k != "subject"})[
            "uuid"
        ]
        == first["uuid"]
    )


@pytest.mark.online
def test_native_null_namespace_marker_is_a_closed_compatibility_gate(native_repo):
    from redis.exceptions import ResponseError

    with pytest.raises(ResponseError, match="Failed to parse query parameter"):
        native_repo.execute(
            "CREATE (:Entity {uuid:'must-not-write'}) RETURN $marker AS marker",
            {"marker": "\u0000null"},
        )
    assert native_repo.execute(
        "MATCH (n:Entity {uuid:'must-not-write'}) RETURN count(n) AS count"
    ) == [{"count": 0}]


@pytest.mark.online
@pytest.mark.parametrize(
    "namespace", [None, "", "default", "雪", "N", "Vdefault", "\\u0000null"]
)
def test_native_repair_identity_roundtrip_and_reconcile_replay(native_repo, namespace):
    from menhir.infrastructure.falkor import repair_key, repair_key_expression
    from menhir.infrastructure.typed_assertion_repository import (
        TypedAssertionRepository,
    )

    repo = TypedAssertionRepository(native_repo)
    repo.activate_scalar_state()
    expression = repair_key_expression("$op", "$kind", "$namespace", "$subject")
    expected = repair_key("op", "MEMORY_DELETE", namespace, "subject")
    assert native_repo.execute(
        f"RETURN {expression} AS key",
        {
            "op": "op",
            "kind": "MEMORY_DELETE",
            "namespace": namespace,
            "subject": "subject",
        },
    ) == [{"key": expected}]
    repo.record_reconcile_intent(
        operation_id="op", operation_kind="ENTITY_UNMERGE", namespace=namespace
    )
    assert not repo.reconcile_complete(
        "op", operation_kind="ENTITY_UNMERGE", namespace=namespace
    )
    repo.record_reconcile_receipt(
        operation_id="op", operation_kind="ENTITY_UNMERGE", namespace=namespace
    )
    repo.record_reconcile_intent(
        operation_id="op", operation_kind="ENTITY_UNMERGE", namespace=namespace
    )
    assert repo.reconcile_complete(
        "op", operation_kind="ENTITY_UNMERGE", namespace=namespace
    )
    rows = native_repo.execute(
        "MATCH (r:ScalarReconcile) RETURN r.receipt_key AS key, r.status AS status"
    )
    assert rows == [
        {"key": repair_key("op", "ENTITY_UNMERGE", namespace), "status": "complete"}
    ]
    repo.activate_scalar_state()  # Repeated activation accepts the exact persisted representation.


@pytest.mark.online
@pytest.mark.parametrize(
    "label,key",
    [("ScalarProjectionRepair", "repair_key"), ("ScalarReconcile", "receipt_key")],
)
@pytest.mark.parametrize("identity", [None, "legacy", "fn1:malformed"])
def test_native_activation_refuses_incompatible_repair_ids_before_ddl(
    native_repo, label, key, identity
):
    from menhir.infrastructure.typed_assertion_repository import (
        TypedAssertionRepository,
    )

    native_repo.execute(
        f"CREATE (r:{label} {{{key}:$key, operation_id:'op', operation_kind:'MEMORY_DELETE', subject_uuid:'s'}})",
        {"key": identity},
    )
    before = native_repo.execute("CALL db.indexes()")
    with pytest.raises(RuntimeError, match="incompatible.*identities"):
        TypedAssertionRepository(native_repo).activate_scalar_state()
    assert native_repo.execute("CALL db.indexes()") == before
    assert native_repo.execute(f"MATCH (r:{label}) RETURN r.{key} AS key") == [
        {"key": identity}
    ]


@pytest.mark.online
@pytest.mark.parametrize("namespace", [None, "default", "雪", "\\u0000null"])
def test_native_scalar_erasure_creates_replayable_repair(native_repo, namespace):
    from menhir.infrastructure.falkor import repair_key
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    adapter = MemoryGraphAdapter(native_repo)
    adapter.activate_scalar_state()
    native_repo.execute(
        """
        CREATE (:Episodic {uuid:'erase',namespace:$namespace}),
               (:Entity {uuid:'subject',name:'Atlas'}),
               (:TypedAssertion {assertion_id:'a',source_key:'source',subject_uuid:'subject',episode_uuid:'erase',
                                 namespace:$namespace}),
               (:TypedAssertionHead {source_key:'source'})
    """,
        {"namespace": namespace},
    )
    result = adapter._memory_queries.delete_memory_with_scalar_cascade(
        "erase", operation_id="op"
    )
    assert result["assertions_deleted"] == 1
    assert result["heads_deleted"] == 1
    repairs = adapter._typed_assertions.pending_projection_repairs(operation_id="op")
    assert len(repairs) == 1
    assert repairs[0]["repair_key"] == repair_key(
        "op", "MEMORY_DELETE", namespace, "subject"
    )
    repaired = adapter.scalar_state_service().repair_pending_deletions(
        operation_id="op"
    )
    assert repaired["failed"] == []
    assert repaired["repaired"] == [repairs[0]["repair_key"]]
    assert not adapter._typed_assertions.pending_projection_repairs(operation_id="op")


@pytest.mark.online
def test_native_namespace_erasure_and_due_activation_use_native_repair_ids(native_repo):
    from menhir.infrastructure.falkor import repair_key
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    adapter = MemoryGraphAdapter(native_repo)
    adapter.activate_scalar_state()
    native_repo.execute("""
        CREATE (:TypedAssertion {assertion_id:'due',subject_uuid:'subject',namespace:'tenant-a',
            valid_at:'2026-01-01T00:00:00+00:00',activation_pending:true}),
            (:TypedAssertion {assertion_id:'other',subject_uuid:'other',namespace:'tenant-b'}),
            (:TurnEvidence {turn_id:'rawturn',namespace:'tenant-a'}),
            (:Entity {uuid:'cross-view',group_id:'tenant-b',is_view:true,view_kind:'scalar_state',
                view_current:true,ss_view_key_current:'cross',episode_uuids:['rawturn','keepturn']}),
            (h:TypedEventAssertionHead)-[:HAS_VERSION]->(:TypedEventAssertion {episode_uuid:'rawturn',namespace:'tenant-a'}),
            (h)-[:HAS_VERSION]->(:TypedEventAssertion {episode_uuid:'keepturn',namespace:'tenant-b'})
    """)
    native_repo.execute("""
        MATCH (v:Entity {uuid:'cross-view'}),(a:TypedAssertion {assertion_id:'due'}),
              (b:TypedAssertion {assertion_id:'other'})
        CREATE (v)-[:CURRENT_ANCHOR]->(a),(v)-[:CONTRIBUTED_TO]->(b)
    """)
    assertions = adapter._typed_assertions
    due = assertions.claim_due_scalar_activations(
        as_of="2026-02-01T00:00:00Z", namespaces=["tenant-a"]
    )
    assert [r["repair_key"] for r in due] == [
        repair_key("due", "TIME_ACTIVATION", "tenant-a", "subject")
    ]
    assert not assertions.claim_due_scalar_activations(
        as_of="2026-02-01T00:00:00Z", namespaces=["tenant-a"]
    )
    result = adapter._memory_queries.delete_namespace_with_scalar_cascade(
        "tenant-a", "tenant-a", operation_id="op"
    )
    assert result["deleted"] == 3
    assert result["dependent_views_retired"] == 1
    assert native_repo.execute(
        "MATCH (v:Entity {uuid:'cross-view'}) RETURN v.retired AS retired,v.episode_uuids AS evidence"
    ) == [{"retired": True, "evidence": ["keepturn"]}]
    assert native_repo.execute(
        "MATCH (h:TypedEventAssertionHead)-[:HAS_VERSION]->(e:TypedEventAssertion) RETURN e.namespace AS namespace"
    ) == [{"namespace": "tenant-b"}]
    assert native_repo.execute(
        "MATCH (r:ViewProjectionRepair) RETURN r.status AS status,r.reconstructible AS reconstructible"
    ) == [{"status": "pending", "reconstructible": True}]
    repairs = assertions.pending_projection_repairs(operation_id="op")
    assert [r["repair_key"] for r in repairs] == [
        repair_key("op", "NAMESPACE_DELETE", "tenant-a", "subject")
    ]
    assert native_repo.execute(
        "MATCH (a:TypedAssertion) RETURN a.assertion_id AS id"
    ) == [{"id": "other"}]


@pytest.mark.online
@pytest.mark.parametrize("namespace_delete", [False, True])
def test_native_erasure_late_failure_rolls_back_repairs_and_deletion(
    native_repo, monkeypatch, namespace_delete
):
    from redis.exceptions import ResponseError

    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    adapter = MemoryGraphAdapter(native_repo)
    adapter.activate_scalar_state()
    native_repo.execute("""
        CREATE (:Episodic {uuid:'erase',namespace:'tenant-a'}),
               (:TypedAssertion {assertion_id:'a',subject_uuid:'s',episode_uuid:'erase',namespace:'tenant-a'})
    """)
    original = native_repo.execute
    snapshot = "MATCH (n) RETURN id(n) AS id,labels(n) AS labels,properties(n) AS props ORDER BY id"
    before = original(snapshot)
    dispatched = []
    return_clause = (
        "RETURN size(doomed) AS deleted, repairs,"
        if namespace_delete
        else "RETURN assertions_deleted, memory_touched, repairs,"
    )
    count_expression = "size(doomed)" if namespace_delete else "assertions_deleted"

    def fail_at_return(query, params=None, **kwargs):
        if return_clause in query:
            dispatched.append(query)
            query = query.replace(
                return_clause,
                f"RETURN range(0,1,{count_expression}-{count_expression}) AS injected_failure, "
                + return_clause.removeprefix("RETURN "),
            )
        return original(query, params, **kwargs)

    monkeypatch.setattr(native_repo, "execute", fail_at_return)
    with pytest.raises(ResponseError, match="(?i)step|increment"):
        if namespace_delete:
            adapter._memory_queries.delete_namespace_with_scalar_cascade(
                "tenant-a", "tenant-a", operation_id="op"
            )
        else:
            adapter._memory_queries.delete_memory_with_scalar_cascade(
                "erase", operation_id="op"
            )
    assert len(dispatched) == 1
    assert original(snapshot) == before


@pytest.mark.online
def test_native_repair_receipts_survive_reopen_and_preserve_namespace_isolation(
    native_repo,
):
    from menhir.infrastructure.falkor import FalkorLiteRepository, repair_key
    from menhir.infrastructure.typed_assertion_repository import (
        TypedAssertionRepository,
    )

    repo = TypedAssertionRepository(native_repo)
    repo.activate_scalar_state()
    for ns in (None, "N", "default"):
        repo.record_reconcile_intent(
            operation_id="op", operation_kind="ENTITY_UNMERGE", namespace=ns
        )
    repo.record_reconcile_receipt(
        operation_id="op", operation_kind="ENTITY_UNMERGE", namespace=None
    )
    native_repo.close()
    reopened = FalkorLiteRepository(native_repo.path)
    try:
        repo = TypedAssertionRepository(reopened)
        repo.activate_scalar_state()
        assert repo.reconcile_complete(
            "op", operation_kind="ENTITY_UNMERGE", namespace=None
        )
        assert not repo.reconcile_complete(
            "op", operation_kind="ENTITY_UNMERGE", namespace="N"
        )
        repo.record_reconcile_receipt(
            operation_id="op", operation_kind="ENTITY_UNMERGE", namespace="N"
        )
        rows = reopened.execute(
            "MATCH (r:ScalarReconcile) RETURN r.receipt_key AS key,r.status AS status ORDER BY key"
        )
        assert rows == sorted(
            [
                {
                    "key": repair_key("op", "ENTITY_UNMERGE", ns),
                    "status": "pending" if ns == "default" else "complete",
                }
                for ns in (None, "N", "default")
            ],
            key=lambda r: r["key"],
        )
    finally:
        reopened.close()


@pytest.mark.online
def test_native_memory_erasure_preserves_surviving_view_sources_and_event_head(
    native_repo,
):
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    adapter = MemoryGraphAdapter(native_repo)
    adapter.activate_scalar_state()
    native_repo.execute("""
        CREATE (ep:Episodic {uuid:'erase',namespace:'tenant-a'})-[:ADMITTED_ON]->
               (:TurnEvidence {turn_id:'turn',namespace:'tenant-a'}),
               (a:TypedAssertion {assertion_id:'doomed',subject_uuid:'subject',episode_uuid:'turn',namespace:'tenant-a'}),
               (b:TypedAssertion {assertion_id:'keeper',subject_uuid:'subject',episode_uuid:'keep',namespace:'tenant-a'}),
               (v:Entity {uuid:'view',is_view:true,view_kind:'scalar_state',view_current:true}),
               (ep)-[:MENTIONS]->(v),(v)-[:CURRENT_ANCHOR]->(a),(v)-[:CONTRIBUTED_TO]->(b),
               (ep)-[:FOUNDS]->(e:TypedEventAssertion {episode_uuid:'turn',namespace:'tenant-a'}),
               (h:TypedEventAssertionHead)-[:HAS_VERSION]->(e),
               (h)-[:HAS_VERSION]->(:TypedEventAssertion {episode_uuid:'keep',namespace:'tenant-a'})
    """)
    result = adapter._memory_queries.delete_memory_with_scalar_cascade(
        "erase", operation_id="op"
    )
    assert result["assertions_deleted"] == 1
    assert result["dependent_views_retired"] == 1
    assert result["event_assertions_deleted"] == 1
    assert result["event_heads_deleted"] == 0
    assert native_repo.execute(
        "MATCH (r:ViewProjectionRepair) RETURN r.status AS status,r.reconstructible AS reconstructible"
    ) == [{"status": "pending", "reconstructible": True}]
    assert native_repo.execute(
        "MATCH (a:TypedAssertion) RETURN a.assertion_id AS id"
    ) == [{"id": "keeper"}]


@pytest.mark.online
def test_native_correlation_path_veto_uses_shared_role_policy(native_repo):
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    adapter = MemoryGraphAdapter(native_repo)
    native_repo.execute(
        "CREATE (:Entity {uuid:'a',name:'ordinary'}),(:Entity {uuid:'b',name:'ordinary'})"
    )
    for name, expected in (
        ("ordinary", False),
        ("thing.py", True),
        ("thing.PY", True),
        ("docs/readme", False),
        ("thing.pyx", False),
    ):
        native_repo.execute(
            "MATCH (n:Entity {uuid:'b'}) SET n.name=$name", {"name": name}
        )
        assert adapter.check_ineligible_node_veto("a", "b") is expected, name
    native_repo.execute(
        "MATCH (n:Entity {uuid:'b'}) SET n.name='ordinary',n.is_view=true"
    )
    assert adapter.check_ineligible_node_veto("a", "b")
