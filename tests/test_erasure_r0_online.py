"""Online counterexample tests for erasure audit remediation R0.1, R0.5 and R0.6.

Plan: menhir-erasure-audit-remediation-plan-2026-11-11 (CF-324, CF-334, CF-336). Real Neo4j through
``test_neo4j_repo``; the sidecar journal is a per-test SQLite file. The rigs are imported from the
existing P4 test modules; the Graphiti fork is the recording ``FakeFork`` (no LLM, no paid calls).
"""

from __future__ import annotations

import uuid as uuidlib
from typing import Any

import pytest

from menhir.infrastructure.erasure_fence import (
    release_memory,
    seal_memory,
    seal_namespace,
)
from tests.test_erasure_fence_seal_online import _Rig as SealRig
from tests.test_erasure_p4_worker_fence import Rig as WorkerRig


# ------------------------------------------------------------------ R0.1 (CF-324) part a
@pytest.mark.online
async def test_failed_fence_release_leaves_the_op_and_the_orphan_job_releases_it(
    test_neo4j_repo, tmp_path, monkeypatch
) -> None:
    """CF-324 change 1: an erase whose release raises reports fence_release_failed and leaves the
    op in erasing_ops; the scheduler job function releases it because the journal row is terminal.

    Fails if ``erasure_fence_orphans`` stops releasing terminal-row ops (or is not wired to the
    coordinator's sweep), or if the coordinator hides the release failure.
    """
    from menhir.services.scheduler_tasks import erasure_fence_orphans

    rig = SealRig(test_neo4j_repo, tmp_path)
    # The job builds ErasureCoordinator(graph_adapter=...) with the DEFAULT journal; point the
    # default sidecar path at the rig's db so both see the same journal row.
    monkeypatch.setenv("MENHIR_MCP_TELEMETRY_DB", str(rig.db))
    try:
        q = rig.seed_q()

        def _boom(_neo4j: Any, _op_id: str) -> list[str]:
            raise RuntimeError("release failed on purpose")

        with monkeypatch.context() as patch:
            patch.setattr("menhir.services.erasure_coordinator.release_memory", _boom)
            out = rig.coord.erase_memory(q)

        op_id = out["op_id"]
        assert out["reason"] == "erased_incomplete", out
        assert "fence_release_failed" in out["incomplete_reasons"], out
        assert rig.fence()["ops"] == [op_id], rig.fence()
        row = rig.journal.get(op_id)
        assert row is not None and row["state"] != "PREPARED", row  # terminal

        result = await erasure_fence_orphans(rig.adapter)
        assert result["released"] == [op_id], result
        assert rig.fence()["ops"] == [], rig.fence()
        assert (await erasure_fence_orphans(rig.adapter))["released"] == []  # idempotent
    finally:
        rig.cleanup()


# ------------------------------------------------------------------ R0.1 (CF-324) parts b, c
def _requeue_row(repo: Any, q: str) -> dict[str, Any]:
    rows = repo.execute(
        "MATCH (n:Episodic {uuid: $u}) RETURN n.processing_state AS state, "
        "n.processing_attempts AS attempts, n.processing_error AS error, "
        "n.fenced_requeues AS requeues, n.retry_after IS NOT NULL AS has_retry, "
        "CASE WHEN n.retry_after IS NULL THEN null "
        "ELSE n.retry_after.epochMillis - datetime().epochMillis END AS delay_ms",
        params={"u": q},
    )
    return dict(rows[0])


@pytest.mark.online
async def test_held_erasing_op_requeues_before_extraction_with_growing_backoff(
    test_neo4j_repo, tmp_path
) -> None:
    """CF-324 changes 2 and 3: with an op held in erasing_ops the worker requeues at gate entry,
    never calling the (paid) extraction, and retry_after doubles per requeue (5s, 10s, 20s).

    Fails if the gate-entry ``check_fence`` in ``run_graphiti_extraction`` is removed (the fork is
    then called before the publish veto), or if REQUEUE_CYPHER goes back to a constant 5s.
    """
    rig = WorkerRig(test_neo4j_repo, tmp_path, "r0b")
    op = uuidlib.uuid4().hex
    try:
        q = rig.seed_q()
        seal_namespace(test_neo4j_repo, op, rig.ns)  # held, never released
        delays: list[float] = []
        for n in range(3):
            if n:  # simulate the retry_after window passing; leave fenced_requeues alone
                test_neo4j_repo.execute(
                    "MATCH (n:Episodic {uuid: $u}) SET n.retry_after = null", params={"u": q}
                )
            await rig.run_worker(q)
            assert rig.fork.calls == [], f"extraction ran on attempt {n + 1}"
            row = _requeue_row(test_neo4j_repo, q)
            assert row["state"] == "PENDING", row
            assert row["attempts"] == 0, row  # the veto is not the episode's fault
            assert row["error"] is None, row
            assert row["requeues"] == n + 1, row
            assert row["has_retry"], row
            expected_ms = 5000 * 2**n
            assert expected_ms - 4000 < row["delay_ms"] <= expected_ms, (n, row)
            delays.append(row["delay_ms"])
        assert delays[0] < delays[1] < delays[2], delays
        assert rig.twins() == []
    finally:
        await rig.shutdown()
        release_memory(test_neo4j_repo, op)
        rig.cleanup()


@pytest.mark.online
@pytest.mark.parametrize("prior", [6, 7, 9])
async def test_fenced_requeue_delay_is_capped_at_ten_minutes(
    prior: int, test_neo4j_repo, tmp_path
) -> None:
    """CF-324 change 3: delay is min(600s, 5s * 2^n). n=6 -> 320s, n=7 would be 640s -> capped.

    Fails if the cap is removed (n=7 and n=9 exceed 600s) or lowered (n=6 stays under it).
    """
    rig = WorkerRig(test_neo4j_repo, tmp_path, f"r0c{prior}")
    op = uuidlib.uuid4().hex
    try:
        q = rig.seed_q()
        test_neo4j_repo.execute(
            "MATCH (n:Episodic {uuid: $u}) SET n.fenced_requeues = $n",
            params={"u": q, "n": prior},
        )
        seal_namespace(test_neo4j_repo, op, rig.ns)
        await rig.run_worker(q)
        row = _requeue_row(test_neo4j_repo, q)
        assert rig.fork.calls == []
        assert row["state"] == "PENDING" and row["requeues"] == prior + 1, row
        expected_ms = min(600, 5 * 2**prior) * 1000
        assert expected_ms - 5000 < row["delay_ms"] <= expected_ms, (prior, row)
        assert row["delay_ms"] > 300_000 or prior < 7, row
    finally:
        await rig.shutdown()
        release_memory(test_neo4j_repo, op)
        rig.cleanup()


# ------------------------------------------------------------------ R0.5 (CF-334)
@pytest.mark.online
def test_replay_after_a_crash_between_seal_and_record_deletes_the_closure_only_twin(
    test_neo4j_repo, tmp_path
) -> None:
    """CF-334: the seal found a reserved twin the request never listed, and the process died before
    the closure was recorded. Replay must read the op's markers back, record the twin as a
    NODE_UUID subject and delete it.

    Fails if ``replay_prepared_row`` stops unioning ``_sealed_closure_uuids`` into its node set.
    """
    rig = SealRig(test_neo4j_repo, tmp_path)
    try:
        q, twin = rig.uid("q"), rig.uid("twin")
        test_neo4j_repo.execute(
            """
            CREATE (q:Episodic {uuid: $q, group_id: $ns, namespace: $ns, processing_state: 'READY',
                                reserved_twin_uuids: [$twin]})
            CREATE (t:Episodic {uuid: $twin, group_id: $ns, name: 'twin', content: 'twin content'})
            """,
            params={"q": q, "twin": twin, "ns": rig.ns},
        )
        op = uuidlib.uuid4().hex
        rig.prepare_row(op, q, [("NODE_UUID", q)])  # the request knew only q
        sealed = seal_memory(test_neo4j_repo, op, [q])
        # Crash here: the closure is never recorded.
        assert twin in sealed.closure_uuids, sealed  # only the seal can find it
        before = {r["subject_value"] for r in rig.subjects.fetch_subjects(op, subject_type="NODE_UUID")}
        assert before == {q}, before
        assert rig.fence()["ops"] == [op]
        row = rig.journal.get(op)
        assert row is not None and row["state"] == "PREPARED", row

        rig.coord.replay_prepared_row(row)

        survivors = test_neo4j_repo.execute(
            "MATCH (n:Episodic) WHERE n.uuid IN $u RETURN n.uuid AS uuid", params={"u": [q, twin]}
        )
        assert [r["uuid"] for r in survivors] == [], survivors
        after = {r["subject_value"] for r in rig.subjects.fetch_subjects(op, subject_type="NODE_UUID")}
        assert twin in after and q in after, after
        assert rig.fence()["ops"] == []
        done = rig.journal.get(op)
        assert done is not None and done["state"] != "PREPARED", done
    finally:
        rig.cleanup()


# ------------------------------------------------------------------ R0.6 (CF-336)
@pytest.mark.online
def test_publish_protocol_is_bound_to_its_claim(test_neo4j_repo, tmp_path) -> None:
    """CF-336: a fresh real claim is fenced (claimed_at == processing_started_at); an old-code
    re-claim that moves processing_started_at without the protocol fields is unfenced_inflight.

    Fails if the claim stops stamping ``publish_protocol_claimed_at`` (first half) or the seal goes
    back to trusting ``publish_protocol = 2`` alone (second half).
    """
    rig = SealRig(test_neo4j_repo, tmp_path)
    op1, op2 = uuidlib.uuid4().hex, uuidlib.uuid4().hex
    try:
        q = rig.seed_q()
        claimed = rig.adapter.claim_pending_episode(
            q, max_attempts=3, worker_id="r0-worker", lease_seconds=300
        )
        assert claimed is not None and claimed["publish_protocol"] == 2, claimed
        state = test_neo4j_repo.execute(
            "MATCH (n:Episodic {uuid: $q}) RETURN n.processing_state AS state, "
            "n.publish_protocol AS proto, "
            "n.publish_protocol_claimed_at = n.processing_started_at AS bound",
            params={"q": q},
        )[0]
        assert state["state"] == "ENRICHING" and state["proto"] == 2, state
        assert state["bound"] is True, state

        try:
            fresh = seal_memory(test_neo4j_repo, op1, [q])
            assert fresh.unfenced_inflight == [], fresh
        finally:
            release_memory(test_neo4j_repo, op1)

        # Old code re-claims: processing_started_at moves, the protocol fields stay.
        test_neo4j_repo.execute(
            "MATCH (n:Episodic {uuid: $q}) "
            "SET n.processing_started_at = datetime() + duration({seconds: 1})",
            params={"q": q},
        )
        try:
            stale = seal_memory(test_neo4j_repo, op2, [q])
            assert stale.unfenced_inflight == [q], stale
        finally:
            release_memory(test_neo4j_repo, op2)
    finally:
        rig.cleanup()
