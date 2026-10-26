"""Online: 2a statement-order veto through the fork's real temporal-invalidation pass.

Real ``_apply_temporal_invalidation`` + real ``MenhirEdgeInvalidationHook`` + real Neo4j
(throwaway test instance, fixture ``test_neo4j_repo``). Run with::

    MENHIR_TEST_NEO4J_URI=bolt://127.0.0.1:<port> pytest --run-online \
        tests/test_edge_invalidation_statement_order_online.py
"""

from __future__ import annotations

import asyncio
import os
import uuid as uuidlib
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("graphiti_core.edge_invalidation")

from graphiti_core.driver.neo4j_driver import Neo4jDriver  # noqa: E402
from graphiti_core.edges import EntityEdge  # noqa: E402
from graphiti_core.utils.maintenance.edge_operations import (  # noqa: E402
    _apply_temporal_invalidation,
)

from menhir.infrastructure.graphiti_edge_invalidation import (  # noqa: E402
    InvalidationGuardState,
    MenhirEdgeInvalidationHook,
    invalidation_guard_state,
)

SAID_EARLY = datetime(2023, 3, 1, tzinfo=timezone.utc)
SAID_LATE = datetime(2023, 5, 20, tzinfo=timezone.utc)
# Event dates: "had it for 3 months" (said early) dates later than "switched yesterday" (said late).
SWITCHED = datetime(2023, 5, 19, tzinfo=timezone.utc)
FOR_3_MONTHS = datetime(2023, 6, 1, tzinfo=timezone.utc)


def _edge(uid: str, fact: str, valid_at: datetime, ns: str) -> EntityEdge:
    return EntityEdge(
        uuid=uid, group_id=ns, source_node_uuid=f"{uid}-s", target_node_uuid=f"{uid}-t",
        created_at=valid_at, name="USES", fact=fact, episodes=[], valid_at=valid_at,
    )


def _seed(repo, uid: str, ns: str, source: str, said: datetime) -> None:
    repo.execute(
        """
        CREATE (a:Entity {uuid: $s, group_id: $ns, name: 'a'})
        CREATE (b:Entity {uuid: $t, group_id: $ns, name: 'b'})
        CREATE (a)-[:RELATES_TO {uuid: $uid, group_id: $ns, fact: 'f', source: $source,
                                 reference_time: datetime($said)}]->(b)
        """,
        {"s": f"{uid}-s", "t": f"{uid}-t", "uid": uid, "ns": ns, "source": source,
         "said": said.isoformat()},
    )


def _driver() -> Neo4jDriver:
    return Neo4jDriver(
        uri=os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://127.0.0.1:7688"),
        user=os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j"),
        password=os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword"),
        database=os.getenv("MENHIR_TEST_NEO4J_DATABASE", "neo4j"),
    )


def _run(resolved, candidates, extracted, episode_said, call_source, on):
    async def go():
        driver = _driver()
        try:
            state = InvalidationGuardState(owner="o", source=call_source)
            hook = MenhirEdgeInvalidationHook(driver, statement_order=on)

            async def call():
                invalidation_guard_state.set(state)
                return await asyncio.create_task(
                    _apply_temporal_invalidation(
                        resolved, candidates, extracted, SimpleNamespace(valid_at=episode_said),
                        edge_invalidation_hook=hook,
                    )
                )

            return await asyncio.create_task(call()), state
        finally:
            await driver.close()

    return asyncio.run(go())


def _ns() -> str:
    return f"so-{uuidlib.uuid4().hex[:8]}"


@pytest.mark.online
@pytest.mark.parametrize("on", [True, False])
def test_newer_candidate_new_mention_said_later_stays_live(test_neo4j_repo, on):
    ns = _ns()
    stored_uid = f"stored-{uuidlib.uuid4().hex[:8]}"
    _seed(test_neo4j_repo, stored_uid, ns, "user", SAID_EARLY)
    stored = _edge(stored_uid, "has had the Fitbit for 3 months", FOR_3_MONTHS, ns)
    new = _edge(f"new-{uuidlib.uuid4().hex[:8]}", "switched to the Fitbit yesterday", SWITCHED, ns)

    invalidated, _state = _run(new, [stored], new, SAID_LATE, "user", on)

    assert stored.expired_at is None and invalidated == []
    if on:
        assert new.expired_at is None and new.invalid_at is None  # both live
    else:
        assert new.expired_at is not None and new.invalid_at == FOR_3_MONTHS  # today's answer


@pytest.mark.online
@pytest.mark.parametrize("on", [True, False])
def test_contradiction_from_mention_said_earlier_keeps_stored_edge(test_neo4j_repo, on):
    # Out-of-order ingest: the stored edge was said later than this call's mention.
    ns = _ns()
    stored_uid = f"stored-{uuidlib.uuid4().hex[:8]}"
    _seed(test_neo4j_repo, stored_uid, ns, "user", SAID_LATE)
    stored = _edge(stored_uid, "switched to the Fitbit yesterday", SWITCHED, ns)
    new = _edge(f"new-{uuidlib.uuid4().hex[:8]}", "has had the Fitbit for 3 months", FOR_3_MONTHS, ns)

    invalidated, _state = _run(new, [stored], new, SAID_EARLY, "user", on)

    assert new.expired_at is None
    if on:
        assert invalidated == [] and stored.expired_at is None and stored.invalid_at is None
    else:
        assert [e.uuid for e in invalidated] == [stored_uid] and stored.expired_at is not None


@pytest.mark.online
def test_contradiction_from_mention_said_later_still_invalidates(test_neo4j_repo):
    ns = _ns()
    stored_uid = f"stored-{uuidlib.uuid4().hex[:8]}"
    _seed(test_neo4j_repo, stored_uid, ns, "user", SAID_EARLY)
    stored = _edge(stored_uid, "lives in Paris", SWITCHED, ns)
    new = _edge(f"new-{uuidlib.uuid4().hex[:8]}", "lives in Rome", FOR_3_MONTHS, ns)

    invalidated, _state = _run(new, [stored], new, SAID_LATE, "user", True)

    assert [e.uuid for e in invalidated] == [stored_uid]
    assert stored.invalid_at == FOR_3_MONTHS and stored.expired_at is not None


@pytest.mark.online
def test_agent_mention_said_later_still_kept_by_authority(test_neo4j_repo):
    ns = _ns()
    stored_uid = f"stored-{uuidlib.uuid4().hex[:8]}"
    _seed(test_neo4j_repo, stored_uid, ns, "user", SAID_EARLY)
    stored = _edge(stored_uid, "lives in Paris", SWITCHED, ns)
    new = _edge(f"new-{uuidlib.uuid4().hex[:8]}", "lives in Rome", FOR_3_MONTHS, ns)

    invalidated, state = _run(new, [stored], new, SAID_LATE, "claude-code", True)

    assert invalidated == [] and stored.expired_at is None
    assert [k.kept_edge_uuid for k in state.kept] == [stored_uid]


@pytest.mark.online
def test_stored_time_in_another_group_is_not_read(test_neo4j_repo):
    # Same uuid seeded under another group_id: the lookup is scoped, so no time -> today's answer.
    ns = _ns()
    stored_uid = f"stored-{uuidlib.uuid4().hex[:8]}"
    _seed(test_neo4j_repo, stored_uid, f"{ns}-other", "user", SAID_LATE)
    stored = _edge(stored_uid, "switched to the Fitbit yesterday", SWITCHED, ns)
    new = _edge(f"new-{uuidlib.uuid4().hex[:8]}", "has had the Fitbit for 3 months", FOR_3_MONTHS, ns)

    invalidated, _state = _run(new, [stored], new, SAID_EARLY, "user", True)

    assert [e.uuid for e in invalidated] == [stored_uid]
