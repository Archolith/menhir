"""Online: the invalidation guard through the fork's real temporal-invalidation pass.

Real ``_apply_temporal_invalidation`` + real ``MenhirEdgeInvalidationHook`` + real Neo4j
(throwaway test instance, fixture ``test_neo4j_repo``). Run with::

    MENHIR_TEST_NEO4J_URI=bolt://127.0.0.1:<port> pytest --run-online \
        tests/test_edge_invalidation_guard_online.py
"""

from __future__ import annotations

import asyncio
import os
import uuid as uuidlib
from datetime import datetime, timezone

import pytest

pytest.importorskip("graphiti_core.edge_invalidation")

from graphiti_core.driver.neo4j_driver import Neo4jDriver  # noqa: E402
from graphiti_core.edges import EntityEdge  # noqa: E402
from graphiti_core.utils.maintenance.edge_operations import (  # noqa: E402
    _apply_temporal_invalidation,
)

from menhir.infrastructure.graphiti_edge_invalidation import (  # noqa: E402
    InvalidationGuardState,
    KeptEdge,
    MenhirEdgeInvalidationHook,
    invalidation_guard_state,
    record_kept_conflicts,
)

OLD = datetime(2026, 1, 1, tzinfo=timezone.utc)
NEW = datetime(2026, 2, 1, tzinfo=timezone.utc)


def _edge(uid: str, fact: str, valid_at: datetime, ns: str) -> EntityEdge:
    return EntityEdge(
        uuid=uid, group_id=ns, source_node_uuid=f"{uid}-s", target_node_uuid=f"{uid}-t",
        created_at=valid_at, name="LIVES_IN", fact=fact, episodes=[], valid_at=valid_at,
    )


def _seed_stored(repo, uid: str, ns: str, source: str | None) -> None:
    repo.execute(
        """
        CREATE (a:Entity {uuid: $s, group_id: $ns, name: 'a'})
        CREATE (b:Entity {uuid: $t, group_id: $ns, name: 'b'})
        CREATE (a)-[r:RELATES_TO {uuid: $uid, group_id: $ns, fact: 'lives in Paris'}]->(b)
        FOREACH (_ IN CASE WHEN $source IS NULL THEN [] ELSE [1] END | SET r.source = $source)
        """,
        {"s": f"{uid}-s", "t": f"{uid}-t", "uid": uid, "ns": ns, "source": source},
    )


def _driver() -> Neo4jDriver:
    return Neo4jDriver(
        uri=os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://127.0.0.1:7688"),
        user=os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j"),
        password=os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword"),
        database=os.getenv("MENHIR_TEST_NEO4J_DATABASE", "neo4j"),
    )


async def _contradict(driver, stored_source: str | None, call_source: str, repo, ns: str):
    stored_uid = f"stored-{uuidlib.uuid4().hex[:8]}"
    _seed_stored(repo, stored_uid, ns, stored_source)
    stored = _edge(stored_uid, "lives in Paris", OLD, ns)
    new = _edge(f"new-{uuidlib.uuid4().hex[:8]}", "lives in Rome", NEW, ns)
    state = InvalidationGuardState(owner="o", source=call_source)
    hook = MenhirEdgeInvalidationHook(driver)

    async def call():
        # Like GraphitiClient.add_episode: bind before the child task is created.
        invalidation_guard_state.set(state)
        return await asyncio.create_task(
            _apply_temporal_invalidation(new, [stored], new, None, edge_invalidation_hook=hook)
        )

    invalidated = await asyncio.create_task(call())
    return stored, state, invalidated


@pytest.mark.online
@pytest.mark.parametrize(
    ("stored_source", "call_source", "survives"),
    [
        ("user", "claude-code", True),    # agent cannot supersede a user edge
        ("manual", "agent_inference", True),
        ("user", "user", False),          # user over user: unchanged
        ("claude-code", "user", False),   # user correction of an agent edge
        ("claude-code", "claude-code", False),
        (None, "claude-code", False),     # unstamped edge is not protected
    ],
)
def test_guard_through_real_temporal_invalidation(
    test_neo4j_repo, stored_source, call_source, survives
):
    ns = f"inv-{uuidlib.uuid4().hex[:8]}"

    async def run():
        driver = _driver()
        try:
            return await _contradict(driver, stored_source, call_source, test_neo4j_repo, ns)
        finally:
            await driver.close()

    stored, state, invalidated = asyncio.run(run())

    if survives:
        assert invalidated == []
        assert stored.invalid_at is None and stored.expired_at is None
        assert len(state.kept) == 1 and state.kept[0].kept_edge_uuid == stored.uuid
    else:
        assert [e.uuid for e in invalidated] == [stored.uuid]
        assert stored.invalid_at is not None and stored.expired_at is not None
        assert state.kept == []


# ---- record_kept_conflicts against real Neo4j ----


def _conflict_node(repo, uid: str) -> list[dict]:
    return repo.execute(
        "MATCH (c:EdgeInvalidationConflict {kept_edge_uuid: $uid}) "
        "RETURN c.uuid AS uuid, c.status AS status, c.seen_count AS seen_count, "
        "c.group_id AS group_id, c.cause_source AS cause_source",
        {"uid": uid},
    )


@pytest.mark.online
def test_record_kept_conflicts_is_idempotent_keeps_resolution_and_spares_entities(
    test_neo4j_repo,
):
    repo = test_neo4j_repo
    ns = f"inv-{uuidlib.uuid4().hex[:8]}"
    kept_uid = f"kept-{uuidlib.uuid4().hex[:8]}"
    ent_a, ent_b = f"ent-{uuidlib.uuid4().hex[:8]}", f"ent-{uuidlib.uuid4().hex[:8]}"
    for ent in (ent_a, ent_b):
        repo.execute("CREATE (:Entity {uuid: $u, group_id: $ns, name: 'x'})", {"u": ent, "ns": ns})
    entity_props_before = {
        r["uuid"]: r["props"]
        for r in repo.execute(
            "MATCH (e:Entity) WHERE e.uuid IN $us RETURN e.uuid AS uuid, properties(e) AS props",
            {"us": [ent_a, ent_b]},
        )
    }
    kept = KeptEdge(
        reason="contradiction", kept_edge_uuid=kept_uid, kept_group_id=ns,
        kept_fact="lives in Paris", cause_edge_uuid=f"cause-{kept_uid}",
        cause_fact="lives in Rome", cause_source="claude-code",
    )

    async def record():
        driver = _driver()
        try:
            return await record_kept_conflicts(driver, [kept])
        finally:
            await driver.close()

    assert asyncio.run(record()) == 1
    (node,) = _conflict_node(repo, kept_uid)
    assert node["status"] == "unresolved" and node["seen_count"] == 1
    assert node["group_id"] == ns and node["cause_source"] == "claude-code"

    assert asyncio.run(record()) == 1
    (node,) = _conflict_node(repo, kept_uid)  # still exactly one node
    assert node["seen_count"] == 2 and node["status"] == "unresolved"

    repo.execute(
        "MATCH (c:EdgeInvalidationConflict {uuid: $u}) SET c.status = 'resolved'",
        {"u": node["uuid"]},
    )
    asyncio.run(record())
    (node,) = _conflict_node(repo, kept_uid)
    assert node["status"] == "resolved"  # a repeat sighting never reopens it
    assert node["seen_count"] == 3

    entity_props_after = {
        r["uuid"]: r["props"]
        for r in repo.execute(
            "MATCH (e:Entity) WHERE e.uuid IN $us RETURN e.uuid AS uuid, properties(e) AS props",
            {"us": [ent_a, ent_b]},
        )
    }
    assert entity_props_after == entity_props_before
    assert not any(
        "conflict_status" in p or "conflict_group_id" in p for p in entity_props_after.values()
    )


# ---- NEWER_CANDIDATE through the real temporal pass ----


async def _newer_candidate(driver, newer_source: str, repo, ns: str):
    target_uid = f"target-{uuidlib.uuid4().hex[:8]}"
    newer_uid = f"newer-{uuidlib.uuid4().hex[:8]}"
    _seed_stored(repo, target_uid, ns, "user")
    _seed_stored(repo, newer_uid, ns, newer_source)
    stored_target = _edge(target_uid, "lives in Paris", OLD, ns)
    stored_newer = _edge(newer_uid, "lives in Rome", NEW, ns)
    new_extracted = _edge(f"new-{uuidlib.uuid4().hex[:8]}", "lives in Rome", NEW, ns)
    state = InvalidationGuardState(owner="o", source="claude-code")
    hook = MenhirEdgeInvalidationHook(driver)

    async def call():
        invalidation_guard_state.set(state)
        # resolved_edge is the stored target (not the extracted mention), so it reaches the
        # NEWER_CANDIDATE branch: no own invalid_at, not yet expired.
        return await asyncio.create_task(
            _apply_temporal_invalidation(
                stored_target, [stored_newer], new_extracted, None, edge_invalidation_hook=hook
            )
        )

    await asyncio.create_task(call())
    return stored_target, state


@pytest.mark.online
@pytest.mark.parametrize(
    ("newer_source", "target_expired"),
    [
        ("user", True),         # a stored human cause supersedes (documented design)
        ("claude-code", False),  # a stored agent cause cannot end a human target
    ],
)
def test_newer_candidate_authority_is_the_cause_edge_source(
    test_neo4j_repo, newer_source, target_expired
):
    ns = f"inv-{uuidlib.uuid4().hex[:8]}"

    async def run():
        driver = _driver()
        try:
            return await _newer_candidate(driver, newer_source, test_neo4j_repo, ns)
        finally:
            await driver.close()

    target, state = asyncio.run(run())
    if target_expired:
        assert target.expired_at is not None
        assert target.invalid_at == NEW
    else:
        assert target.invalid_at is None and target.expired_at is None
    assert state.kept == []  # NEWER_CANDIDATE is never recorded as a conflict
