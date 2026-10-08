"""Concurrent persist writers on one edge: exactly one contract wins (review 245-1).

Writer A runs the persist statement in an open transaction and holds the edge lock; writer B starts
while A is uncommitted, so B's first read still sees no contract. Without the lock-and-recheck B
would overwrite A's contract after A commits.
"""
from __future__ import annotations

from datetime import date
import os
import threading
import time
import uuid as uuidlib

import pytest

from menhir.domain.namespace import namespace_spellings
from menhir.infrastructure.anchored_time_persist import LOCK_PROPERTY, PERSIST_CYPHER

pytestmark = pytest.mark.online


def _row(uid: str, expression: str) -> dict:
    return {
        "uuid": uid, "time_basis": "speech_relative", "time_expression": expression,
        "time_kind": "point_event", "time_granularity": "month",
        "time_window_start": "2023-04-01", "time_window_end": "2023-04-30",
        "time_anchor_ref": None, "time_anchor_offset": None, "time_planned_start": None,
        "time_planned_end": None, "time_outcome": "written",
        "time_speech_date": date(2023, 5, 14).isoformat(), "time_contract": f"at1|p|{expression}",
    }


def test_concurrent_writers_on_one_edge_write_exactly_once(test_neo4j_repo):
    neo4j = pytest.importorskip("neo4j")
    g, ep, uid = f"g-{uuidlib.uuid4().hex[:8]}", str(uuidlib.uuid4()), str(uuidlib.uuid4())
    test_neo4j_repo.execute(
        "CREATE (:Entity {uuid: $a, group_id: $g})-[:RELATES_TO {uuid: $u, group_id: $g, "
        "episodes: [$ep], fact: 'f'}]->(:Entity {uuid: $b, group_id: $g})",
        params={"a": str(uuidlib.uuid4()), "b": str(uuidlib.uuid4()), "u": uid, "g": g, "ep": ep},
    )

    def params(expression: str) -> dict:
        return {"rows": [_row(uid, expression)], "group_id": g, "episode_uuid": ep,
                "tenant_namespaces": namespace_spellings(g)}

    # The same test instance test_neo4j_repo connected to (and prod-checked) above.
    database = os.getenv("MENHIR_TEST_NEO4J_DATABASE", "neo4j")
    driver = neo4j.GraphDatabase.driver(
        os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://127.0.0.1:7688"),
        auth=(os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j"),
              os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword")))
    try:
        session_a = driver.session(database=database)
        tx_a = session_a.begin_transaction()
        written_a = tx_a.run(PERSIST_CYPHER, params("writer A")).single()["written"]
        b_result: dict = {}

        def writer_b() -> None:
            with driver.session(database=database) as session_b:
                b_result["written"] = session_b.execute_write(
                    lambda tx: tx.run(PERSIST_CYPHER, params("writer B")).single()["written"])

        thread = threading.Thread(target=writer_b)
        thread.start()
        time.sleep(1.0)  # B reads no contract, then blocks on A's lock
        tx_a.commit()
        session_a.close()
        thread.join(timeout=30)
        assert not thread.is_alive()
    finally:
        driver.close()

    rows = test_neo4j_repo.execute(
        f"MATCH ()-[r:RELATES_TO {{uuid: $u}}]->() RETURN r.time_expression AS expression, "
        f"r.{LOCK_PROPERTY} AS lock",
        params={"u": uid},
    )
    assert (written_a, b_result["written"]) == (1, 0)
    assert rows[0]["expression"] == "writer A"
    assert rows[0]["lock"] is None  # the lock property never outlives the statement
