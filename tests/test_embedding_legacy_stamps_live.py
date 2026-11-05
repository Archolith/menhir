"""CF-260 live: the source-memory model filter admits the current model and ingest's 'unknown'."""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

pytestmark = pytest.mark.online


@pytest.fixture
def graph(test_neo4j_repo):
    tag = f"test-cf260-{uuidlib.uuid4()}"
    ns = f"{tag}-ns"
    stamps = {"current": "m1", "blank": "", "unknown": "unknown", "other": "m2"}
    for key, stamp in stamps.items():
        test_neo4j_repo.execute(
            """
            CREATE (:Episodic {uuid:$uuid, namespace:$ns, processing_state:'READY',
                               content:'moved to Denver', source:'message',
                               content_embedding:[0.1, 0.2, 0.3], content_embedding_model:$stamp,
                               test_tag:$tag})
            """,
            params={"uuid": f"{tag}-{key}", "ns": ns, "stamp": stamp, "tag": tag},
        )
    test_neo4j_repo.execute(
        """
        CREATE (:Episodic {uuid:$uuid, namespace:$ns, processing_state:'READY',
                           content:'moved to Denver', source:'message',
                           content_embedding:[0.1, 0.2, 0.3], test_tag:$tag})
        """,
        params={"uuid": f"{tag}-null", "ns": ns, "tag": tag},
    )
    try:
        yield MemoryGraphAdapter(neo4j=test_neo4j_repo), ns, tag
    finally:
        test_neo4j_repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": tag})


def test_filter_admits_current_and_unknown_stamps_only(graph) -> None:
    adapter, ns, tag = graph

    hits = adapter.search_episode_embeddings([0.1, 0.2, 0.3], limit=10, namespace=ns, model="m1")

    # 'unknown' is ingest's pre-fix stamp; blank/NULL stay excluded (#171 A5).
    found = {str(h.get("uuid")) for h in hits}
    assert found == {f"{tag}-current", f"{tag}-unknown"}


def test_no_model_compares_every_vector(graph) -> None:
    adapter, ns, tag = graph

    hits = adapter.search_episode_embeddings([0.1, 0.2, 0.3], limit=10, namespace=ns)

    assert f"{tag}-other" in {str(h.get("uuid")) for h in hits}
