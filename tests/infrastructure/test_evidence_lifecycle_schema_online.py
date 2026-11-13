"""#603: the evidence-lifecycle uniqueness constraints come online at start, over existing duplicates.

Each test drops the four constraints first (they are database-wide) and leaves them online.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

import pytest
from neo4j import GraphDatabase

from menhir.infrastructure.erasure_fence import fence_lock_cypher
from menhir.infrastructure.evidence_lifecycle_schema import (
    DUPLICATE_FENCE_LABEL,
    EVIDENCE_LIFECYCLE_REQUIRED_CONSTRAINTS,
    EvidenceLifecycleSchemaError,
    ensure_evidence_lifecycle_schema,
    evidence_lifecycle_schema_ready,
)

pytestmark = pytest.mark.online


def _drop_constraints(repo) -> None:
    for name, *_ in EVIDENCE_LIFECYCLE_REQUIRED_CONSTRAINTS:
        repo.execute(f"DROP CONSTRAINT {name} IF EXISTS")


@pytest.fixture
def repo(test_neo4j_repo):
    _drop_constraints(test_neo4j_repo)
    keys: list[str] = []
    yield test_neo4j_repo, keys
    test_neo4j_repo.execute(
        "MATCH (n) WHERE (n:EvidenceNamespaceFence OR n:EvidenceNamespaceFenceDuplicate) "
        "AND n.namespace_key IN $keys DETACH DELETE n",
        params={"keys": keys},
    )
    test_neo4j_repo.execute(
        "MATCH (n) WHERE (n:EvidencePublicationIntent OR n:ViewProjectionRepair) "
        "AND n.test_tag IN $keys DETACH DELETE n",
        params={"keys": keys},
    )
    ensure_evidence_lifecycle_schema(test_neo4j_repo)


def _key(keys: list[str]) -> str:
    key = f"t603-{uuid.uuid4().hex[:10]}"
    keys.append(key)
    return key


def test_merges_duplicate_fences_into_one_holding_the_highest_epoch(repo) -> None:
    neo4j, keys = repo
    key = _key(keys)
    neo4j.execute(
        "UNWIND $copies AS c CREATE (f:EvidenceNamespaceFence) SET f = c, f.namespace_key = $key, "
        "f.created_at = datetime()",
        params={"key": key, "copies": [
            {"erasure_epoch": 2, "generation": 0, "erasing_ops": ["a"]},
            {"erasure_epoch": 5, "generation": 1, "erasing_ops": ["b"]},
            {"erasure_epoch": 3, "generation": 0, "erasing_ops": ["a", "c"]},
        ]},
    )
    # A publication intent hangs off a copy that will be merged away.
    neo4j.execute(
        "MATCH (f:EvidenceNamespaceFence {namespace_key: $key, erasure_epoch: 2}) "
        "CREATE (f)-[:GOVERNS_PUBLICATION]->(:EvidencePublicationIntent "
        "{intent_key: $key + '-i', test_tag: $key})",
        params={"key": key},
    )
    assert not evidence_lifecycle_schema_ready(neo4j)

    result = ensure_evidence_lifecycle_schema(neo4j)

    assert result["status"] == "ok" and not result["skipped"]
    assert evidence_lifecycle_schema_ready(neo4j)
    rows = neo4j.execute(
        "MATCH (f:EvidenceNamespaceFence {namespace_key: $key}) "
        "OPTIONAL MATCH (f)-[:GOVERNS_PUBLICATION]->(i:EvidencePublicationIntent) "
        "RETURN f.erasure_epoch AS epoch, f.generation AS generation, "
        "f.erasing_ops AS ops, collect(i.intent_key) AS intents",
        params={"key": key},
    )
    assert len(rows) == 1
    assert rows[0]["epoch"] == 5
    assert rows[0]["generation"] == 1
    assert sorted(rows[0]["ops"]) == ["a", "b", "c"]
    assert rows[0]["intents"] == [key + "-i"]
    kept = neo4j.execute(
        f"MATCH (d:{DUPLICATE_FENCE_LABEL} {{namespace_key: $key}}) "
        "RETURN count(d) AS n, collect(d.erasure_epoch) AS epochs",
        params={"key": key},
    )
    assert kept[0]["n"] == 2 and sorted(kept[0]["epochs"]) == [2, 3]


def test_second_run_is_a_no_op(repo) -> None:
    neo4j, _ = repo
    ensure_evidence_lifecycle_schema(neo4j)
    assert ensure_evidence_lifecycle_schema(neo4j)["skipped"] is True


def _lock_in_thread(key: str, barrier: threading.Barrier, errors: list[BaseException]) -> None:
    driver = GraphDatabase.driver(
        os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://127.0.0.1:7688"),
        auth=(os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j"),
              os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword")),
    )
    try:
        with driver.session(database=os.getenv("MENHIR_TEST_NEO4J_DATABASE", "neo4j")) as s:
            barrier.wait(10)
            tx = s.begin_transaction()
            tx.run(fence_lock_cypher("$key"), key=key, fence_nonce=uuid.uuid4().hex).consume()
            time.sleep(0.2)  # hold the lock so the other first writers overlap it
            tx.commit()
    except BaseException as exc:  # noqa: BLE001 - reported by the test
        errors.append(exc)
    finally:
        driver.close()


def test_concurrent_first_writers_lock_one_fence_once_constrained(repo) -> None:
    """The #603 race: first writers in a new namespace all MERGE at once; one node results."""
    neo4j, keys = repo
    ensure_evidence_lifecycle_schema(neo4j)
    key = _key(keys)
    writers = 6
    barrier = threading.Barrier(writers)
    errors: list[BaseException] = []
    threads = [threading.Thread(target=_lock_in_thread, args=(key, barrier, errors))
               for _ in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    rows = neo4j.execute(
        "MATCH (f:EvidenceNamespaceFence {namespace_key: $key}) RETURN count(f) AS n",
        params={"key": key},
    )
    assert rows[0]["n"] == 1


def test_refuses_duplicate_repair_keys(repo) -> None:
    neo4j, keys = repo
    key = _key(keys)
    neo4j.execute(
        "UNWIND [1, 2] AS _ CREATE (:ViewProjectionRepair {repair_key: $key, test_tag: $key})",
        params={"key": key},
    )
    with pytest.raises(EvidenceLifecycleSchemaError, match="operator review"):
        ensure_evidence_lifecycle_schema(neo4j)
    assert not evidence_lifecycle_schema_ready(neo4j)
    neo4j.execute("MATCH (r:ViewProjectionRepair {test_tag: $key}) DETACH DELETE r",
                  params={"key": key})


def test_refuses_to_merge_a_fence_copy_with_unknown_relationships(repo) -> None:
    neo4j, keys = repo
    key = _key(keys)
    neo4j.execute(
        "CREATE (:EvidenceNamespaceFence {namespace_key: $key, erasure_epoch: 1}) "
        "CREATE (d:EvidenceNamespaceFence {namespace_key: $key, erasure_epoch: 0}) "
        "CREATE (d)-[:SOMETHING_ELSE]->(:EvidencePublicationIntent "
        "{intent_key: $key + '-x', test_tag: $key})",
        params={"key": key},
    )
    with pytest.raises(EvidenceLifecycleSchemaError, match="SOMETHING_ELSE"):
        ensure_evidence_lifecycle_schema(neo4j)
    rows = neo4j.execute(
        "MATCH (f:EvidenceNamespaceFence {namespace_key: $key}) RETURN count(f) AS n",
        params={"key": key},
    )
    assert rows[0]["n"] == 2  # the refused merge rolled back whole
    neo4j.execute(
        "MATCH (f:EvidenceNamespaceFence {namespace_key: $key, erasure_epoch: 0}) DETACH DELETE f",
        params={"key": key},
    )
