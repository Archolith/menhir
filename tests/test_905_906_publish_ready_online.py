"""#905/#906 online: the Cypher behind the link outcome, the completion predicate and the clear.

Online because the decisions are Cypher: the link write stores the outcome, ``linked_twin_artifact``
computes ``completed`` and reads the stored text, the retry tier re-admits only a completed link,
and ``clear_uncompleted_twin_link`` deletes an empty twin. The offline counterparts are in
tests/test_905_906_878_publish_ready.py.
"""

from __future__ import annotations

import asyncio
import uuid as uuidlib
from datetime import datetime, timezone
from typing import Any

import pytest

from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository
from menhir.infrastructure.publish_fence import ClaimFence, GateFence, MenhirPublishFence

pytestmark = pytest.mark.online

WORKER = "w905"


@pytest.fixture
def g(test_neo4j_repo):
    tag = f"i905-{uuidlib.uuid4().hex[:10]}"
    repo = EpisodeLifecycleRepository()
    repo.neo4j = test_neo4j_repo
    try:
        yield repo, test_neo4j_repo, tag
    finally:
        test_neo4j_repo.execute(
            "MATCH (n) WHERE n.uuid STARTS WITH $tag OR n.namespace_key = $tag DETACH DELETE n",
            params={"tag": tag},
        )


def _seed(neo4j: Any, tag: str, *, state: str = "FAILED", outcome: str | None, mentions: bool,
          parked: bool = False) -> tuple[str, str]:
    q, twin = f"{tag}-q", f"{tag}-twin"
    neo4j.execute(
        """
        CREATE (q:Episodic {uuid: $q, namespace: $tag, name: $q, content: 'x',
                            processing_state: $state, processing_attempts: 1,
                            processing_error: 'relationless_extraction retryable=false',
                            processing_completed_at: datetime('2000-01-01T00:00:00Z'),
                            created_at: datetime('2000-01-01T00:00:00Z'),
                            resolved_episode_uuid: $twin, resolved_extraction_outcome: $outcome})
        CREATE (g:Episodic {uuid: $twin, group_id: $tag, name: 'twin', menhir_memory_uuid: $q})
        FOREACH (_ IN CASE WHEN $parked THEN [1] ELSE [] END |
            SET q.retry_parked_at = datetime(),
                q.retry_parked_for = toString(q.processing_completed_at),
                q.retry_parked_reason = 'terminal')
        FOREACH (_ IN CASE WHEN $mentions THEN [1] ELSE [] END |
            CREATE (g)-[:MENTIONS]->(a:Entity {uuid: $tag + '-alice', name: 'Alice', group_id: $tag})
            CREATE (g)-[:MENTIONS]->(b:Entity {uuid: $tag + '-acme', name: 'Acme', group_id: $tag})
            CREATE (a)-[:RELATES_TO {uuid: $tag + '-edge', name: 'WORKS_AT',
                                     fact: '[synthetic] alice acme', episodes: [$twin]}]->(b))
        """,
        params={"q": q, "twin": twin, "tag": tag, "state": state, "outcome": outcome,
                "mentions": mentions, "parked": parked},
    )
    return q, twin


def test_collapsed_empty_link_is_not_completed(g) -> None:
    repo, neo4j, tag = g
    q, _twin = _seed(neo4j, tag, outcome="collapsed", mentions=False)
    artifact = repo.linked_twin_artifact(q)
    assert artifact is not None
    assert artifact["completed"] is False
    assert artifact["extraction_outcome"] == "collapsed"


def test_legacy_link_with_mentions_stays_a_completion_and_carries_its_text(g) -> None:
    repo, neo4j, tag = g
    q, _twin = _seed(neo4j, tag, outcome=None, mentions=True)
    artifact = repo.linked_twin_artifact(q)
    assert artifact["completed"] is True
    assert {d["name"] for d in artifact["entity_details"].values()} == {"Alice", "Acme"}
    edge = artifact["edge_details"][f"{tag}-edge"]
    assert edge["fact"] == "[synthetic] alice acme"
    assert edge["source_node_uuid"] == f"{tag}-alice"
    assert edge["target_node_uuid"] == f"{tag}-acme"


def test_empty_outcome_is_a_completion_without_mentions(g) -> None:
    repo, neo4j, tag = g
    q, _twin = _seed(neo4j, tag, outcome="empty", mentions=False)
    assert repo.linked_twin_artifact(q)["completed"] is True


def _candidates(repo: EpisodeLifecycleRepository, q: str) -> list[dict]:
    return [r for r in repo.fetch_failed_episode_retry_candidates(limit=500) if r["uuid"] == q]


def test_parked_row_with_a_collapsed_link_is_not_readmitted(g) -> None:
    repo, neo4j, tag = g
    q, _twin = _seed(neo4j, tag, outcome="collapsed", mentions=False, parked=True)
    assert _candidates(repo, q) == []


def test_parked_row_with_a_completed_link_is_readmitted(g) -> None:
    repo, neo4j, tag = g
    q, _twin = _seed(neo4j, tag, outcome=None, mentions=True, parked=True)
    assert len(_candidates(repo, q)) == 1


def test_clear_drops_the_collapsed_link_and_deletes_the_empty_twin(g) -> None:
    repo, neo4j, tag = g
    q, twin = _seed(neo4j, tag, state="ENRICHING", outcome="collapsed", mentions=False)
    assert repo.clear_uncompleted_twin_link(q, twin) is True
    row = neo4j.execute(
        "MATCH (q:Episodic {uuid: $q}) "
        "RETURN q.resolved_episode_uuid AS link, q.resolved_extraction_outcome AS outcome, "
        "EXISTS { MATCH (:Episodic {uuid: $twin}) } AS twin_exists",
        params={"q": q, "twin": twin},
    )[0]
    assert (row["link"], row["outcome"], row["twin_exists"]) == (None, None, False)


def _twin_state(neo4j: Any, q: str, twin: str) -> dict:
    return dict(neo4j.execute(
        "MATCH (q:Episodic {uuid: $q}) "
        "RETURN q.resolved_episode_uuid AS link, "
        "EXISTS { MATCH (:Episodic {uuid: $twin}) } AS twin_exists",
        params={"q": q, "twin": twin},
    )[0])


def test_clear_deletes_an_empty_twin_with_its_read_context_edges(g) -> None:
    repo, neo4j, tag = g
    q, twin = _seed(neo4j, tag, state="ENRICHING", outcome="collapsed", mentions=False)
    neo4j.execute(
        """
        MATCH (g:Episodic {uuid: $twin})
        CREATE (p:Episodic {uuid: $tag + '-prev', group_id: $tag})
        CREATE (t:TurnEvidence {uuid: $tag + '-turn', turn_id: $tag + '-turn', namespace: $tag})
        CREATE (g)-[:READ_CONTEXT {kind: 'previous_episode'}]->(p)
        CREATE (g)-[:READ_CONTEXT {kind: 'repair_turn'}]->(t)
        """,
        params={"twin": twin, "tag": tag},
    )

    assert repo.clear_uncompleted_twin_link(q, twin) is True
    assert _twin_state(neo4j, q, twin) == {"link": None, "twin_exists": False}
    left = neo4j.execute(
        "MATCH (n) WHERE n.uuid IN [$tag + '-prev', $tag + '-turn'] "
        "RETURN count(n) AS kept, count { (n)<-[:READ_CONTEXT]-() } AS edges",
        params={"tag": tag},
    )[0]
    assert (left["kept"], left["edges"]) == (2, 0)


def test_clear_unlinks_but_keeps_a_twin_with_any_other_relationship(g) -> None:
    repo, neo4j, tag = g
    q, twin = _seed(neo4j, tag, state="ENRICHING", outcome="collapsed", mentions=False)
    neo4j.execute(
        """
        MATCH (g:Episodic {uuid: $twin})
        CREATE (o:Episodic {uuid: $tag + '-other', group_id: $tag})
        CREATE (o)-[:READ_CONTEXT {kind: 'previous_episode'}]->(g)
        """,
        params={"twin": twin, "tag": tag},
    )

    assert repo.clear_uncompleted_twin_link(q, twin) is True
    assert _twin_state(neo4j, q, twin) == {"link": None, "twin_exists": True}


def test_clear_refuses_a_completed_link(g) -> None:
    repo, neo4j, tag = g
    q, twin = _seed(neo4j, tag, state="ENRICHING", outcome=None, mentions=True)
    assert repo.clear_uncompleted_twin_link(q, twin) is False
    assert repo.linked_twin_artifact(q)["resolved_episode_uuid"] == twin


def test_link_write_stores_the_outcome(g) -> None:
    _repo, neo4j, tag = g
    q, twin = f"{tag}-q", f"{tag}-twin"
    neo4j.execute(
        "CREATE (:Episodic {uuid: $q, namespace: $tag, processing_state: 'ENRICHING'}) "
        "CREATE (:Episodic {uuid: $twin, group_id: $tag})",
        params={"q": q, "twin": twin, "tag": tag},
    )
    claim = ClaimFence(q_uuid=q, worker_id=WORKER, claim_started_at=datetime.now(timezone.utc),
                       namespace_key=tag, reserved_twin_uuid=twin)
    fence = MenhirPublishFence(
        claim, GateFence(epoch=0),
        extraction_outcome_provider=lambda nodes, edges: {
            "outcome": "policy_empty", "policy_empty_reason": "claimless_question", "self_bound": True,
        },
    )

    def work(tx: Any) -> None:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(fence.after_write(
                tx, episode_uuid=twin, entity_node_uuids=[], entity_edge_uuids=[],
                previous_episode_uuids=[],
            ))
        finally:
            loop.close()

    neo4j.execute_write(work)
    row = neo4j.execute(
        "MATCH (q:Episodic {uuid: $q}) RETURN q.resolved_episode_uuid AS link, "
        "q.resolved_extraction_outcome AS outcome, q.resolved_policy_empty_reason AS reason, "
        "q.resolved_self_bound AS self_bound",
        params={"q": q},
    )[0]
    assert dict(row) == {"link": twin, "outcome": "policy_empty", "reason": "claimless_question",
                         "self_bound": True}
