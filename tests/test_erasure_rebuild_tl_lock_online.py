"""T-L (erasure rebuild plan v2, section 6): lock-then-separate-statement re-read semantics.

Not xfail: a semantics probe that must pass before the fenced-publish work (P4) builds on it.
Extends tests/test_anchored_time_persist_lock_live.py's harness (two real transactions, commit of
the lock holder only after SHOW TRANSACTIONS reports the waiter blocked by it).

Schedule: tx A (the "erase seal") takes the fence lock by writing ``f.lock_nonce``, bumps
``f.erasure_epoch`` and MERGEs an ErasureMarker, then stays open. tx B (the "publisher") reads the
epoch BEFORE locking (stale view), then SETs the same lock property and blocks. A commits. B then
re-reads in SEPARATE ``tx.run`` statements of the same transaction and must see A's epoch bump and
A's marker node. Breaks (fails) if Neo4j's read-committed re-read inside a transaction that has
just acquired a lock returned pre-A state, which would invalidate the lock-then-recheck idiom.
"""
from __future__ import annotations

import os
import threading
import time
import uuid as uuidlib

import pytest

pytestmark = pytest.mark.online

_BLOCK_WAIT_S = 20.0


def _wait_until_blocked(driver, database: str, tag: str) -> bool:
    deadline = time.monotonic() + _BLOCK_WAIT_S
    with driver.session(database=database) as monitor:
        while time.monotonic() < deadline:
            rows = monitor.run(
                "SHOW TRANSACTIONS YIELD transactionId, status, metaData "
                "WHERE metaData.tl IN [$a, $b] RETURN transactionId, status, metaData.tl AS who",
                a=f"{tag}-a", b=f"{tag}-b").data()
            ids = {row["who"]: row for row in rows}
            a, b = ids.get(f"{tag}-a"), ids.get(f"{tag}-b")
            if a and b and str(b["status"]).startswith("Blocked by") \
                    and a["transactionId"] in str(b["status"]):
                return True
            time.sleep(0.05)
    return False


def test_lock_then_separate_run_reread_sees_epoch_bump_and_merged_marker(test_neo4j_repo):
    neo4j = pytest.importorskip("neo4j")
    ns = f"tl-{uuidlib.uuid4().hex[:12]}"
    marker_uuid = str(uuidlib.uuid4())
    op_id = uuidlib.uuid4().hex
    tag = uuidlib.uuid4().hex
    test_neo4j_repo.execute(
        "MERGE (f:EvidenceNamespaceFence {namespace_key: $ns}) "
        "SET f.erasure_epoch = 0, f.generation = 0",
        params={"ns": ns},
    )
    database = os.getenv("MENHIR_TEST_NEO4J_DATABASE", "neo4j")
    driver = neo4j.GraphDatabase.driver(
        os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://127.0.0.1:7688"),
        auth=(os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j"),
              os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword")))
    result: dict = {}
    try:
        session_a = driver.session(database=database)
        tx_a = session_a.begin_transaction(metadata={"tl": f"{tag}-a"})
        tx_a.run(
            "MATCH (f:EvidenceNamespaceFence {namespace_key: $ns}) "
            "SET f.lock_nonce = $n SET f.erasure_epoch = f.erasure_epoch + 1, "
            "f.erasing_ops = [$op]", ns=ns, n="a", op=op_id).consume()
        tx_a.run(
            "MERGE (m:ErasureMarker {namespace_key: $ns, uuid: $u, op_id: $op}) "
            "SET m.sealed_at = datetime()", ns=ns, u=marker_uuid, op=op_id).consume()

        @neo4j.unit_of_work(metadata={"tl": f"{tag}-b"})
        def publisher(tx):
            q = "MATCH (f:EvidenceNamespaceFence {namespace_key: $ns}) "
            # Stale read before locking: the gate-entry epoch the worker would have held.
            result["epoch_before_lock"] = tx.run(
                q + "RETURN f.erasure_epoch AS e", ns=ns).single()["e"]
            tx.run(q + "SET f.lock_nonce = $n", ns=ns, n="b").consume()  # blocks on A
            # Separate statements of the SAME transaction, after the lock was granted.
            row = tx.run(q + "RETURN f.erasure_epoch AS e, f.erasing_ops AS ops",
                         ns=ns).single()
            result["epoch_after_lock"], result["ops_after_lock"] = row["e"], row["ops"]
            result["markers_after_lock"] = tx.run(
                "MATCH (m:ErasureMarker {namespace_key: $ns, uuid: $u}) RETURN count(m) AS c",
                ns=ns, u=marker_uuid).single()["c"]
            return True

        def run_b() -> None:
            try:
                with driver.session(database=database) as session_b:
                    result["b_done"] = session_b.execute_write(publisher)
            except Exception as exc:  # surfaced by the assertions
                result["error_b"] = exc

        thread = threading.Thread(target=run_b)
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
        test_neo4j_repo.execute(
            "MATCH (n) WHERE (n:EvidenceNamespaceFence OR n:ErasureMarker) "
            "AND n.namespace_key = $ns DETACH DELETE n", params={"ns": ns})

    assert result["blocked"], "tx B was never observed blocked by tx A's lock"
    assert "error_b" not in result, result.get("error_b")
    assert result["epoch_before_lock"] == 0  # the stale pre-lock view
    assert result["epoch_after_lock"] == 1, "re-read after lock missed A's epoch bump"
    assert result["ops_after_lock"] == [op_id]
    assert result["markers_after_lock"] == 1, "re-read after lock missed A's MERGEd marker"
