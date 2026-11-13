"""#604: concurrent admission denials for one subject each leave an audit record.

Since #600 intake runs in worker threads, so one subject's denials can race on the shared
view key's compare-and-set. Before the fix the loser's record was dropped at DEBUG.
"""

from __future__ import annotations

import logging
import threading
import uuid

import pytest

from menhir.infrastructure import memory_graph_adapter as mga
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

pytestmark = pytest.mark.online

#: More than the old fixed 5 attempts, so a fixed retry count without backoff could lose one.
_WRITERS = 8

_AUDITS = (
    "MATCH (n:Entity {view_kind: 'admission_audit', view_subject: $s, group_id: $ns}) "
)


@pytest.fixture
def adapter_and_ns(test_neo4j_repo):
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    adapter.bootstrap_phase_one()
    adapter.ensure_evidence_lifecycle_schema()
    ns = f"t604-{uuid.uuid4().hex[:10]}"
    yield adapter, ns
    test_neo4j_repo.execute(
        "MATCH (n:Entity {view_kind: 'admission_audit'}) WHERE n.group_id = $ns "
        "DETACH DELETE n",
        params={"ns": ns},
    )
    test_neo4j_repo.execute(
        "MATCH (n:TurnEvidence) WHERE n.namespace = $ns DETACH DELETE n", params={"ns": ns}
    )
    test_neo4j_repo.execute(
        "MATCH (f:EvidenceNamespaceFence {namespace_key: $ns}) DETACH DELETE f",
        params={"ns": ns},
    )


def _plant_turns(repo, ns: str, turn_ids: list[str]) -> None:
    repo.execute(
        "UNWIND $ids AS t CREATE (:TurnEvidence {turn_id: t, namespace: $ns, role: 'user'})",
        params={"ids": turn_ids, "ns": ns},
    )


def test_concurrent_denials_for_one_subject_each_leave_an_audit_record(
    adapter_and_ns, test_neo4j_repo, caplog
) -> None:
    adapter, ns = adapter_and_ns
    subject = f"user-{uuid.uuid4().hex[:8]}"
    _plant_turns(test_neo4j_repo, ns, [f"{ns}-turn-{i}" for i in range(_WRITERS)])
    barrier = threading.Barrier(_WRITERS)
    errors: list[BaseException] = []

    def deny(i: int) -> None:
        try:
            barrier.wait(10)
            adapter.record_admission_audit(
                subject=subject, namespace=ns, requested_source="user",
                effective_source="agent", granted=False,
                turn_evidence_uuid=f"{ns}-turn-{i}", reason=f"denied-{i}",
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)

    caplog.set_level(logging.DEBUG, logger="menhir.infrastructure.memory_graph_adapter")
    threads = [threading.Thread(target=deny, args=(i,), daemon=True) for i in range(_WRITERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not any(t.is_alive() for t in threads), "a writer hung"

    assert errors == []
    rows = test_neo4j_repo.execute(
        _AUDITS + "RETURN n.reason AS reason, n.turn_evidence_uuid AS turn, "
        "coalesce(n.view_current, true) AS current",
        params={"s": subject, "ns": ns},
    )
    assert sorted(r["reason"] for r in rows) == sorted(f"denied-{i}" for i in range(_WRITERS))
    assert all(r["turn"] == f"{ns}-turn-{r['reason'].split('-')[1]}" for r in rows)
    assert sum(1 for r in rows if r["current"]) == 1
    # Not asserted (the scheduler decides), but recorded so a run shows the race was exercised.
    retries = sum("lost a concurrent write" in r.getMessage() for r in caplog.records)
    print(f"admission audit retries observed: {retries}")


def _stale_first_read(monkeypatch, views, stale: dict) -> dict:
    real_current = views._current_by_key
    calls = {"n": 0}

    def first_read_is_stale(key, *args, **kwargs):
        calls["n"] += 1
        return stale if calls["n"] == 1 else real_current(key, *args, **kwargs)

    monkeypatch.setattr(views, "_current_by_key", first_read_is_stale)
    return calls


def _current_version(repo, subject: str, ns: str) -> dict:
    rows = repo.execute(
        _AUDITS + "WHERE coalesce(n.view_current, true) "
        "RETURN n.uuid AS uuid, n.view_sig AS sig, toString(n.valid_at) AS valid_at",
        params={"s": subject, "ns": ns},
    )
    assert len(rows) == 1
    return dict(rows[0])


def test_a_same_signature_write_based_on_a_superseded_version_is_retried(
    adapter_and_ns, test_neo4j_repo, monkeypatch
) -> None:
    """The unchanged-signature path touches the version it read. If a different denial
    superseded that version first, the touch finds nothing; that is the same lost race and
    must be retried, not dropped (#604)."""
    adapter, ns = adapter_and_ns
    subject = f"user-{uuid.uuid4().hex[:8]}"
    turn_a, turn_b = f"{ns}-turn-a", f"{ns}-turn-b"
    _plant_turns(test_neo4j_repo, ns, [turn_a, turn_b])
    denial_a = dict(subject=subject, namespace=ns, requested_source="user",
                    effective_source="agent", granted=False, turn_evidence_uuid=turn_a,
                    reason="denied-a")
    adapter.record_admission_audit(**denial_a)
    stale = _current_version(test_neo4j_repo, subject, ns)
    adapter.record_admission_audit(**{**denial_a, "turn_evidence_uuid": turn_b,
                                      "reason": "denied-b"})

    calls = _stale_first_read(monkeypatch, adapter._views, stale)
    adapter.record_admission_audit(**denial_a)

    assert calls["n"] == 2  # the stale attempt lost and was retried from a fresh read
    rows = test_neo4j_repo.execute(
        _AUDITS + "RETURN n.reason AS reason, coalesce(n.view_current, true) AS current",
        params={"s": subject, "ns": ns},
    )
    assert sorted(r["reason"] for r in rows) == ["denied-a", "denied-a", "denied-b"]
    assert [r["reason"] for r in rows if r["current"]] == ["denied-a"]


def test_a_retry_after_the_turn_is_erased_does_not_write_the_turn_id_back(
    adapter_and_ns, test_neo4j_repo, monkeypatch
) -> None:
    """Denial D reads the current audit V naming turn T; an erasure of T then retires V and
    strips the id (as turn_evidence_repository does). D's write loses and retries; the retry
    must keep the record but not re-store the erased id."""
    adapter, ns = adapter_and_ns
    monkeypatch.setattr(mga, "_sleep", lambda _s: None)
    subject = f"user-{uuid.uuid4().hex[:8]}"
    turn = f"{ns}-turn-erased"
    _plant_turns(test_neo4j_repo, ns, [turn])
    denial = dict(subject=subject, namespace=ns, requested_source="user",
                  effective_source="agent", granted=False, turn_evidence_uuid=turn,
                  reason="denied")
    adapter.record_admission_audit(**denial)
    stale = _current_version(test_neo4j_repo, subject, ns)
    test_neo4j_repo.execute(
        "MATCH (t:TurnEvidence {turn_id: $t}) DETACH DELETE t", params={"t": turn}
    )
    test_neo4j_repo.execute(
        "MATCH (v:Entity {uuid: $u}) SET v.view_current = false, v.qs_current = false, "
        "v.retired = true, v.retired_reason = 'contributing_evidence_erased' "
        "REMOVE v.turn_evidence_uuid",
        params={"u": stale["uuid"]},
    )

    calls = _stale_first_read(monkeypatch, adapter._views, stale)
    adapter.record_admission_audit(**denial)

    assert calls["n"] == 2
    rows = test_neo4j_repo.execute(
        _AUDITS + "RETURN n.turn_evidence_uuid AS turn, coalesce(n.view_current, true) AS current",
        params={"s": subject, "ns": ns},
    )
    assert len(rows) == 2
    assert all(r["turn"] is None for r in rows)
    assert sum(1 for r in rows if r["current"]) == 1
