"""#604: concurrent admission denials for one subject each leave an audit record.

Since #600 intake runs in worker threads, so one subject's denials can race on the shared
view key's compare-and-set. Before the fix the loser's record was dropped at DEBUG.
"""

from __future__ import annotations

import logging
import threading
import uuid

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

pytestmark = pytest.mark.online

_WRITERS = 4


@pytest.fixture
def adapter_and_ns(test_neo4j_repo):
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    adapter.bootstrap_phase_one()
    adapter.ensure_evidence_lifecycle_schema()
    ns = f"t604-{uuid.uuid4().hex[:10]}"
    yield adapter, ns
    test_neo4j_repo.execute(
        "MATCH (n) WHERE n.group_id = $ns OR n.namespace_key = $ns DETACH DELETE n",
        params={"ns": ns},
    )


def test_concurrent_denials_for_one_subject_each_leave_an_audit_record(
    adapter_and_ns, test_neo4j_repo, caplog
) -> None:
    adapter, ns = adapter_and_ns
    subject = f"user-{uuid.uuid4().hex[:8]}"
    barrier = threading.Barrier(_WRITERS)
    errors: list[BaseException] = []

    def deny(i: int) -> None:
        try:
            barrier.wait(10)
            adapter.record_admission_audit(
                subject=subject, namespace=ns, requested_source="user",
                effective_source="agent", granted=False,
                turn_evidence_uuid=f"turn-{i}", reason=f"denied-{i}",
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)

    caplog.set_level(logging.INFO, logger="menhir.infrastructure.memory_graph_adapter")
    threads = [threading.Thread(target=deny, args=(i,)) for i in range(_WRITERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    assert errors == []
    rows = test_neo4j_repo.execute(
        "MATCH (n:Entity {view_kind: 'admission_audit', view_subject: $s, group_id: $ns}) "
        "RETURN n.reason AS reason, coalesce(n.view_current, true) AS current",
        params={"s": subject, "ns": ns},
    )
    assert sorted(r["reason"] for r in rows) == sorted(f"denied-{i}" for i in range(_WRITERS))
    assert sum(1 for r in rows if r["current"]) == 1
    # Not asserted (the scheduler decides), but recorded so a run shows the race was exercised.
    retries = sum("lost a concurrent write" in r.getMessage() for r in caplog.records)
    print(f"admission audit retries observed: {retries}")
