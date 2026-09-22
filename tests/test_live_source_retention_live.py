from __future__ import annotations

import uuid as uuidlib
from typing import Any

import pytest

from menhir.infrastructure.consolidation_queries import ConsolidationRepository
from menhir.infrastructure.correlation_queries import CorrelationRepository
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter


pytestmark = pytest.mark.online


@pytest.fixture
def live_repo(test_neo4j_repo):
    return test_neo4j_repo


@pytest.fixture
def tagged_graph(live_repo):
    tag = f"test-live-retention-{uuidlib.uuid4()}"
    ids = {
        "tag": tag,
        "source_a": f"{tag}-source-a",
        "source_b": f"{tag}-source-b",
        "target": f"{tag}-target",
        "survivor": f"{tag}-survivor",
        "absorbed": f"{tag}-absorbed",
    }
    live_repo.execute(
        """
        CREATE (:Episodic {uuid:$source_a, test_tag:$tag, namespace:'default', user_flagged:false})
        CREATE (:Episodic {uuid:$source_b, test_tag:$tag, namespace:'default', user_flagged:true})
        CREATE (:Entity {uuid:$target, test_tag:$tag, namespace:'default',
                         scope:'PERSISTENT', freshness:'ACTIVE', content:'full'})
        CREATE (:Entity {uuid:$survivor, test_tag:$tag, namespace:'default',
                         scope:'PERSISTENT', freshness:'ACTIVE', name:'survivor'})
        CREATE (:Entity {uuid:$absorbed, test_tag:$tag, namespace:'default',
                         scope:'PERSISTENT', freshness:'ACTIVE', name:'absorbed'})
        """,
        params=ids,
    )
    yield ids
    live_repo.execute(
        "MATCH (n) WHERE n.test_tag = $tag DETACH DELETE n", params={"tag": tag}
    )


def _exists(live_repo, uuid: str) -> bool:
    rows = live_repo.execute(
        "MATCH (n {uuid:$uuid}) RETURN count(n) AS count", params={"uuid": uuid}
    )
    return bool(rows and int(rows[0]["count"]) > 0)


def test_flag_unflag_reflag_is_live_and_beneficial_rehydration_is_allowed(
    live_repo, tagged_graph
) -> None:
    adapter = MemoryGraphAdapter(neo4j=live_repo)
    lifecycle = ConsolidationRepository(live_repo)
    for source in (tagged_graph["source_a"], tagged_graph["source_b"]):
        assert (
            adapter.record_retention_sources(
                source_episode_uuid=source,
                entity_uuids=[tagged_graph["target"]],
                namespace="default",
            )
            == 1
        )

    assert lifecycle.compress_node(tagged_graph["target"], "summary") is False
    assert adapter.unflag_memory(tagged_graph["source_b"]) is True
    assert lifecycle.compress_node(tagged_graph["target"], "summary") is True

    assert adapter.flag_memory(tagged_graph["source_a"]) is True
    assert lifecycle.complete_rehydration(tagged_graph["target"], "full again") is True
    assert lifecycle.compress_node(tagged_graph["target"], "summary again") is False

    row = live_repo.execute(
        "MATCH (n:Entity {uuid:$uuid}) RETURN coalesce(n.user_flagged, false) AS direct",
        params={"uuid": tagged_graph["target"]},
    )[0]
    assert row["direct"] is False, "source intent must not be copied onto the entity"


def test_flag_added_after_ttl_discovery_blocks_final_delete(
    live_repo, tagged_graph
) -> None:
    adapter = MemoryGraphAdapter(neo4j=live_repo)
    lifecycle = ConsolidationRepository(live_repo)
    live_repo.execute(
        """
        MATCH (target:Entity {uuid:$target})
        SET target.scope='SESSION', target.ttl_expires=datetime() - duration({days:1})
        """,
        params=tagged_graph,
    )
    adapter.record_retention_sources(
        source_episode_uuid=tagged_graph["source_a"],
        entity_uuids=[tagged_graph["target"]],
        namespace="default",
    )

    candidates = lifecycle.fetch_ttl_expired_session_uuids()
    assert tagged_graph["target"] in {str(row["uuid"]) for row in candidates}
    assert adapter.flag_memory(tagged_graph["source_a"]) is True

    deleted = lifecycle.delete_entities_returning_uuids(
        [tagged_graph["target"]], require_scope="SESSION", protect_retention=True
    )
    assert deleted == []
    assert _exists(live_repo, tagged_graph["target"])


def test_source_flag_added_between_merge_preflight_and_mutation_abstains(
    live_repo, tagged_graph
) -> None:
    adapter = MemoryGraphAdapter(neo4j=live_repo)
    adapter.record_retention_sources(
        source_episode_uuid=tagged_graph["source_a"],
        entity_uuids=[tagged_graph["absorbed"]],
        namespace="default",
    )

    class FlagAfterSnapshot:
        def __init__(self, delegate: Any) -> None:
            self.delegate = delegate
            self.injected = False

        def execute(
            self, query: str, params: dict[str, Any] | None = None, **kwargs: Any
        ):
            rows = self.delegate.execute(query, params=params, **kwargs)
            if not self.injected and "survivor_retention_sources_before" in query:
                self.injected = True
                self.delegate.execute(
                    "MATCH (source:Episodic {uuid:$uuid}) SET source.user_flagged=true",
                    params={"uuid": tagged_graph["source_a"]},
                )
            return rows

    repo = CorrelationRepository(FlagAfterSnapshot(live_repo))
    result = repo.merge_entity(
        tagged_graph["survivor"], tagged_graph["absorbed"], similarity=0.99
    )

    assert result["merged"] == 0
    assert result["reason"] == "ELIGIBILITY_CHANGED_AT_MUTATION"
    assert _exists(live_repo, tagged_graph["absorbed"])


def test_merge_does_not_propagate_cross_tenant_retention_edge(
    live_repo, tagged_graph
) -> None:
    live_repo.execute(
        """
        MATCH (source:Episodic {uuid:$source}), (absorbed:Entity {uuid:$absorbed})
        SET source.namespace='other', source.user_flagged=false
        CREATE (source)-[:RETENTION_SOURCE]->(absorbed)
        """,
        params={
            "source": tagged_graph["source_b"],
            "absorbed": tagged_graph["absorbed"],
        },
    )

    result = CorrelationRepository(live_repo).merge_entity(
        tagged_graph["survivor"], tagged_graph["absorbed"], similarity=0.99
    )

    assert result["merged"] == 1
    rows = live_repo.execute(
        """
        MATCH (source:Episodic {uuid:$source})-[r:RETENTION_SOURCE]->
              (survivor:Entity {uuid:$survivor})
        RETURN count(r) AS count
        """,
        params={
            "source": tagged_graph["source_b"],
            "survivor": tagged_graph["survivor"],
        },
    )
    assert int(rows[0]["count"]) == 0
