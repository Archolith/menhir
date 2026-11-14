"""#623 and #622: admission audits against a turn's erasure, and one record per verdict.

#623: an audit lives in its own namespace while the turn it names lives in the user's, and the
turn's erasure locks only the user namespace's fence. The audit write must serialize with that
erasure, so no audit can store the id of a turn whose erasure has committed. Both orders are run
with a held transaction, so the tests show the writers block, not only the final state.

#622: identical verdicts are separate records.
"""

from __future__ import annotations

import os
import threading
import uuid

import neo4j
import pytest

from menhir.infrastructure import memory_graph_adapter as mga
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.infrastructure.turn_evidence_repository import TurnEvidenceRepository
from menhir.infrastructure.view_write_repository import ViewWriteConflict

pytestmark = pytest.mark.online

#: How long a writer that should be blocked is given to (wrongly) finish.
_BLOCKED_S = 2.0
_JOIN_S = 60


@pytest.fixture
def env(test_neo4j_repo):
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    adapter.bootstrap_phase_one()
    adapter.ensure_evidence_lifecycle_schema()
    tag = uuid.uuid4().hex[:10]
    # Sorted fence order puts the turn's fence first, so a held audit fence parks the audit
    # while it holds the turn's fence.
    ns_turn, ns_audit = f"a623-{tag}", f"z623-{tag}"
    yield adapter, ns_turn, ns_audit
    nss = [ns_turn, ns_audit]
    test_neo4j_repo.execute(
        "MATCH (n:Entity {view_kind: 'admission_audit'}) WHERE n.group_id IN $nss "
        "DETACH DELETE n", params={"nss": nss})
    test_neo4j_repo.execute(
        "MATCH (n:TurnEvidence) WHERE n.namespace IN $nss DETACH DELETE n", params={"nss": nss})
    test_neo4j_repo.execute(
        "MATCH (r:ViewProjectionRepair) WHERE r.namespace IN $nss OR r.namespace_key IN $nss "
        "DETACH DELETE r", params={"nss": nss})
    test_neo4j_repo.execute(
        "MATCH (f:EvidenceNamespaceFence) WHERE f.namespace_key IN $nss DETACH DELETE f",
        params={"nss": nss})


@pytest.fixture
def driver():
    drv = neo4j.GraphDatabase.driver(
        os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://127.0.0.1:7688"),
        auth=(os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j"),
              os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword")))
    yield drv
    drv.close()


def _database() -> str:
    return os.getenv("MENHIR_TEST_NEO4J_DATABASE", "neo4j")


def _plant_turn(repo, ns: str, turn_id: str) -> None:
    repo.execute(
        "MERGE (f:EvidenceNamespaceFence {namespace_key: $ns}) "
        "ON CREATE SET f.generation = 0, f.created_at = datetime() "
        "CREATE (:TurnEvidence {turn_id: $t, namespace: $ns, role: 'user'})",
        params={"t": turn_id, "ns": ns})


def _audits(repo, subject: str, ns: str) -> list[dict]:
    return [dict(r) for r in repo.execute(
        "MATCH (n:Entity {view_kind: 'admission_audit', view_subject: $s, group_id: $ns}) "
        "RETURN n.uuid AS uuid, n.turn_evidence_uuid AS turn, n.reason AS reason, "
        "coalesce(n.view_current, true) AS current, coalesce(n.retired, false) AS retired",
        params={"s": subject, "ns": ns})]


def _named(repo, turn_id: str) -> int:
    return repo.execute(
        "MATCH (n:Entity) WHERE n.turn_evidence_uuid = $t RETURN count(n) AS c",
        params={"t": turn_id})[0]["c"]


def _start(fn) -> tuple[threading.Thread, dict]:
    out: dict = {}
    done = threading.Event()

    def run() -> None:
        try:
            out["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertions
            out["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    out["done"] = done
    return thread, out


def _denial(subject: str, ns: str, turn_id: str | None, reason: str = "denied") -> dict:
    return {"subject": subject, "namespace": ns, "requested_source": "user",
            "effective_source": "agent", "granted": False, "turn_evidence_uuid": turn_id,
            "reason": reason}


def test_an_audit_waits_for_an_in_flight_erasure_and_drops_the_erased_id(
    env, test_neo4j_repo, driver
) -> None:
    """The issue's interleaving: the verdict names T, T's erasure is in flight, then the first
    audit write runs. The write blocks on T's fence and, once the erasure commits, keeps the
    record without T's id."""
    adapter, ns_turn, ns_audit = env
    subject, turn = f"user-{uuid.uuid4().hex[:8]}", f"{ns_turn}-turn"
    _plant_turn(test_neo4j_repo, ns_turn, turn)

    with driver.session(database=_database()) as session:
        erasure = session.begin_transaction()
        erasure.run(
            "MATCH (f:EvidenceNamespaceFence {namespace_key: $ns}) SET f.lock_nonce = 'erase' "
            "WITH f MATCH (t:TurnEvidence {turn_id: $t}) DETACH DELETE t",
            ns=ns_turn, t=turn).consume()
        thread, out = _start(
            lambda: adapter.record_admission_audit(**_denial(subject, ns_audit, turn)))
        try:
            assert not out["done"].wait(_BLOCKED_S), "audit did not wait for the erasure"
            assert _audits(test_neo4j_repo, subject, ns_audit) == []
        finally:
            erasure.commit()
        thread.join(_JOIN_S)

    assert "error" not in out, out.get("error")
    assert out["result"]["turn_evidence_uuid_recorded"] is False
    rows = _audits(test_neo4j_repo, subject, ns_audit)
    assert [(r["turn"], r["current"]) for r in rows] == [(None, True)]
    assert _named(test_neo4j_repo, turn) == 0


def _purge(repo, ns_turn: str, turn: str):
    return TurnEvidenceRepository(repo).purge_namespace(ns_turn)


def _delete_memory(repo, ns_turn: str, turn: str):
    return MemoryQueryRepository(repo).delete_memory_with_scalar_cascade(
        turn, operation_id=f"op-{uuid.uuid4().hex[:8]}")


def _delete_namespace(repo, ns_turn: str, turn: str):
    return MemoryQueryRepository(repo).delete_namespace_with_scalar_cascade(
        ns_turn, ns_turn, operation_id=f"op-{uuid.uuid4().hex[:8]}")


@pytest.mark.parametrize("erase", [_purge, _delete_memory, _delete_namespace],
                         ids=["purge_namespace", "delete_memory", "delete_namespace"])
def test_an_erasure_waits_for_an_in_flight_audit_and_strips_its_id(
    env, test_neo4j_repo, driver, erase
) -> None:
    """The other order: the audit holds T's fence (parked on its own namespace's fence), then
    T's real erasure starts. The erasure blocks until the audit commits, then retires the new
    audit and strips T's id, so nothing names T afterwards."""
    adapter, ns_turn, ns_audit = env
    subject, turn = f"user-{uuid.uuid4().hex[:8]}", f"{ns_turn}-turn"
    _plant_turn(test_neo4j_repo, ns_turn, turn)
    test_neo4j_repo.execute(
        "MERGE (f:EvidenceNamespaceFence {namespace_key: $ns}) "
        "ON CREATE SET f.generation = 0, f.created_at = datetime()", params={"ns": ns_audit})

    with driver.session(database=_database()) as session:
        hold = session.begin_transaction()
        hold.run("MATCH (f:EvidenceNamespaceFence {namespace_key: $ns}) "
                 "SET f.lock_nonce = 'hold'", ns=ns_audit).consume()
        audit_thread, audit = _start(
            lambda: adapter.record_admission_audit(**_denial(subject, ns_audit, turn)))
        erase_thread, erased = None, {}
        try:
            assert not audit["done"].wait(_BLOCKED_S), "audit did not wait for its fence"
            erase_thread, erased = _start(lambda: erase(test_neo4j_repo, ns_turn, turn))
            assert not erased["done"].wait(_BLOCKED_S), "erasure did not wait for the audit"
        finally:
            hold.rollback()
        audit_thread.join(_JOIN_S)
        if erase_thread is not None:
            erase_thread.join(_JOIN_S)

    assert "error" not in audit, audit.get("error")
    assert "error" not in erased, erased.get("error")
    # The audit stored T while T was live; the erasure that followed removed it.
    assert audit["result"]["turn_evidence_uuid_recorded"] is True
    rows = _audits(test_neo4j_repo, subject, ns_audit)
    assert len(rows) == 1
    assert rows[0]["turn"] is None and rows[0]["retired"] is True
    assert _named(test_neo4j_repo, turn) == 0


def test_identical_denials_each_leave_a_record(env, test_neo4j_repo) -> None:
    """#622: the common denial has no turn id, so every repeat used to share one signature
    and collapse into a single node."""
    adapter, _ns_turn, ns_audit = env
    subject = f"user-{uuid.uuid4().hex[:8]}"
    for _ in range(3):
        adapter.record_admission_audit(
            **_denial(subject, ns_audit, None, "no turn_evidence_uuid provided"))

    rows = _audits(test_neo4j_repo, subject, ns_audit)
    assert len(rows) == 3
    assert sum(1 for r in rows if r["current"]) == 1


def test_a_retry_of_a_verdict_that_did_commit_does_not_duplicate_it(
    env, test_neo4j_repo, monkeypatch
) -> None:
    """A verdict's retries share its id: if the first attempt committed but reported a
    conflict, the retry finds its own record current and refreshes it."""
    adapter, _ns_turn, ns_audit = env
    monkeypatch.setattr(mga, "_sleep", lambda _s: None)
    subject = f"user-{uuid.uuid4().hex[:8]}"
    real_record = adapter._views.record
    calls = {"n": 0}

    def commit_then_report_conflict(kind_name, **payload):
        calls["n"] += 1
        result = real_record(kind_name, **payload)
        if calls["n"] == 1:
            raise ViewWriteConflict("simulated: committed, then the reply was lost")
        return result

    monkeypatch.setattr(adapter._views, "record", commit_then_report_conflict)
    result = adapter.record_admission_audit(**_denial(subject, ns_audit, None))

    assert calls["n"] == 2
    assert result["created"] is False
    assert len(_audits(test_neo4j_repo, subject, ns_audit)) == 1
