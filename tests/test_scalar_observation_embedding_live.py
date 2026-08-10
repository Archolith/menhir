"""Live proof for Phase 4a.1 (plan menhir-observation-nodes-and-view-authority-recall, 7.H): the
RESUMABLE, VERSIONED backfill embeds each :TypedAssertion's stated_span into a name_embedding.

This is the observation-recall foundation (7.B=Option 1: the assertion IS the recall node, needing a
name_embedding from stated_span). Recall-neutral -- it writes a property nothing reads until the
observation lane (4a.2). The backfill is resumable by construction (embedded rows drop out of the
selection) and versioned (a model change bumps embed_version -> re-embed). Online (:7688); a FakeNeo4j
cannot evaluate the selection/UNWIND write (G7). `embed` is injected, so the test uses a deterministic
fake vector (no provider/network).
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.domain.typed_assertion import TypedAssertion
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.typed_assertion_repository import TypedAssertionRepository


@pytest.fixture
def live_repo(test_neo4j_repo):
    MemoryGraphAdapter(neo4j=test_neo4j_repo).activate_scalar_state()
    return TypedAssertionRepository(test_neo4j_repo)


def _seed(repo, raw, *, ns, entity, n):
    """Create n bound absolute assertions (distinct episode+span => distinct source_key) in namespace ns.
    `repo` is the TypedAssertionRepository (record_assertion); `raw` is the Neo4jRepository (MERGE)."""
    raw.execute("MERGE (e:Entity {uuid:$u}) SET e.name=$u", {"u": entity})
    for i in range(n):
        ep = f"ep-{uuidlib.uuid4().hex[:8]}"
        raw.execute("MERGE (e:Episodic {uuid:$u})", {"u": ep})
        span = f"I own {10 + i} rare coins"
        repo.record_assertion(TypedAssertion(
            subject_uuid=entity, subject_display="user", attribute="owned", scope="",
            value_kind="count", unit="", operation="absolute", value=10 + i,
            stated_span=span, span_start=0, span_end=len(span), episode_uuid=ep,
            valid_at="2026-07-01T00:00:00+00:00", learned_at="2026-07-01T00:00:00+00:00",
            evidence_tier="agent", perceiver_version="v1", namespace=ns,
        ))


def _emb_rows(repo, ns):
    return repo.execute(
        "MATCH (a:TypedAssertion {namespace:$ns}) "
        "RETURN a.embed_version AS v, size(coalesce(a.name_embedding, [])) AS dim",
        {"ns": ns},
    )


FAKE = [0.1, 0.2, 0.3]


@pytest.mark.online
def test_write_time_embedding_is_searchable_and_survives_reperception(live_repo, test_neo4j_repo):
    # 4a.1 write-time: an assertion recorded WITH a name_embedding is immediately searchable (no
    # backfill), and a later re-perception WITHOUT an embedder (ON MATCH) does NOT overwrite it.
    from menhir.infrastructure.memory_queries import MemoryQueryRepository
    queries = MemoryQueryRepository(test_neo4j_repo)
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    ent = f"ent-{uuidlib.uuid4().hex[:8]}"
    ep = f"ep-{uuidlib.uuid4().hex[:8]}"
    test_neo4j_repo.execute("MERGE (n:Entity {uuid:$u}) SET n.name=$u", {"u": ent})
    test_neo4j_repo.execute("MERGE (e:Episodic {uuid:$u})", {"u": ep})
    span = "I own 20 rare coins"

    def _mk(embedding, embed_version):
        return TypedAssertion(
            subject_uuid=ent, subject_display="user", attribute="owned", scope="", value_kind="count",
            unit="", operation="absolute", value=20, stated_span=span, span_start=0, span_end=len(span),
            episode_uuid=ep, valid_at="2026-07-01T00:00:00+00:00",
            learned_at="2026-07-01T00:00:00+00:00", evidence_tier="agent", perceiver_version="v1",
            namespace=ns, name_embedding=embedding, embed_version=embed_version)

    rec = live_repo.record_assertion(_mk([1.0, 0.0, 0.0], "m1"))
    aid = rec["assertion_id"]
    # immediately searchable via the observation lane (no backfill ran).
    hits = queries.search_assertion_embeddings([1.0, 0.0, 0.0], namespaces=[ns])
    assert [h["assertion_id"] for h in hits] == [aid]
    assert _emb_rows(test_neo4j_repo, ns)[0] == {"v": "m1", "dim": 3}

    # a re-perception with NO embedder (same source_key -> ON MATCH) must not wipe the embedding.
    live_repo.record_assertion(_mk(None, None))
    assert _emb_rows(test_neo4j_repo, ns)[0] == {"v": "m1", "dim": 3}
    assert [h["assertion_id"] for h in
            queries.search_assertion_embeddings([1.0, 0.0, 0.0], namespaces=[ns])] == [aid]


@pytest.mark.online
def test_backfill_embeds_all_then_idempotent_then_reembeds_on_version_bump(live_repo, test_neo4j_repo):
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    _seed(live_repo, test_neo4j_repo, ns=ns, entity=f"ent-{uuidlib.uuid4().hex[:8]}", n=3)

    r1 = live_repo.backfill_assertion_embeddings(lambda t: FAKE, embed_version="v1", namespace=ns)
    assert r1 == {"embedded": 3, "remaining": 0}
    rows = _emb_rows(test_neo4j_repo, ns)
    assert all(row["v"] == "v1" and row["dim"] == 3 for row in rows), rows

    # idempotent: nothing left to embed on a second pass.
    assert live_repo.backfill_assertion_embeddings(lambda t: FAKE, embed_version="v1", namespace=ns) \
        == {"embedded": 0, "remaining": 0}

    # version bump re-selects everything (a model change must re-embed).
    r3 = live_repo.backfill_assertion_embeddings(lambda t: [0.4, 0.5], embed_version="v2", namespace=ns)
    assert r3 == {"embedded": 3, "remaining": 0}
    rows = _emb_rows(test_neo4j_repo, ns)
    assert all(row["v"] == "v2" and row["dim"] == 2 for row in rows), rows


@pytest.mark.online
def test_backfill_is_bounded_and_resumes(live_repo, test_neo4j_repo):
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    _seed(live_repo, test_neo4j_repo, ns=ns, entity=f"ent-{uuidlib.uuid4().hex[:8]}", n=3)

    r1 = live_repo.backfill_assertion_embeddings(lambda t: FAKE, embed_version="v1", namespace=ns, batch=2)
    assert r1 == {"embedded": 2, "remaining": 1}
    r2 = live_repo.backfill_assertion_embeddings(lambda t: FAKE, embed_version="v1", namespace=ns, batch=2)
    assert r2 == {"embedded": 1, "remaining": 0}


@pytest.mark.online
def test_failed_embed_leaves_row_for_retry(live_repo, test_neo4j_repo):
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    _seed(live_repo, test_neo4j_repo, ns=ns, entity=f"ent-{uuidlib.uuid4().hex[:8]}", n=2)

    # provider error -> embed returns None -> no write, rows stay selected (no partial/corrupt state).
    assert live_repo.backfill_assertion_embeddings(lambda t: None, embed_version="v1", namespace=ns) \
        == {"embedded": 0, "remaining": 2}
    # a later pass with a working embedder finishes them.
    assert live_repo.backfill_assertion_embeddings(lambda t: FAKE, embed_version="v1", namespace=ns) \
        == {"embedded": 2, "remaining": 0}
