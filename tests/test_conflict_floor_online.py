"""Online: the similarity floor's vector read against real Neo4j (``--run-online``).

A wrong query here fails open, so the floor would go quiet with every unit test still passing.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.services.correlation_service_signals import pair_cosine


@pytest.fixture
def entities(test_neo4j_repo):
    tag = f"test-floor-{uuidlib.uuid4()}"
    ids = {"a": f"{tag}-a", "b": f"{tag}-b", "bare": f"{tag}-bare"}
    test_neo4j_repo.execute(
        """
        CREATE (:Entity {uuid: $a, name: 'floor a', name_embedding: [1.0, 0.0], test_tag: $tag})
        CREATE (:Entity {uuid: $b, name: 'floor b', name_embedding: [0.0, 1.0], test_tag: $tag})
        CREATE (:Entity {uuid: $bare, name: 'floor bare', test_tag: $tag})
        """,
        params={**ids, "tag": tag},
    )
    yield ids
    test_neo4j_repo.execute(
        "MATCH (n) WHERE n.test_tag = $tag DETACH DELETE n", params={"tag": tag}
    )


@pytest.mark.online
def test_vector_read_returns_the_pair_in_requested_order(test_neo4j_repo, entities) -> None:
    repo = CorrelationRepository(test_neo4j_repo)
    vec_b, vec_a = repo.fetch_pair_name_embeddings(entities["b"], entities["a"])
    assert (vec_b, vec_a) == ([0.0, 1.0], [1.0, 0.0])
    assert pair_cosine(vec_a, vec_b) == pytest.approx(0.0)


@pytest.mark.online
def test_vector_read_gives_no_signal_for_a_missing_embedding_or_node(test_neo4j_repo, entities) -> None:
    repo = CorrelationRepository(test_neo4j_repo)
    assert repo.fetch_pair_name_embeddings(entities["a"], entities["bare"]) == ([1.0, 0.0], None)
    assert repo.fetch_pair_name_embeddings(entities["a"], "no-such-node") == ([1.0, 0.0], None)
