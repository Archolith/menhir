"""Live proof for Phase 4a.2 (plan menhir-observation-nodes-and-view-authority-recall, G12/7.H): the
observation candidate lane -- :TypedAssertion observations are searchable by embedding and hydratable,
which the Entity-only pipeline (fetch_candidate_metadata matches (n:Entity)) cannot do.

Online (:7688) because it exercises Neo4j's vector.similarity.cosine and the materializable filter a
FakeNeo4j cannot evaluate (G7). Embeddings are SET directly for deterministic cosine ordering (no
provider/network).
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.domain.typed_assertion import TypedAssertion
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.infrastructure.typed_assertion_repository import TypedAssertionRepository
from menhir.infrastructure.view_repository import ViewRepository


@pytest.fixture
def lane(test_neo4j_repo):
    MemoryGraphAdapter(neo4j=test_neo4j_repo).activate_scalar_state()
    return TypedAssertionRepository(test_neo4j_repo), MemoryQueryRepository(test_neo4j_repo)


def _mk(raw, *, ns, entity, span, embedding):
    ep = f"ep-{uuidlib.uuid4().hex[:8]}"
    raw.execute("MERGE (e:Entity {uuid:$u}) SET e.name=$u", {"u": entity})
    raw.execute("MERGE (e:Episodic {uuid:$u})", {"u": ep})
    repo = TypedAssertionRepository(raw)
    res = repo.record_assertion(TypedAssertion(
        subject_uuid=entity, subject_display="user", attribute="owned", scope="",
        value_kind="count", unit="", operation="absolute", value=20,
        stated_span=span, span_start=0, span_end=len(span), episode_uuid=ep,
        valid_at="2026-07-01T00:00:00+00:00", learned_at="2026-07-01T00:00:00+00:00",
        evidence_tier="agent", perceiver_version="v1", namespace=ns,
    ))
    aid = res["assertion_id"]
    raw.execute("MATCH (a:TypedAssertion {assertion_id:$id}) SET a.name_embedding = $emb",
                {"id": aid, "emb": embedding})
    return aid


@pytest.mark.online
def test_observation_search_ranks_by_cosine_and_scopes_namespace(lane, test_neo4j_repo):
    _repo, queries = lane
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    ent = f"ent-{uuidlib.uuid4().hex[:8]}"
    a = _mk(test_neo4j_repo, ns=ns, entity=ent, span="I own 20 rare coins", embedding=[1.0, 0.0, 0.0])
    b = _mk(test_neo4j_repo, ns=ns, entity=ent, span="I own 20 comic books", embedding=[0.0, 1.0, 0.0])

    hits = queries.search_assertion_embeddings([1.0, 0.0, 0.0], namespaces=[ns])
    ids = [h["assertion_id"] for h in hits]
    assert ids[0] == a, hits                       # cosine=1 with the query vector ranks first
    assert set(ids) == {a, b}                       # both materializable observations are candidates
    assert hits[0]["stated_span"] == "I own 20 rare coins"   # the recall surface is the user's words
    assert all(h["cosine"] is not None for h in hits)

    # namespace scoping: a search in a DIFFERENT namespace sees none of these.
    assert queries.search_assertion_embeddings([1.0, 0.0, 0.0], namespaces=["ns-other"]) == []


@pytest.mark.online
def test_observation_search_excludes_superseded_and_unbound(lane, test_neo4j_repo):
    _repo, queries = lane
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    ent = f"ent-{uuidlib.uuid4().hex[:8]}"
    live = _mk(test_neo4j_repo, ns=ns, entity=ent, span="I own 20 rare coins", embedding=[1.0, 0.0, 0.0])
    gone = _mk(test_neo4j_repo, ns=ns, entity=ent, span="I owned 5 old coins", embedding=[0.9, 0.1, 0.0])
    pend = _mk(test_neo4j_repo, ns=ns, entity=ent, span="maybe 3 coins", embedding=[0.8, 0.2, 0.0])
    test_neo4j_repo.execute("MATCH (a:TypedAssertion {assertion_id:$id}) SET a.superseded = true", {"id": gone})
    test_neo4j_repo.execute("MATCH (a:TypedAssertion {assertion_id:$id}) SET a.binding_pending = true", {"id": pend})

    ids = [h["assertion_id"] for h in queries.search_assertion_embeddings([1.0, 0.0, 0.0], namespaces=[ns])]
    assert ids == [live]                            # only the current + bound observation surfaces


@pytest.mark.online
def test_fetch_assertion_candidate_metadata_hydrates_by_id(lane, test_neo4j_repo):
    _repo, queries = lane
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    ent = f"ent-{uuidlib.uuid4().hex[:8]}"
    a = _mk(test_neo4j_repo, ns=ns, entity=ent, span="I own 20 rare coins", embedding=[1.0, 0.0, 0.0])
    gone = _mk(test_neo4j_repo, ns=ns, entity=ent, span="I owned 5 old coins", embedding=[0.9, 0.1, 0.0])
    test_neo4j_repo.execute("MATCH (a:TypedAssertion {assertion_id:$id}) SET a.superseded = true", {"id": gone})

    meta = queries.fetch_assertion_candidate_metadata([a, gone, "nope"])
    assert len(meta) == 1 and meta[0]["assertion_id"] == a   # superseded + unknown ids dropped
    row = meta[0]
    assert row["stated_span"] == "I own 20 rare coins"
    assert row["attribute"] == "owned" and row["value_kind"] == "count" and row["subject_uuid"] == ent
    assert queries.fetch_assertion_candidate_metadata([]) == []


@pytest.mark.online
def test_fetch_current_scalar_view_for_slot_resolves_by_slot(lane, test_neo4j_repo):
    # Phase 4a.4: the deterministic authority lookup returns the CURRENT View for a slot, scoped by
    # namespace, and None for a slot with no current View.
    views = ViewRepository(test_neo4j_repo)
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    ent = f"ent-{uuidlib.uuid4().hex[:8]}"
    views.record_scalar_state(
        subject="user", subject_uuid=ent, attribute="owned", scope="", value_kind="count", unit="",
        value=37, valid_at="2026-07-02T00:00:00+00:00", namespace=ns)

    hit = views.fetch_current_scalar_view_for_slot(
        subject_uuid=ent, attribute="owned", scope="", value_kind="count", unit="", namespace=ns)
    assert hit is not None and str(hit["value"]) == "37" and hit["attribute"] == "owned"

    # a different slot / wrong namespace resolves to nothing (no cross-slot or cross-tenant leak).
    assert views.fetch_current_scalar_view_for_slot(
        subject_uuid=ent, attribute="weight", scope="", value_kind="measurement", unit="kg",
        namespace=ns) is None
    assert views.fetch_current_scalar_view_for_slot(
        subject_uuid=ent, attribute="owned", scope="", value_kind="count", unit="",
        namespace="ns-other") is None


@pytest.mark.online
def test_foundation_gate_agent_tier_view_is_not_a_foundation(lane, test_neo4j_repo):
    # Phase 4b (G10): a View folded from a perception (`agent`-tier) assertion must NOT constitute a
    # source foundation -- current_authority (the fold SSOT) reports its effective tier as `agent`, which
    # is EXCLUDED from FOUNDATION_TIERS, so recall keeps it advisory. Proven against the REAL fold.
    from datetime import datetime, timezone
    from menhir.domain.scalar_view_authority import FOUNDATION_TIERS
    from menhir.services.scalar_state_service import ScalarStateService

    repo, _queries = lane
    ns = f"ns-{uuidlib.uuid4().hex[:8]}"
    ent = f"ent-{uuidlib.uuid4().hex[:8]}"
    ep = f"ep-{uuidlib.uuid4().hex[:8]}"
    test_neo4j_repo.execute("MERGE (n:Entity {uuid:$u}) SET n.name=$u", {"u": ent})
    test_neo4j_repo.execute("MERGE (e:Episodic {uuid:$u})", {"u": ep})
    span = "I own 20 rare coins"
    repo.record_assertion(TypedAssertion(
        subject_uuid=ent, subject_display="user", attribute="owned", scope="", value_kind="count",
        unit="", operation="absolute", value=20, stated_span=span, span_start=0, span_end=len(span),
        episode_uuid=ep, valid_at="2026-07-01T00:00:00+00:00", learned_at="2026-07-01T00:00:00+00:00",
        evidence_tier="agent", perceiver_version="v1", namespace=ns))

    svc = ScalarStateService(repo, ViewRepository(test_neo4j_repo))
    authority = svc.current_authority(ent, namespace=ns, as_of=datetime.now(timezone.utc))
    tier = authority[("owned", "", "count", "")]
    assert tier == "agent"                        # perception forces agent
    assert tier not in FOUNDATION_TIERS           # so the View stays advisory (G10)
