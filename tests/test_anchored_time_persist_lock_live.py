"""Concurrent persist writers on one edge: exactly one contract wins (reviews 245-1, R2-6).

Writer A runs the persist statement in an open transaction and holds the edge lock. Writer B starts
while A is uncommitted, and A commits only after ``SHOW TRANSACTIONS`` reports B blocked by A, so
the contested schedule is observed rather than assumed. The control runs the same schedule with the
pre-lock statement and must see B overwrite A, which shows the harness exercises the race.
"""
from __future__ import annotations

from datetime import date
import os
import threading
import time
import uuid as uuidlib

import pytest

from menhir.domain.namespace import namespace_spellings, tenant_scope_cypher
from menhir.infrastructure.anchored_time_persist import LOCK_PROPERTY, PERSIST_CYPHER

pytestmark = pytest.mark.online

#: The statement before the lock-and-recheck (360b40f^), verbatim: read eligibility once, then SET.
_PRE_LOCK_PROPERTIES = (
    "time_basis", "time_expression", "time_kind", "time_granularity", "time_window_start",
    "time_window_end", "time_anchor_ref", "time_anchor_offset", "time_planned_start",
    "time_planned_end", "time_outcome", "time_speech_date", "time_contract",
)
PRE_LOCK_CYPHER = (
    "UNWIND $rows AS row "
    "MATCH ()-[r:RELATES_TO {uuid: row.uuid}]->() "
    "WHERE " + tenant_scope_cypher("r") + " "
    "AND r.group_id = $group_id "
    "AND r.time_contract IS NULL "
    "AND $episode_uuid IN coalesce(r.episodes, []) "
    "SET " + ", ".join(f"r.{name} = row.{name}" for name in _PRE_LOCK_PROPERTIES) + " "
    "RETURN count(r) AS written"
)

_BLOCK_WAIT_S = 20.0


def _row(uid: str, expression: str) -> dict:
    return {
        "uuid": uid, "time_basis": "speech_relative", "time_expression": expression,
        "time_kind": "point_event", "time_granularity": "month",
        "time_window_start": "2023-04-01", "time_window_end": "2023-04-30",
        "time_anchor_ref": None, "time_anchor_offset": None, "time_planned_start": None,
        "time_planned_end": None, "time_outcome": "written",
        "time_speech_date": date(2023, 5, 14).isoformat(), "time_ambiguity": None,
        "time_contract": f"at1|p|{expression}",
    }


def _race(test_neo4j_repo, statement: str) -> dict:
    """Run A (held open) and B on one edge; commit A only once B is seen blocked by A."""
    neo4j = pytest.importorskip("neo4j")
    g, ep, uid = f"g-{uuidlib.uuid4().hex[:8]}", str(uuidlib.uuid4()), str(uuidlib.uuid4())
    tag = uuidlib.uuid4().hex
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
    result: dict = {}
    try:
        session_a = driver.session(database=database)
        tx_a = session_a.begin_transaction(metadata={"r26": f"{tag}-a"})
        result["written_a"] = tx_a.run(statement, params("writer A")).single()["written"]

        @neo4j.unit_of_work(metadata={"r26": f"{tag}-b"})
        def write_b(tx):
            return tx.run(statement, params("writer B")).single()["written"]

        def writer_b() -> None:
            try:
                with driver.session(database=database) as session_b:
                    result["written_b"] = session_b.execute_write(write_b)
            except Exception as exc:  # surfaced by the assertions below
                result["error_b"] = exc

        thread = threading.Thread(target=writer_b)
        thread.start()
        try:
            result["blocked"] = _wait_until_blocked(driver, database, tag)
        finally:
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
    result["expression"], result["lock"] = rows[0]["expression"], rows[0]["lock"]
    return result


def _wait_until_blocked(driver, database: str, tag: str) -> dict | None:
    """B's transaction row once its status is 'Blocked by' A's transaction id, else None."""
    deadline = time.monotonic() + _BLOCK_WAIT_S
    with driver.session(database=database) as monitor:
        while time.monotonic() < deadline:
            rows = monitor.run(
                "SHOW TRANSACTIONS YIELD transactionId, status, metaData "
                "WHERE metaData.r26 IN [$a, $b] RETURN transactionId, status, metaData.r26 AS who",
                a=f"{tag}-a", b=f"{tag}-b").data()
            ids = {row["who"]: row for row in rows}
            a, b = ids.get(f"{tag}-a"), ids.get(f"{tag}-b")
            if a and b and str(b["status"]).startswith("Blocked by") \
                    and a["transactionId"] in str(b["status"]):
                return {"a": a["transactionId"], "b": b["transactionId"], "status": b["status"]}
            time.sleep(0.05)
    return None


def test_concurrent_writers_on_one_edge_write_exactly_once(test_neo4j_repo):
    result = _race(test_neo4j_repo, PERSIST_CYPHER)
    assert result["blocked"], "writer B was never observed blocked on writer A's lock"
    assert "error_b" not in result, result.get("error_b")
    assert (result["written_a"], result["written_b"]) == (1, 0)
    assert result["expression"] == "writer A"
    assert result["lock"] is None  # the lock property never outlives the statement


def test_pre_lock_statement_loses_the_race(test_neo4j_repo):
    """Control: under the same observed schedule the old statement lets B overwrite A."""
    result = _race(test_neo4j_repo, PRE_LOCK_CYPHER)
    assert result["blocked"], "writer B was never observed blocked on writer A's lock"
    assert "error_b" not in result, result.get("error_b")
    assert (result["written_a"], result["written_b"]) == (1, 1)
    assert result["expression"] == "writer B"
