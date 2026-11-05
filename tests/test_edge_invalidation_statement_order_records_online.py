"""Online: a 2a KEEP through the real fork pass is written as an EdgeInvalidationConflict.

Run with::

    MENHIR_TEST_NEO4J_URI=bolt://127.0.0.1:<port> pytest --run-online \
        tests/test_edge_invalidation_statement_order_records_online.py
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
    record_kept_conflicts,
)

SAID_EARLY = datetime(2023, 3, 1, tzinfo=timezone.utc)
SAID_LATE = datetime(2023, 5, 20, tzinfo=timezone.utc)
SWITCHED = datetime(2023, 5, 19, tzinfo=timezone.utc)
FOR_3_MONTHS = datetime(2023, 6, 1, tzinfo=timezone.utc)


def _edge(uid: str, fact: str, valid_at: datetime, ns: str) -> EntityEdge:
    return EntityEdge(
        uuid=uid, group_id=ns, source_node_uuid=f"{uid}-s", target_node_uuid=f"{uid}-t",
        created_at=valid_at, name="USES", fact=fact, episodes=[], valid_at=valid_at,
    )


def _driver() -> Neo4jDriver:
    return Neo4jDriver(
        uri=os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://127.0.0.1:7688"),
        user=os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j"),
        password=os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword"),
        database=os.getenv("MENHIR_TEST_NEO4J_DATABASE", "neo4j"),
    )


@pytest.mark.online
def test_statement_order_keep_is_recorded_and_entities_untouched(test_neo4j_repo):
    repo = test_neo4j_repo
    ns = f"sor-{uuidlib.uuid4().hex[:8]}"
    stored_uid = f"stored-{uuidlib.uuid4().hex[:8]}"
    repo.execute(
        """
        CREATE (a:Entity {uuid: $s, group_id: $ns, name: 'user'})
        CREATE (b:Entity {uuid: $t, group_id: $ns, name: 'Fitbit'})
        CREATE (a)-[:RELATES_TO {uuid: $uid, group_id: $ns, fact: 'f', source: 'user',
                                 reference_time: datetime($said)}]->(b)
        """,
        {"s": f"{stored_uid}-s", "t": f"{stored_uid}-t", "uid": stored_uid, "ns": ns,
         "said": SAID_EARLY.isoformat()},
    )
    stored = _edge(stored_uid, "has had the Fitbit for 3 months", FOR_3_MONTHS, ns)
    new = _edge(f"new-{uuidlib.uuid4().hex[:8]}", "switched to a Garmin yesterday", SWITCHED, ns)

    async def go():
        driver = _driver()
        try:
            state = InvalidationGuardState(owner="o", source="user")
            hook = MenhirEdgeInvalidationHook(driver, statement_order=True)

            async def call():
                invalidation_guard_state.set(state)
                await asyncio.create_task(
                    _apply_temporal_invalidation(
                        new, [stored], new, SimpleNamespace(valid_at=SAID_LATE),
                        edge_invalidation_hook=hook,
                    )
                )

            await asyncio.create_task(call())
            return state, await record_kept_conflicts(driver, state.kept)
        finally:
            await driver.close()

    state, written = asyncio.run(go())

    assert new.expired_at is None and stored.expired_at is None
    assert written == 1
    (row,) = repo.execute(
        "MATCH (c:EdgeInvalidationConflict {kept_edge_uuid: $k}) "
        "RETURN c.cause_edge_uuid AS cause, c.reason AS reason, c.kept_by AS kept_by, "
        "c.status AS status, c.group_id AS group_id, c.cause_source AS cause_source",
        {"k": new.uuid},
    )
    assert row == {
        "cause": stored_uid, "reason": "newer_candidate", "kept_by": "statement_order",
        "status": "unresolved", "group_id": ns, "cause_source": "user",
    }
    flagged = repo.execute(
        "MATCH (e:Entity {group_id: $ns}) WHERE e.conflict_group_id IS NOT NULL "
        "OR e.conflict_status IS NOT NULL RETURN count(e) AS n",
        {"ns": ns},
    )
    assert flagged[0]["n"] == 0
