"""A paraphrased duplicate mention must not drop a stored ``r.source`` on save.

graphiti_core's duplicate path clears ``attributes`` when ``edge_type_candidates`` is not None
(``Graphiti.add_episode`` passes ``edge_types or {}``) and the Neo4j save is a full replace, so a
stored ``source`` vanishes unless it is named in ``preserved_attributes``. Menhir always adds
``GUARD_PRESERVED_EDGE_ATTRIBUTES`` via ``guarded_preserved_edge_attributes_kwargs``.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
import uuid as uuidlib

import pytest

from menhir.infrastructure.anchored_time_persist import PRESERVED_EDGE_ATTRIBUTES
from menhir.infrastructure.graphiti_edge_invalidation import (
    GUARD_PRESERVED_EDGE_ATTRIBUTES,
    guarded_preserved_edge_attributes_kwargs,
)

NOW = datetime(2024, 4, 10, 18, 31, tzinfo=timezone.utc)


# ---- unit (no Neo4j) -------------------------------------------------------------------------


def test_guarded_kwargs_empty_base_yields_source_only():
    assert guarded_preserved_edge_attributes_kwargs({}) == {"preserved_edge_attributes": ("source",)}
    assert GUARD_PRESERVED_EDGE_ATTRIBUTES == ("source",)


def test_guarded_kwargs_does_not_duplicate_source_and_keeps_base_order():
    out = guarded_preserved_edge_attributes_kwargs({"preserved_edge_attributes": ("b", "source", "a")})
    assert out["preserved_edge_attributes"] == ("b", "source", "a")


def test_guarded_kwargs_appends_source_after_base_in_order():
    out = guarded_preserved_edge_attributes_kwargs({"preserved_edge_attributes": ("z", "a")})
    assert out["preserved_edge_attributes"] == ("z", "a", "source")
    out = guarded_preserved_edge_attributes_kwargs({"preserved_edge_attributes": PRESERVED_EDGE_ATTRIBUTES})
    assert out["preserved_edge_attributes"] == tuple(PRESERVED_EDGE_ATTRIBUTES) + ("source",)


# ---- online ----------------------------------------------------------------------------------


@pytest.fixture
async def driver(test_neo4j_repo):
    from graphiti_core.driver.neo4j_driver import Neo4jDriver

    drv = Neo4jDriver(os.environ["NEO4J_URI"], os.environ["NEO4J_USER"],
                      os.environ["NEO4J_PASSWORD"], os.environ.get("NEO4J_DATABASE", "neo4j"))
    if drv._init_task is not None:
        await drv._init_task
    yield drv
    await drv.close()


class _DuplicateLLM:
    """Marks related edge 0 as a duplicate and contradicts nothing."""

    async def generate_response(self, *args, **kwargs):
        return {"duplicate_facts": [0], "contradicted_facts": []}


def _stored_source(repo, uid: str):
    rows = repo.execute(
        "MATCH ()-[r:RELATES_TO {uuid: $u}]->() RETURN r.source AS source", params={"u": uid})
    assert len(rows) == 1
    return rows[0]["source"]


async def _resolve_paraphrase_and_save(driver, repo, preserved: frozenset[str]) -> str:
    from graphiti_core.edges import EntityEdge
    from graphiti_core.nodes import EpisodeType, EpisodicNode
    from graphiti_core.utils.maintenance.edge_operations import resolve_extracted_edge

    g = f"g-{uuidlib.uuid4().hex[:8]}"
    user, rome = str(uuidlib.uuid4()), str(uuidlib.uuid4())
    repo.execute("UNWIND $ids AS id CREATE (:Entity {uuid: id, group_id: $g})",
                 params={"ids": [user, rome], "g": g})

    def new_edge(fact: str, episode: str) -> EntityEdge:
        return EntityEdge(uuid=str(uuidlib.uuid4()), group_id=g, source_node_uuid=user,
                          target_node_uuid=rome, created_at=NOW, name="RELATES_TO", fact=fact,
                          episodes=[episode], valid_at=NOW, fact_embedding=[0.1, 0.2, 0.3, 0.4])

    stored_edge = new_edge("The user was in Rome.", str(uuidlib.uuid4()))
    await stored_edge.save(driver)
    repo.execute("MATCH ()-[r:RELATES_TO {uuid: $u}]->() SET r.source = 'user'",
                 params={"u": stored_edge.uuid})
    assert _stored_source(repo, stored_edge.uuid) == "user"

    stored = await EntityEdge.get_by_uuid(driver, stored_edge.uuid)
    await stored.load_fact_embedding(driver)
    assert stored.attributes.get("source") == "user"

    ep2 = str(uuidlib.uuid4())
    episode = EpisodicNode(uuid=ep2, name="ep", group_id=g, source=EpisodeType.message,
                           source_description="chat", content="user: I visited Rome",
                           valid_at=NOW, created_at=NOW)
    paraphrase = new_edge("The user visited Rome.", ep2)
    resolved, _, _ = await resolve_extracted_edge(
        _DuplicateLLM(), paraphrase, [stored], [], episode,
        edge_type_candidates={}, preserved_attributes=preserved)
    assert resolved.uuid == stored_edge.uuid  # the duplicate path was actually taken
    await resolved.save(driver)
    return stored_edge.uuid


@pytest.mark.online
async def test_guarded_preserved_attributes_keep_stored_source_on_duplicate(driver, test_neo4j_repo):
    preserved = frozenset(guarded_preserved_edge_attributes_kwargs({})["preserved_edge_attributes"])
    uid = await _resolve_paraphrase_and_save(driver, test_neo4j_repo, preserved)
    assert _stored_source(test_neo4j_repo, uid) == "user"


@pytest.mark.online
async def test_control_without_preservation_loses_stored_source(driver, test_neo4j_repo):
    uid = await _resolve_paraphrase_and_save(driver, test_neo4j_repo, frozenset())
    assert _stored_source(test_neo4j_repo, uid) is None


@pytest.mark.online
async def test_guarded_preserved_attributes_with_anchored_time_flags_keep_source(
        driver, test_neo4j_repo):
    kwargs = guarded_preserved_edge_attributes_kwargs(
        {"preserved_edge_attributes": PRESERVED_EDGE_ATTRIBUTES})
    preserved = frozenset(kwargs["preserved_edge_attributes"])
    assert "source" in preserved and set(PRESERVED_EDGE_ATTRIBUTES) <= preserved
    uid = await _resolve_paraphrase_and_save(driver, test_neo4j_repo, preserved)
    assert _stored_source(test_neo4j_repo, uid) == "user"
