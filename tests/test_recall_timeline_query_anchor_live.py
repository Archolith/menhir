"""CF-268 live: `recall_timeline` query mode against a real graph and the real adapter.

The subject constraint must run inside the vector search (before its LIMIT), and store-only
receipts (no resolved Graphiti episode, e.g. assistant turns) must be skipped, not raised on.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService
from menhir.services.timeline_service import _QUERY_NO_HIT_NOTE

pytestmark = pytest.mark.online

MODEL = "text-embedding-3-small"
QUERY_VEC = [0.1, 0.2, 0.3]
#: More non-subject neighbours than the candidate cap, so a subject hit ranks past it.
NEIGHBOURS = 25


class _Embedder:
    model = MODEL


class _Graphiti:
    embedder_ref = _Embedder()

    async def embed_query(self, query: str) -> list[float]:
        return QUERY_VEC


def _svc(adapter: MemoryGraphAdapter) -> RecallService:
    return RecallService(graphiti_client=_Graphiti(), graph_adapter=adapter,
                         scoring_service=ScoringService())


@pytest.fixture
def graph(test_neo4j_repo):
    tag = f"test-cf268-{uuidlib.uuid4()}"
    ns, empty_ns = f"{tag}-ns", f"{tag}-empty"
    subject = f"{tag}-entity"
    test_neo4j_repo.execute(
        """
        CREATE (s:Entity {uuid:$subject, name:'Rachel', group_id:$ns, test_tag:$tag})
        // Store-only top hit: embedded, READY, but no resolved Graphiti episode.
        CREATE (:Episodic {uuid:$store_only, namespace:$ns, processing_state:'READY',
                           content:'assistant turn', source:'message',
                           content_embedding:[0.1, 0.2, 0.3], content_embedding_model:$model,
                           test_tag:$tag})
        // Non-subject memories, each strictly farther from the query than the last.
        WITH s
        UNWIND range(1, $neighbours) AS i
        CREATE (g:Episodic {uuid:$tag + '-g' + toString(i), group_id:$ns,
                            valid_at:datetime('2026-01-01T10:00:00Z') + duration({days: i}),
                            created_at:datetime('2026-01-01T10:00:00Z') + duration({days: i}),
                            content:'neighbour', source:'message', test_tag:$tag})
        CREATE (:Episodic {uuid:$tag + '-q' + toString(i), namespace:$ns,
                           resolved_episode_uuid:$tag + '-g' + toString(i),
                           processing_state:'READY', content:'neighbour', source:'message',
                           content_embedding:[0.1, 0.2, 0.3 + 0.001 * i],
                           content_embedding_model:$model, test_tag:$tag})
        """,
        params={"tag": tag, "ns": ns, "subject": subject, "model": MODEL,
                "neighbours": NEIGHBOURS, "store_only": f"{tag}-store-only"},
    )
    test_neo4j_repo.execute(
        """
        MATCH (s:Entity {uuid:$subject})
        UNWIND [1, 2] AS i
        CREATE (g:Episodic {uuid:$tag + '-gs' + toString(i), group_id:$ns,
                            valid_at:datetime('2026-03-01T10:00:00Z') + duration({days: i}),
                            created_at:datetime('2026-03-01T10:00:00Z') + duration({days: i}),
                            content:'about Rachel', source:'message', test_tag:$tag})
        CREATE (g)-[:MENTIONS]->(s)
        CREATE (:Episodic {uuid:$tag + '-qs' + toString(i), namespace:$ns,
                           resolved_episode_uuid:$tag + '-gs' + toString(i),
                           processing_state:'READY', content:'about Rachel', source:'message',
                           content_embedding:[0.3, 0.2, 0.1], content_embedding_model:$model,
                           test_tag:$tag})
        // A namespace whose only embedded memory is store-only.
        CREATE (:Episodic {uuid:$tag + '-only', namespace:$empty_ns, processing_state:'READY',
                           content:'assistant turn', source:'message',
                           content_embedding:[0.1, 0.2, 0.3], content_embedding_model:$model,
                           test_tag:$tag})
        """,
        params={"tag": tag, "ns": ns, "empty_ns": empty_ns, "subject": subject, "model": MODEL},
    )
    try:
        yield MemoryGraphAdapter(neo4j=test_neo4j_repo), ns, empty_ns, tag
    finally:
        test_neo4j_repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": tag})


@pytest.fixture
def crowded(test_neo4j_repo):
    """Closer store-only receipts, and closer receipts whose subject episode cannot anchor."""
    tag = f"test-cf268b-{uuidlib.uuid4()}"
    ns, other = f"{tag}-ns", f"{tag}-other"
    subject = f"{tag}-entity"
    test_neo4j_repo.execute(
        """
        CREATE (s:Entity {uuid:$subject, name:'Rachel', group_id:$ns, test_tag:$tag})
        // 25 closer store-only receipts (no resolved episode), then one real memory.
        WITH s
        UNWIND range(1, $n) AS i
        CREATE (:Episodic {uuid:$tag + '-so' + toString(i), namespace:$ns,
                           processing_state:'READY', content:'assistant turn', source:'message',
                           content_embedding:[0.1, 0.2, 0.3 + 0.001 * i],
                           content_embedding_model:$model, test_tag:$tag})
        WITH DISTINCT s
        CREATE (g:Episodic {uuid:$tag + '-real', group_id:$ns,
                            valid_at:datetime('2026-02-01T10:00:00Z'),
                            created_at:datetime('2026-02-01T10:00:00Z'),
                            content:'real memory', source:'message', test_tag:$tag})
        CREATE (:Episodic {uuid:$tag + '-real-q', namespace:$ns, resolved_episode_uuid:$tag + '-real',
                           processing_state:'READY', content:'real memory', source:'message',
                           content_embedding:[0.3, 0.2, 0.1], content_embedding_model:$model,
                           test_tag:$tag})
        // The subject's visible memory, farther than everything below.
        CREATE (v:Episodic {uuid:$tag + '-vis', group_id:$ns,
                            valid_at:datetime('2026-03-01T10:00:00Z'),
                            created_at:datetime('2026-03-01T10:00:00Z'),
                            content:'visible', source:'message', test_tag:$tag})
        CREATE (v)-[:MENTIONS]->(s)
        CREATE (:Episodic {uuid:$tag + '-vis-q', namespace:$ns, resolved_episode_uuid:$tag + '-vis',
                           processing_state:'READY', content:'visible', source:'message',
                           content_embedding:[0.3, 0.2, 0.1], content_embedding_model:$model,
                           test_tag:$tag})
        // Adversarial: closer receipts whose episode mentions the subject but is FAILED /
        // in another silo.
        CREATE (f:Episodic {uuid:$tag + '-failed', group_id:$ns, processing_state:'FAILED',
                            valid_at:datetime('2026-03-02T10:00:00Z'),
                            created_at:datetime('2026-03-02T10:00:00Z'),
                            content:'failed', source:'message', test_tag:$tag})
        CREATE (f)-[:MENTIONS]->(s)
        CREATE (:Episodic {uuid:$tag + '-failed-q', namespace:$ns,
                           resolved_episode_uuid:$tag + '-failed', processing_state:'READY',
                           content:'failed', source:'message',
                           content_embedding:[0.1, 0.2, 0.3], content_embedding_model:$model,
                           test_tag:$tag})
        CREATE (x:Episodic {uuid:$tag + '-foreign', group_id:$other,
                            valid_at:datetime('2026-03-03T10:00:00Z'),
                            created_at:datetime('2026-03-03T10:00:00Z'),
                            content:'foreign', source:'message', test_tag:$tag})
        CREATE (x)-[:MENTIONS]->(s)
        CREATE (:Episodic {uuid:$tag + '-foreign-q', namespace:$ns,
                           resolved_episode_uuid:$tag + '-foreign', processing_state:'READY',
                           content:'foreign', source:'message',
                           content_embedding:[0.1, 0.2, 0.3], content_embedding_model:$model,
                           test_tag:$tag})
        // 25 closer receipts behind hidden (scope=CANDIDATE) subject-mentioning episodes.
        WITH DISTINCT s
        UNWIND range(1, $n) AS i
        CREATE (h:Episodic {uuid:$tag + '-hid' + toString(i), group_id:$ns, scope:'CANDIDATE',
                            valid_at:datetime('2026-03-04T10:00:00Z') + duration({days: i}),
                            created_at:datetime('2026-03-04T10:00:00Z') + duration({days: i}),
                            content:'hidden', source:'message', test_tag:$tag})
        CREATE (h)-[:MENTIONS]->(s)
        CREATE (:Episodic {uuid:$tag + '-hid-q' + toString(i), namespace:$ns,
                           resolved_episode_uuid:$tag + '-hid' + toString(i),
                           processing_state:'READY', content:'hidden', source:'message',
                           content_embedding:[0.1, 0.2, 0.3 + 0.001 * i],
                           content_embedding_model:$model, test_tag:$tag})
        """,
        params={"tag": tag, "ns": ns, "other": other, "subject": subject, "model": MODEL,
                "n": NEIGHBOURS},
    )
    try:
        yield MemoryGraphAdapter(neo4j=test_neo4j_repo), ns, tag
    finally:
        test_neo4j_repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": tag})


def test_resolved_only_search_excludes_store_only_receipts(crowded) -> None:
    adapter, ns, tag = crowded
    hits = adapter.search_episode_embeddings(
        QUERY_VEC, limit=50, namespace=ns, model=MODEL, resolved_only=True
    )
    assert hits and not any("-so" in h["uuid"] for h in hits)


@pytest.mark.asyncio
async def test_rank_26_memory_anchors_behind_25_closer_store_only_receipts(crowded) -> None:
    adapter, ns, tag = crowded
    # No subject: 25 store-only receipts are closer than every real memory.
    result = await _svc(adapter).recall_timeline(namespace=ns, query="anything", limit=3)
    anchors = [e.uuid for e in result.entries if e.is_anchor]
    assert len(anchors) == 1 and not anchors[0].startswith(f"{tag}-so")
    assert result.note is None


def test_subject_search_skips_hidden_failed_and_foreign_episodes(crowded) -> None:
    adapter, ns, tag = crowded
    hits = adapter.search_episode_embeddings(
        QUERY_VEC, limit=20, namespace=ns, model=MODEL, subject_uuid=f"{tag}-entity",
        resolved_only=True,
    )
    # 25 closer CANDIDATE-scope, 1 FAILED and 1 foreign-silo episode must not be returned
    # nor use up slots; only the visible subject memory remains.
    assert [h["uuid"] for h in hits] == [f"{tag}-vis-q"]


@pytest.mark.asyncio
async def test_subject_query_anchors_on_visible_episode_behind_hidden_ones(crowded) -> None:
    adapter, ns, tag = crowded
    result = await _svc(adapter).recall_timeline(
        namespace=ns, query="anything", subject=f"{tag}-entity", limit=3
    )
    assert [e.uuid for e in result.entries if e.is_anchor] == [f"{tag}-vis"]
    assert {e.uuid for e in result.entries} <= {f"{tag}-vis"}


@pytest.mark.asyncio
async def test_subject_hit_ranked_past_the_candidate_cap_still_anchors(graph) -> None:
    adapter, ns, _empty, tag = graph
    result = await _svc(adapter).recall_timeline(
        namespace=ns, query="anything", subject=f"{tag}-entity", limit=5
    )
    anchors = [e.uuid for e in result.entries if e.is_anchor]
    # Rachel's memories rank 27th and 28th globally; the subject constraint runs before LIMIT.
    assert len(anchors) == 1 and anchors[0] in {f"{tag}-gs1", f"{tag}-gs2"}
    assert result.note is None
    assert all(e.uuid.startswith(f"{tag}-gs") for e in result.entries)


def test_search_with_subject_returns_only_subject_receipts(graph) -> None:
    adapter, ns, _empty, tag = graph
    hits = adapter.search_episode_embeddings(
        QUERY_VEC, limit=20, namespace=ns, model=MODEL, subject_uuid=f"{tag}-entity"
    )
    assert {h["uuid"] for h in hits} == {f"{tag}-qs1", f"{tag}-qs2"}


@pytest.mark.asyncio
async def test_store_only_top_hit_without_subject_is_skipped(graph) -> None:
    adapter, ns, _empty, tag = graph
    top = adapter.search_episode_embeddings(QUERY_VEC, limit=1, namespace=ns, model=MODEL)
    assert top[0]["uuid"] == f"{tag}-store-only"
    result = await _svc(adapter).recall_timeline(namespace=ns, query="anything", limit=3)
    anchors = [e.uuid for e in result.entries if e.is_anchor]
    assert anchors == [f"{tag}-g1"]


@pytest.mark.asyncio
async def test_nothing_anchorable_returns_no_hit_note_not_an_error(graph) -> None:
    adapter, _ns, empty_ns, _tag = graph
    result = await _svc(adapter).recall_timeline(namespace=empty_ns, query="anything", limit=3)
    assert result.entries == ()
    assert result.note == _QUERY_NO_HIT_NOTE
