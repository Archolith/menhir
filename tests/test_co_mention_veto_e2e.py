"""End-to-end coverage for the co-mention merge veto.

Why this file exists
--------------------
``check_co_mention_veto`` shipped on 2026-07-04 querying a relationship type that
does not exist (``MENTIONED_IN``; the real provenance edge is ``MENTIONS``). The
query matched nothing, so the veto returned False for every pair -- a safety guard
that silently failed open for every merge.

The existing unit tests did not catch it because they mock the repository
(``repo.check_co_mention_veto.return_value = True``): they assert the *service*
reacts to a veto, never that the *Cypher* actually finds a co-mention.

So this module tests the query itself, two ways:

1. ``test_co_mention_query_uses_declared_edge_type`` -- offline. Asserts every
   relationship type referenced by the veto query is declared in
   ``schema.EDGE_LABELS``. This alone would have caught the original bug with no
   database.
2. ``test_co_mention_veto_*`` -- online (``--run-online``). Builds a real throwaway
   subgraph in Neo4j and asserts the veto actually fires on a co-mentioned pair and
   stays quiet on a non-co-mentioned pair.
"""

from __future__ import annotations

import re
import uuid as uuidlib

import pytest

from menhir.config import MemorySettings
from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.neo4j import Neo4jRepository
from menhir.infrastructure.schema import EDGE_LABELS


def _rel_types_in(cypher: str) -> set[str]:
    """Extract relationship types referenced as -[:TYPE]- in a Cypher string."""
    return set(re.findall(r"-\[:([A-Z_]+)\]", cypher))


@pytest.mark.unit
def test_co_mention_query_uses_declared_edge_type() -> None:
    """The veto query must only traverse edge types that actually exist.

    Regression guard for the MENTIONED_IN bug: a query against an undeclared edge
    type matches nothing, which makes this safety veto fail open.
    """
    import inspect

    source = inspect.getsource(CorrelationRepository.check_co_mention_veto)
    rel_types = _rel_types_in(source)

    assert rel_types, "expected the veto query to traverse at least one edge type"
    assert "MENTIONED_IN" not in rel_types, (
        "MENTIONED_IN is not a real edge type -- the veto would match nothing and fail open"
    )
    undeclared = rel_types - set(EDGE_LABELS)
    assert not undeclared, (
        f"veto query traverses edge types not declared in schema.EDGE_LABELS: {undeclared}. "
        f"An undeclared edge type matches nothing and silently disables this veto."
    )
    assert "MENTIONS" in rel_types


@pytest.fixture
def live_repo(test_neo4j_repo):
    """Alias onto the STOOD-UP TEST instance (conftest.test_neo4j_repo).

    Never the operator's real graph: these tests run destructive, unscoped queries.
    """
    return test_neo4j_repo


@pytest.fixture
def co_mention_fixture(live_repo):
    """Create a throwaway subgraph and tear it down afterwards.

    Layout:
        ep_shared  -[:MENTIONS]-> ent_a      (co-mentioned pair: a + b)
        ep_shared  -[:MENTIONS]-> ent_b
        ep_other   -[:MENTIONS]-> ent_c      (c is mentioned only by its own episode)
    """
    tag = f"test-co-mention-{uuidlib.uuid4()}"
    ids = {
        "a": f"{tag}-a",
        "b": f"{tag}-b",
        "c": f"{tag}-c",
        "ep_shared": f"{tag}-ep-shared",
        "ep_other": f"{tag}-ep-other",
    }
    live_repo.execute(
        """
        CREATE (a:Entity {uuid: $a, name: 'veto test a', test_tag: $tag})
        CREATE (b:Entity {uuid: $b, name: 'veto test b', test_tag: $tag})
        CREATE (c:Entity {uuid: $c, name: 'veto test c', test_tag: $tag})
        CREATE (ep1:Episodic {uuid: $ep_shared, name: 'shared episode', test_tag: $tag})
        CREATE (ep2:Episodic {uuid: $ep_other, name: 'other episode', test_tag: $tag})
        CREATE (ep1)-[:MENTIONS]->(a)
        CREATE (ep1)-[:MENTIONS]->(b)
        CREATE (ep2)-[:MENTIONS]->(c)
        """,
        params={**ids, "tag": tag},
    )
    yield ids
    live_repo.execute(
        "MATCH (n) WHERE n.test_tag = $tag DETACH DELETE n", params={"tag": tag}
    )


@pytest.mark.online
def test_co_mention_veto_fires_for_co_mentioned_pair(live_repo, co_mention_fixture) -> None:
    """Two entities mentioned by the SAME episode are distinct -- the veto must fire."""
    queries = CorrelationRepository(live_repo)
    assert queries.check_co_mention_veto(co_mention_fixture["a"], co_mention_fixture["b"]) is True


@pytest.mark.online
def test_co_mention_veto_quiet_for_non_co_mentioned_pair(live_repo, co_mention_fixture) -> None:
    """Entities with no shared episode must NOT be vetoed (merge may proceed to the judge)."""
    queries = CorrelationRepository(live_repo)
    assert queries.check_co_mention_veto(co_mention_fixture["a"], co_mention_fixture["c"]) is False


@pytest.mark.online
def test_co_mention_veto_is_symmetric(live_repo, co_mention_fixture) -> None:
    """Argument order must not change the verdict."""
    queries = CorrelationRepository(live_repo)
    a, b = co_mention_fixture["a"], co_mention_fixture["b"]
    assert queries.check_co_mention_veto(a, b) == queries.check_co_mention_veto(b, a) is True
