"""A sync write that takes the namespace fence lock must never wait for it on the event loop.

The publish fence holds ``EvidenceNamespaceFence`` inside Graphiti's async publish transaction,
which only the event loop can commit. A sync driver call that queues for the same lock ON the
loop therefore waits forever: the release PR #599 stdio E2E hung this way (an admitted
add_memory's evidence projection behind a same-namespace publish).

Each test holds the fence lock from another thread, runs the code under test beside a
heartbeat, and fails if the loop stops ticking while the lock is held.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from neo4j import GraphDatabase

from menhir.domain.session import new_session
from menhir.infrastructure.erasure_fence import fence_lock_cypher
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.publish_fence import ClaimFence, FenceLost

pytestmark = pytest.mark.online

HOLD_S = 1.5
MAX_GAP_S = 0.5


def _hold_fence(namespace_key: str, acquired: threading.Event) -> threading.Thread:
    """Hold the namespace's fence lock in an open write transaction for HOLD_S, then roll back."""

    def run() -> None:
        driver = GraphDatabase.driver(
            os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://127.0.0.1:7688"),
            auth=(os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j"),
                  os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword")),
        )
        try:
            with driver.session(database=os.getenv("MENHIR_TEST_NEO4J_DATABASE", "neo4j")) as s:
                tx = s.begin_transaction()
                tx.run(fence_lock_cypher("$key"), key=namespace_key,
                       fence_nonce=uuid.uuid4().hex).consume()
                acquired.set()
                time.sleep(HOLD_S)
                tx.rollback()
        finally:
            driver.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert acquired.wait(10), "could not take the fence lock"
    return thread


async def _run_beside_heartbeat(work: Any) -> tuple[Any, float]:
    """Run ``work`` while a heartbeat ticks; return its result (or exception) and the longest gap."""
    gaps: list[float] = []
    stop = asyncio.Event()

    async def heartbeat() -> None:
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)
    try:
        result: Any = await work
    except Exception as exc:  # noqa: BLE001 - the loop's liveness is what is under test
        result = exc
    stop.set()
    await beat
    return result, max(gaps)


def test_admitted_add_memory_does_not_block_the_loop_on_the_fence(test_neo4j_repo) -> None:
    """Fails if queue_episode_for_enrichment waits for the fence lock on the event loop."""
    from menhir.services.ingest_intake import IngestIntakeMixin

    namespace = f"fence-loop-{uuid.uuid4().hex[:8]}"
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    turn = adapter.record_turn_evidence(
        text="I have 25 postcards", role="user", declarant="user", source_kind="e2e",
        session_id="s-fence", namespace=namespace,
    )

    class _Intake(IngestIntakeMixin):
        graph_adapter = adapter
        _enrichment_enabled = False

    acquired = threading.Event()
    holder = _hold_fence(namespace, acquired)
    result, gap = asyncio.run(_run_beside_heartbeat(_Intake().queue_episode_for_enrichment(
        "The user has 25 postcards", new_session("u-fence", session_id="s-fence"),
        "claude-code", namespace=namespace, turn_evidence_uuid=turn["turn_id"],
    )))
    holder.join(10)
    try:
        assert gap < MAX_GAP_S, f"event loop blocked for {gap:.2f}s behind the fence lock"
        assert not isinstance(result, Exception), result
        assert str(result.status.value if hasattr(result.status, "value") else result.status) \
            .lower() == "queued"
        projected = test_neo4j_repo.execute(
            "MATCH (p:Episodic {evidence_projection_of: $t}) RETURN count(p) AS n",
            params={"t": turn["turn_id"]},
        )
        assert projected[0]["n"] == 1, "the projection is still created once the lock frees"
    finally:
        test_neo4j_repo.execute(
            "MATCH (n) WHERE n.namespace = $ns OR n.group_id = $ns DETACH DELETE n "
            "WITH 1 AS x MATCH (f:EvidenceNamespaceFence {namespace_key: $ns}) DETACH DELETE f",
            params={"ns": namespace},
        )


def _hold_episode_lock(episode_uuid: str, acquired: threading.Event) -> threading.Thread:
    """Hold a write lock on the episode for HOLD_S, as LOCK_CYPHER does for the claimed queue node."""

    def run() -> None:
        driver = GraphDatabase.driver(
            os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://127.0.0.1:7688"),
            auth=(os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j"),
                  os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword")),
        )
        try:
            with driver.session(database=os.getenv("MENHIR_TEST_NEO4J_DATABASE", "neo4j")) as s:
                tx = s.begin_transaction()
                tx.run("MATCH (q:Episodic {uuid: $u}) SET q.lock_nonce = $n",
                       u=episode_uuid, n=uuid.uuid4().hex).consume()
                acquired.set()
                time.sleep(HOLD_S)
                tx.rollback()
        finally:
            driver.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert acquired.wait(10), "could not lock the episode"
    return thread


def test_shutdown_does_not_block_the_loop_on_a_locked_episode(test_neo4j_repo) -> None:
    """Fails if shutdown releases this worker's leases on the event loop (#601)."""
    from menhir.services.ingest_queue import IngestQueueMixin

    namespace = f"fence-loop-{uuid.uuid4().hex[:8]}"
    worker_id = f"w-shutdown-{uuid.uuid4().hex[:8]}"
    episode_uuid = str(uuid.uuid4())
    test_neo4j_repo.execute(
        "CREATE (:Episodic {uuid: $u, namespace: $ns, group_id: $ns, processing_state: 'ENRICHING', "
        "processing_owner: $w, processing_attempts: 1})",
        params={"u": episode_uuid, "ns": namespace, "w": worker_id},
    )

    class _Queue(IngestQueueMixin):
        graph_adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
        _worker_id = worker_id
        _max_enrichment_attempts = 3

        def __init__(self) -> None:
            self._worker_tasks = []
            self._pending_queue = None
            self._queued_episode_ids = set()

    try:
        acquired = threading.Event()
        holder = _hold_episode_lock(episode_uuid, acquired)
        result, gap = asyncio.run(_run_beside_heartbeat(_Queue().shutdown()))
        holder.join(10)
        assert gap < MAX_GAP_S, f"event loop blocked for {gap:.2f}s behind the episode lock"
        assert result == 1, result
        row = test_neo4j_repo.execute(
            "MATCH (n:Episodic {uuid: $u}) RETURN n.processing_state AS s", params={"u": episode_uuid}
        )
        assert row[0]["s"] == "PENDING", "the lease is still released once the lock frees"
    finally:
        test_neo4j_repo.execute(
            "MATCH (n:Episodic {uuid: $u}) DETACH DELETE n", params={"u": episode_uuid}
        )


def test_fenced_post_publish_write_does_not_block_the_loop(test_neo4j_repo) -> None:
    """Fails if stamp_and_finalize's fenced writes take the fence lock on the event loop."""
    from menhir.services import enrichment_steps

    namespace = f"fence-loop-{uuid.uuid4().hex[:8]}"
    ctx = SimpleNamespace(
        graph_adapter=MemoryGraphAdapter(neo4j=test_neo4j_repo),
        claim_fence=ClaimFence(
            q_uuid=str(uuid.uuid4()), worker_id="w-fence", claim_started_at=None,
            namespace_key=namespace, reserved_twin_uuid=str(uuid.uuid4()),
        ),
        gate_fence=None,
    )
    acquired = threading.Event()
    holder = _hold_fence(namespace, acquired)
    result, gap = asyncio.run(_run_beside_heartbeat(
        enrichment_steps._fenced_write_off_loop(ctx, lambda ex: None)
    ))
    holder.join(10)
    try:
        assert gap < MAX_GAP_S, f"event loop blocked for {gap:.2f}s behind the fence lock"
        # The queue node does not exist, so once the lock frees the re-check refuses.
        assert isinstance(result, FenceLost), result
    finally:
        test_neo4j_repo.execute(
            "MATCH (f:EvidenceNamespaceFence {namespace_key: $ns}) DETACH DELETE f",
            params={"ns": namespace},
        )
