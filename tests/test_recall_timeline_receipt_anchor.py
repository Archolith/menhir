"""T1/T2: `recall_timeline` anchors on receipt (queue-node) uuids against a real graph.

Each memory has Menhir's queue node (receipt uuid, `namespace`, no `valid_at`) and the Graphiti
episode it resolves to (`resolved_episode_uuid`). The source-memory search, add_memory and
recall_history hand out receipt uuids, while the timeline orders Graphiti episodes, so query mode
and `around=<receipt>` must resolve the receipt -- and only through a visible, non-FAILED queue
node in the caller's silo. The offline stub tests cannot see this: they use one uuid space.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService

pytestmark = pytest.mark.online

MODEL = "text-embedding-3-small"
QUERY_VEC = [0.1, 0.2, 0.3]


class _Embedder:
    model = MODEL


class _Graphiti:
    embedder_ref = _Embedder()

    async def embed_query(self, query: str) -> list[float]:
        return QUERY_VEC


@pytest.fixture
def graph(test_neo4j_repo):
    tag = f"test-t1-{uuidlib.uuid4()}"
    ns, other = f"{tag}-ns", f"{tag}-other"
    u = {k: f"{tag}-{k}" for k in (
        "g1", "q1", "g2", "q2", "g3", "q3", "q3_hidden", "q3_failed",
        "foreign", "pending", "stale_model",
    )}
    test_neo4j_repo.execute(
        """
        CREATE (:Episodic {uuid:$g1, group_id:$ns, valid_at:datetime('2026-01-01T10:00:00Z'),
                           created_at:datetime('2026-01-01T10:00:00Z'), content:'moved to Denver',
                           source:'message', test_tag:$tag})
        CREATE (:Episodic {uuid:$q1, namespace:$ns, resolved_episode_uuid:$g1,
                           processing_state:'READY', content:'moved to Denver', source:'message',
                           reference_time:datetime('2026-01-01T10:00:00Z'),
                           content_embedding:[0.1, 0.2, 0.25], content_embedding_model:$model,
                           test_tag:$tag})
        CREATE (:Episodic {uuid:$g2, group_id:$ns, valid_at:datetime('2026-01-02T10:00:00Z'),
                           created_at:datetime('2026-01-02T10:00:00Z'), content:'bought a bike',
                           source:'message', test_tag:$tag})
        CREATE (:Episodic {uuid:$q2, namespace:$ns, resolved_episode_uuid:$g2,
                           processing_state:'READY', content:'bought a bike', source:'message',
                           test_tag:$tag})
        // Same space-dimension vector, closer to the query, but from another embedder (T2).
        CREATE (:Episodic {uuid:$stale_model, namespace:$ns, resolved_episode_uuid:$g2,
                           processing_state:'READY', content:'bought a bike', source:'message',
                           content_embedding:[0.1, 0.2, 0.3], content_embedding_model:'old-model',
                           test_tag:$tag})
        CREATE (:Episodic {uuid:$g3, group_id:$ns, valid_at:datetime('2026-01-03T10:00:00Z'),
                           created_at:datetime('2026-01-03T10:00:00Z'), content:'new job',
                           source:'message', test_tag:$tag})
        CREATE (:Episodic {uuid:$q3, namespace:$ns, resolved_episode_uuid:$g3,
                           processing_state:'READY', content:'new job', source:'message',
                           test_tag:$tag})
        // Hidden and FAILED queue nodes pointing at a VISIBLE episode: the receipt itself decides.
        CREATE (:Episodic {uuid:$q3_hidden, namespace:$ns, resolved_episode_uuid:$g3,
                           processing_state:'READY', scope:'CANDIDATE', content:'new job',
                           test_tag:$tag})
        CREATE (:Episodic {uuid:$q3_failed, namespace:$ns, resolved_episode_uuid:$g3,
                           processing_state:'FAILED', content:'new job', test_tag:$tag})
        // Another silo's queue node pointing into this silo must not act as a key into it.
        CREATE (:Episodic {uuid:$foreign, namespace:$other, resolved_episode_uuid:$g1,
                           processing_state:'READY', content:'other silo', test_tag:$tag})
        // Enrichment not finished: no Graphiti episode yet.
        CREATE (:Episodic {uuid:$pending, namespace:$ns, processing_state:'PENDING',
                           content:'still enriching', test_tag:$tag})
        """,
        params={**u, "ns": ns, "other": other, "tag": tag, "model": MODEL},
    )
    try:
        yield MemoryGraphAdapter(neo4j=test_neo4j_repo), ns, other, u
    finally:
        test_neo4j_repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": tag})


def _svc(adapter: MemoryGraphAdapter) -> RecallService:
    return RecallService(graphiti_client=_Graphiti(), graph_adapter=adapter,
                         scoring_service=ScoringService())


def test_anchor_accepts_receipt_and_episode_uuid(graph) -> None:
    adapter, ns, _other, u = graph
    assert adapter.timeline_anchor(uuid=u["q1"], namespace=ns)["uuid"] == u["g1"]
    assert adapter.timeline_anchor(uuid=u["g1"], namespace=ns)["uuid"] == u["g1"]
    assert adapter.timeline_anchor(uuid=u["q3"], namespace=ns)["uuid"] == u["g3"]


@pytest.mark.parametrize("key", ["q3_hidden", "q3_failed", "pending"])
def test_anchor_refuses_hidden_failed_or_unresolved_receipts(graph, key) -> None:
    adapter, ns, _other, u = graph
    assert adapter.timeline_anchor(uuid=u[key], namespace=ns) is None


def test_anchor_never_crosses_silos(graph) -> None:
    adapter, ns, other, u = graph
    # A foreign receipt is not a key into this silo, even though its pointer targets it.
    assert adapter.timeline_anchor(uuid=u["foreign"], namespace=ns) is None
    # And this silo's receipt is invisible from the other silo.
    assert adapter.timeline_anchor(uuid=u["q1"], namespace=other) is None


def test_receipt_anchor_honours_the_subject_thread(graph, test_neo4j_repo) -> None:
    adapter, ns, _other, u = graph
    subject = f"{u['g1']}-entity"
    test_neo4j_repo.execute(
        """
        MATCH (g:Episodic {uuid:$g1})
        CREATE (s:Entity {uuid:$subject, name:'Denver', group_id:$ns, test_tag:g.test_tag})
        CREATE (g)-[:MENTIONS]->(s)
        """,
        params={"g1": u["g1"], "subject": subject, "ns": ns},
    )
    hit = adapter.timeline_anchor(uuid=u["q1"], namespace=ns, subject_uuid=subject)
    assert hit is not None and hit["uuid"] == u["g1"]
    # g3 does not mention the subject, so its receipt is not an anchor on that thread.
    assert adapter.timeline_anchor(uuid=u["q3"], namespace=ns, subject_uuid=subject) is None


@pytest.mark.asyncio
async def test_query_mode_anchors_on_resolved_episode_with_current_model(graph) -> None:
    adapter, ns, _other, u = graph
    result = await _svc(adapter).recall_timeline(namespace=ns, query="where do I live", limit=3)
    anchors = [e.uuid for e in result.entries if e.is_anchor]
    # The stale-model vector is the closer one; the #220 filter keeps it out of the seed (T2).
    assert anchors == [u["g1"]]
    assert [e.uuid for e in result.entries] == [u["g1"], u["g2"]]


@pytest.mark.asyncio
async def test_around_receipt_matches_around_episode(graph) -> None:
    adapter, ns, _other, u = graph
    by_receipt = await _svc(adapter).recall_timeline(namespace=ns, around=u["q3"], limit=3)
    by_episode = await _svc(adapter).recall_timeline(namespace=ns, around=u["g3"], limit=3)
    assert [e.uuid for e in by_receipt.entries] == [e.uuid for e in by_episode.entries]
    assert [e.uuid for e in by_receipt.entries if e.is_anchor] == [u["g3"]]


@pytest.mark.asyncio
async def test_around_hidden_receipt_is_unknown(graph) -> None:
    adapter, ns, _other, u = graph
    with pytest.raises(ValueError, match="unknown or hidden memory"):
        await _svc(adapter).recall_timeline(namespace=ns, around=u["q3_hidden"])
