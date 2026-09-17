"""Live regression for #88: a shared session_id must not bridge namespaces.

This deliberately uses the same production composition root and real LLM-backed enrichment
helpers as the consumer-session acceptance gate.  The historical failure was not a pure Cypher
or unit-test defect: the second namespace collapsed during Graphiti entity resolution only when
two logical silos shared a session id.  A mocked extractor would therefore prove the wrong thing.

Run explicitly with a configured LLM/embedder and the disposable Neo4j test instance::

    pytest --run-online -m needs_llm tests/test_cross_namespace_session_id_live.py -s

The ordinary GitHub online job intentionally excludes ``needs_llm`` tests, so a green standard CI
run does not settle this regression.  The MVP release audit/E2E campaign must record one real run.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

# Reuse the acceptance gate's guarded disposable-stack fixtures as a pytest plugin rather than
# importing fixture symbols into this module.  Importing ``stack`` directly makes any helper/test
# parameter named ``stack`` look like an F811 redefinition even though pytest is supplying it.
pytest_plugins = ("tests.test_consumer_session_e2e",)

from tests.test_consumer_session_e2e import Client

pytestmark = [pytest.mark.online, pytest.mark.needs_llm]


def _count(stack, cypher: str, **params: object) -> int:
    rows = stack.neo4j.execute(cypher, params=params or None)
    return int(rows[0]["n"]) if rows else 0


@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_shared_session_id_does_not_collapse_second_namespace(stack) -> None:
    """Two identical memories with one session id still enrich independently by namespace.

    This is the smallest release-grade form of #88's historical reproducer.  The first namespace
    is allowed to finish before the second is queued, matching the observed "later twin collapses"
    failure rather than relying on a concurrency race.  Success requires more than READY status:
    each namespace must have its own extracted entity graph and no MENTIONS edge may cross the
    Graphiti ``group_id`` boundary.
    """

    from menhir.core import prepare_memory_runtime

    await prepare_memory_runtime(stack)

    # The shared stack fixture is hard-pinned to the disposable :7688 graph.  Start from an empty
    # corpus so any candidate/dedupe hit necessarily came from this reproducer.
    stack.neo4j.execute("MATCH (n) DETACH DELETE n")

    suffix = uuid4().hex[:10]
    shared_session_id = f"shared-session-{suffix}"
    namespace_a = f"shared-a-{suffix}"
    namespace_b = f"shared-b-{suffix}"

    # Multiple concrete named entities + explicit relations make a genuinely empty extraction very
    # unlikely.  The content is intentionally byte-identical across the two namespaces: that is the
    # condition which historically caused the second namespace's dedupe/resolution to collapse.
    text = (
        f"Project Asterion{suffix} uses PostgreSQL 16. "
        f"Alice Example maintains Asterion{suffix} for Northwind Labs."
    )

    client_a = Client(stack, namespace_a, shared_session_id)
    client_b = Client(stack, namespace_b, shared_session_id)

    episode_a = await client_a.say_and_remember(text)
    states_a = await client_a.await_enrichment()
    assert states_a.get(episode_a) == "READY", (
        "first namespace did not enrich cleanly; cannot evaluate the later-twin regression: "
        f"{states_a}"
    )

    group_a_mentions = _count(
        stack,
        "MATCH (e:Episodic)-[:MENTIONS]->(n:Entity) "
        "WHERE e.group_id = $group AND n.group_id = $group RETURN count(*) AS n",
        group=namespace_a,
    )
    assert group_a_mentions > 0, (
        "first namespace reached READY but produced no same-group MENTIONS; fixture did not "
        "exercise entity resolution"
    )

    # Queue the identical later twin only after A is fully READY.  #88's standing evidence says
    # this second logical namespace is the one that collapsed when session_id was shared.
    episode_b = await client_b.say_and_remember(text)
    states_b = await client_b.await_enrichment()
    assert states_b.get(episode_b) == "READY", (
        "#88 reproduced: second namespace failed enrichment under a shared session_id: "
        f"{states_b}"
    )

    group_b_mentions = _count(
        stack,
        "MATCH (e:Episodic)-[:MENTIONS]->(n:Entity) "
        "WHERE e.group_id = $group AND n.group_id = $group RETURN count(*) AS n",
        group=namespace_b,
    )
    assert group_b_mentions > 0, (
        "#88 reproduced: second namespace reached a terminal state without its own extracted "
        "entity graph"
    )

    entities_a = _count(
        stack,
        "MATCH (n:Entity) WHERE n.group_id = $group RETURN count(n) AS n",
        group=namespace_a,
    )
    entities_b = _count(
        stack,
        "MATCH (n:Entity) WHERE n.group_id = $group RETURN count(n) AS n",
        group=namespace_b,
    )
    assert entities_a > 0 and entities_b > 0, (
        "both namespaces must own entity rows after identical-content enrichment; "
        f"counts={{'{namespace_a}': {entities_a}, '{namespace_b}': {entities_b}}}"
    )

    cross_mentions = _count(
        stack,
        "MATCH (e:Episodic)-[:MENTIONS]->(n:Entity) "
        "WHERE e.group_id IN $groups AND n.group_id IN $groups "
        "AND e.group_id <> n.group_id RETURN count(*) AS n",
        groups=[namespace_a, namespace_b],
    )
    assert cross_mentions == 0, (
        "namespace isolation violation: an episode MENTIONS an entity owned by the other group; "
        f"cross_mentions={cross_mentions}"
    )