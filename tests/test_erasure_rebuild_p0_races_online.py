"""P0 counterexamples for the erasure rebuild, lane B: enrichment, publish and erase races.

Plan: menhir-erasure-rebuild-plan-v2 section 6 (T1, T2, T3, T4a, T4b, T5, T11, T12, T16, T18, T19,
T25, T26). Every test EXCEPT T18 (which passes today and is a plain regression guard) is
``xfail(strict=True, raises=AssertionError)``: it states the behaviour the
rebuilt design promises and FAILS on today's code on the intended assertion. When the rebuild lands
the marker is removed.

How the races are driven (no LLM, no paid calls, public entry points only):

* the real ``IngestService._process_pending_episode`` worker (claim, heartbeat, reconcile, embed,
  gate, ``add_episode``, ``stamp_and_finalize``) runs against a real Neo4j;
* the Graphiti fork is replaced by ``FakeFork``, mounted under the REAL ``GraphitiClient`` wrapper so
  the worker's call shape is unchanged. ``FakeFork.add_episode`` does one real write transaction that
  creates a twin ``:Episodic`` (+ entity, + fact edge), and it blocks on asyncio barriers BEFORE that
  transaction opens or AFTER it commits, never inside it. It honours ``new_episode_uuid`` and
  ``publish_fence`` (before_write/after_write inside the transaction) if the worker passes them; the
  worker passes neither at e574abdf, which is exactly why the publish is unfenced today;
* erase goes through ``ErasureCoordinator.erase_memory``; the reaper and the scheduler retry job are
  called through their own entry points.

Honesty rules: the assertion that decides each test is a plain ``assert``. Everything that merely
establishes the scenario (the worker really claimed, the twin really committed, the erase really ran
inside the window, the canary was present beforehand) is a ``_require`` that raises ``HarnessError``
(a RuntimeError), so a broken harness is reported as an ERROR rather than xfailing "successfully".
Cypher is read at the instants the plan names, with the worker still parked or immediately after the
fake's commit attempt, and the compensation sweep is never run.
"""

from __future__ import annotations

import asyncio
import os
import uuid as uuidlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

import pytest

from menhir.infrastructure.erasure_subjects import ErasureSubjectStore
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.services.erasure_coordinator import ErasureCoordinator

SUCCESS_REASONS = frozenset({"erased", "graph_already_absent", "nothing_to_erase"})
_WAIT_S = 25.0


def p0(reason: str):
    """The P0 marker: must fail on the intended assertion today, and only on an assertion."""
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=reason)


# ------------------------------------------------------------------ harness
class HarnessError(RuntimeError):
    """The scenario could not be set up. Never an AssertionError, so it can never xfail a test."""


def _require(condition: object, message: str) -> None:
    if not condition:
        raise HarnessError(message)


async def _bounded(awaitable: Awaitable[Any], what: str, timeout_s: float = _WAIT_S) -> Any:
    try:
        return await asyncio.wait_for(awaitable, timeout_s)
    except asyncio.TimeoutError as exc:
        raise HarnessError(f"timed out waiting for {what}") from exc


class Gate:
    """A two-sided asyncio barrier: the worker parks on it, the test releases it."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def park(self) -> None:
        self.reached.set()
        await _bounded(self.release.wait(), f"release of gate {self.name}", 60.0)

    async def wait_reached(self) -> None:
        await _bounded(self.reached.wait(), f"gate {self.name} to be reached")

    def open(self) -> None:
        self.release.set()


class FakeEmbedder:
    """Graphiti's embedder surface that GraphitiClient.embed_query calls."""

    model = "fake-embed-model"

    def __init__(self) -> None:
        self.before_return: Callable[[], Awaitable[None]] | None = None

    async def create(self, _input: Any) -> list[float]:
        if self.before_return is not None:
            await self.before_return()
        return [0.25, 0.5, 0.75]


class FakeFork:
    """Stands in for ``graphiti_core.Graphiti`` under the real ``GraphitiClient`` wrapper."""

    def __init__(self, repo: Any, tag: str, canary: str) -> None:
        self.repo = repo
        self.tag = tag
        self.canary = canary
        self.calls: list[SimpleNamespace] = []
        self.handler: Callable[[FakeFork, SimpleNamespace], Awaitable[Any]] | None = None

    async def add_episode(
        self,
        *,
        name: str,
        episode_body: str,
        source_description: str,
        reference_time: datetime,
        group_id: str,
        **kw: Any,
    ) -> Any:
        n = len(self.calls) + 1
        call = SimpleNamespace(
            n=n,
            name=name,
            body=episode_body,
            group_id=group_id,
            kw=kw,
            twin_uuid=str(kw.get("new_episode_uuid") or f"{self.tag}-twin{n}"),
            published=False,
        )
        self.calls.append(call)
        if self.handler is None:
            return await self.publish(call)
        return await self.handler(self, call)

    async def publish(self, call: SimpleNamespace, *, entities: bool = True) -> Any:
        """One real write transaction: the twin, and (optionally) an entity and a fact edge, each
        carrying the canary. Runs ``publish_fence`` hooks inside the transaction if one was passed."""
        fence = call.kw.get("publish_fence")
        loop = asyncio.get_running_loop()
        t, g = call.twin_uuid, call.group_id
        e1, e2, edge, mention = (f"{t}-e1", f"{t}-e2", f"{t}-edge", f"{t}-mention")

        def work(tx: Any) -> None:
            if fence is not None:
                asyncio.run_coroutine_threadsafe(
                    fence.before_write(tx, episode_uuid=t, previous_episode_uuids=[]), loop
                ).result(30)
            tx.execute(
                """
                CREATE (:Episodic {uuid:$tu, group_id:$g, name:$nm, content:$body,
                                   valid_at:datetime(), created_at:datetime()})
                """,
                {"tu": t, "g": g, "nm": call.name, "body": call.body},
            )
            if entities:
                tx.execute(
                    """
                    MATCH (t:Episodic {uuid:$tu})
                    CREATE (a:Entity {uuid:$e1, name:$n1, group_id:$g, summary:$canary,
                                      created_at:datetime()})
                    CREATE (b:Entity {uuid:$e2, name:$n2, group_id:$g, summary:'a colour',
                                      created_at:datetime()})
                    CREATE (t)-[:MENTIONS {uuid:$mu, group_id:$g, created_at:datetime()}]->(a)
                    CREATE (a)-[:RELATES_TO {uuid:$ru, name:'HAS_COLOUR', fact:$fact,
                                             episodes:[$tu], group_id:$g,
                                             created_at:datetime()}]->(b)
                    """,
                    {
                        "tu": t, "g": g, "e1": e1, "e2": e2, "n1": f"{t}-ent1", "n2": f"{t}-ent2",
                        "canary": self.canary, "mu": mention, "ru": edge,
                        "fact": f"{self.canary} is the favourite colour",
                    },
                )
            if fence is not None:
                asyncio.run_coroutine_threadsafe(
                    fence.after_write(
                        tx,
                        episode_uuid=t,
                        entity_node_uuids=[e1, e2] if entities else [],
                        entity_edge_uuids=[edge] if entities else [],
                        previous_episode_uuids=[],
                    ),
                    loop,
                ).result(30)

        await asyncio.to_thread(self.repo.execute_write, work)
        call.published = True
        return SimpleNamespace(
            episode=SimpleNamespace(uuid=t),
            nodes=[SimpleNamespace(uuid=e1, name="", content=""), SimpleNamespace(uuid=e2, name="", content="")]
            if entities
            else [],
            edges=[SimpleNamespace(uuid=edge)] if entities else [],
            episodic_edges=[],
        )


class Rig:
    """One test's world: a private tag, the real adapter/coordinator/service, and a fake fork."""

    def __init__(self, repo: Any, tmp_path: Path, tag: str) -> None:
        from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

        self.repo = repo
        self.tag = f"{tag}-{uuidlib.uuid4().hex[:6]}"
        self.ns = f"{self.tag}-ns"
        self.canary = f"CANARY-{tag.upper()}-{uuidlib.uuid4().hex[:8]}"
        self.adapter = MemoryGraphAdapter(neo4j=repo)
        db = tmp_path / "p0-races.db"
        McpTelemetryStore(db_path=db)._ensure_ready()
        self.coord = ErasureCoordinator(
            graph_adapter=self.adapter,
            journal=GraphOperationsJournal(db_path=db),
            subjects=ErasureSubjectStore(db_path=db),
        )
        self.fork = FakeFork(repo, self.tag, self.canary)
        self.embedder = FakeEmbedder()
        self.service = self.make_service()
        self.tasks: list[asyncio.Task[Any]] = []
        self.gates: list[Gate] = []
        self.n_seeded = 0

    def make_service(self) -> Any:
        from menhir.infrastructure import GraphitiClient
        from menhir.services.ingest_service import IngestService

        client = GraphitiClient(client=self.fork, embedder_ref=self.embedder)
        service = IngestService(graphiti_client=client, graph_adapter=self.adapter, llm=None)
        service._processing_heartbeat_interval_s = 3600.0
        return service

    def gate(self, name: str) -> Gate:
        g = Gate(name)
        self.gates.append(g)
        return g

    # -- seeding / reading
    def seed_q(self, *, namespace: str | None = None, name: str | None = None, canary: str | None = None) -> str:
        self.n_seeded += 1
        q = f"{self.tag}-q{self.n_seeded}"
        text = f"The probe's favourite colour is {canary or self.canary}"
        self.adapter.create_pending_episode(
            episode_uuid=q,
            name=name or f"anchor-{q}",
            content=text,
            session_id=f"{self.tag}-session",
            user_id="p0-user",
            source="claude-code",
            source_confidence=0.5,
            namespace=namespace or self.ns,
        )
        return q

    def q_row(self, q: str) -> dict[str, Any] | None:
        rows = self.repo.execute(
            """
            MATCH (n:Episodic {uuid:$u})
            RETURN n.processing_state AS state, n.processing_substage AS substage,
                   n.processing_attempts AS attempts, n.resolved_episode_uuid AS resolved,
                   n.processing_owner AS owner, n.publish_protocol AS publish_protocol,
                   n.content_embedding IS NOT NULL AS has_embedding, n.erased_at IS NOT NULL AS erased
            """,
            params={"u": q},
        )
        return dict(rows[0]) if rows else None

    def carriers(self, canary: str | None = None) -> list[str]:
        """Every node and relationship still holding the canary in a text property."""
        phrase = canary or self.canary
        nodes = self.repo.execute(
            """
            MATCH (n)
            WHERE any(k IN ['content','summary','fact','diff','text','original_content',
                            'processing_error'] WHERE n[k] CONTAINS $p)
            RETURN labels(n)[0] AS label, coalesce(n.uuid, '?') AS uuid
            """,
            params={"p": phrase},
        )
        rels = self.repo.execute(
            """
            MATCH ()-[r]->()
            WHERE any(k IN ['fact','name','episode_body'] WHERE r[k] CONTAINS $p)
            RETURN type(r) AS label, coalesce(r.uuid, '?') AS uuid
            """,
            params={"p": phrase},
        )
        return sorted([f"{r['label']}:{r['uuid']}" for r in nodes] + [f"rel:{r['label']}:{r['uuid']}" for r in rels])

    # -- driving
    def start_worker(self, q: str, service: Any = None) -> asyncio.Task[Any]:
        task = asyncio.create_task((service or self.service)._process_pending_episode(q))
        self.tasks.append(task)
        return task

    async def run_worker(self, q: str) -> None:
        await _bounded(self.start_worker(q), f"worker for {q} to finish")

    async def wait_enriching(self, q: str, attempts: int | None = None) -> dict[str, Any]:
        for _ in range(int(_WAIT_S * 20)):
            row = self.q_row(q)
            if row and row["state"] == "ENRICHING" and (attempts is None or row["attempts"] == attempts):
                return row
            await asyncio.sleep(0.05)
        raise HarnessError(f"{q} never reached ENRICHING (attempts={attempts}): {self.q_row(q)}")

    async def erase(self, q: str, **kw: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self.coord.erase_memory, q, **kw)

    async def shutdown(self) -> None:
        for g in self.gates:
            g.open()
        for task in self.tasks:
            if not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), 5.0)
                except BaseException:  # noqa: BLE001 - teardown only
                    task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for task in list(self.service._worker_tasks):
            task.cancel()
        await asyncio.gather(*self.service._worker_tasks, return_exceptions=True)

    def cleanup(self) -> None:
        self.repo.execute(
            "MATCH (n) WHERE n.uuid STARTS WITH $tag OR n.group_id = $ns OR n.namespace = $ns "
            "OR n.namespace_key = $ns DETACH DELETE n",
            params={"tag": self.tag, "ns": self.ns},
        )


def _expire_lease(repo: Any, q: str) -> None:
    repo.execute(
        "MATCH (n:Episodic {uuid:$u}) SET n.processing_lease_expires_at = datetime() - duration('PT1H')",
        params={"u": q},
    )


def _force_failed(repo: Any, q: str) -> None:
    repo.execute(
        "MATCH (n:Episodic {uuid:$u}) SET n.processing_state='FAILED', n.processing_error='p0 forced', "
        "n.processing_completed_at = datetime() - duration('PT2H'), n.retry_after = null, "
        "n.processing_owner = null, n.processing_lease_expires_at = null",
        params={"u": q},
    )


def _set_receipt_nonempty() -> None:
    from menhir.infrastructure.graphiti_extraction_policy import get_extraction_receipt

    receipt = get_extraction_receipt()
    _require(receipt is not None, "no live extraction receipt inside add_episode")
    receipt.raw_entity_count = 2
    receipt.raw_edge_count = 1


# ------------------------------------------------------------------ T1
@pytest.mark.online
@p0("T1/B-F1: a twin published before a collapsed attempt stays behind after erase of the unlinked FAILED queue node")
async def test_t1_erase_after_collapsed_attempt_leaves_no_twin(test_neo4j_repo, tmp_path) -> None:
    """Breaks (passes) once erase resolves the twin from a durable pre-publish link or marker instead
    of from ``resolved_episode_uuid``, which a failed ``stamp_and_finalize`` never wrote."""
    rig = Rig(test_neo4j_repo, tmp_path, "t1")
    try:
        q = rig.seed_q()

        async def handler(fork: FakeFork, call: SimpleNamespace) -> Any:
            published = await fork.publish(call)
            _set_receipt_nonempty()  # the model "extracted" content that resolution then dropped
            return SimpleNamespace(episode=published.episode, nodes=[], edges=[], episodic_edges=[])

        rig.fork.handler = handler
        await rig.run_worker(q)
        row = rig.q_row(q)
        _require(rig.fork.calls and rig.fork.calls[0].published, "the fake never committed the twin")
        _require(row and row["state"] in ("FAILED", "PENDING"), f"attempt did not fail back: {row}")
        _require(row["resolved"] is None, f"queue node unexpectedly linked: {row}")
        before = rig.carriers()
        _require(any(c.startswith("Episodic:" + rig.tag + "-twin") for c in before), f"twin not committed: {before}")

        out = await rig.erase(q)

        left = rig.carriers()
        assert not left or out.get("reason") not in SUCCESS_REASONS, (
            f"erase_memory replied reason={out.get('reason')!r} but the published twin survives: {left}"
        )
    finally:
        await rig.shutdown()
        rig.cleanup()


# ------------------------------------------------------------------ T2
@pytest.mark.online
@p0("T2: an attempt-1 twin that predates the attempt-2 claim survives an erase during attempt 2 (no sweep)")
async def test_t2_erase_during_second_attempt_removes_first_attempt_twin(test_neo4j_repo, tmp_path) -> None:
    """Breaks (passes) once erase covers twins of EARLIER attempts (reserved uuids / marker), not only
    the link a finished attempt would write."""
    rig = Rig(test_neo4j_repo, tmp_path, "t2")
    gate = rig.gate("attempt2-pre-publish")
    try:
        q = rig.seed_q()

        async def attempt1(fork: FakeFork, call: SimpleNamespace) -> Any:
            # Twin with NO entity neighbour: attempt 2's reconcile (which needs one) does not adopt it.
            await fork.publish(call, entities=False)
            raise TimeoutError("graphiti add_episode timed out after 1.0s; remote completion status unknown")

        async def attempt2(fork: FakeFork, call: SimpleNamespace) -> Any:
            await gate.park()
            return await fork.publish(call, entities=False)

        rig.fork.handler = attempt1
        await rig.run_worker(q)
        _require(rig.fork.calls and rig.fork.calls[0].published, "attempt 1 did not commit its twin")
        twin1 = rig.fork.calls[0].twin_uuid
        test_neo4j_repo.execute(
            "MATCH (n:Episodic {uuid:$u}) SET n.processing_state = CASE WHEN n.processing_state = 'FAILED' "
            "THEN 'FAILED' ELSE 'PENDING' END, n.retry_after = datetime() - duration('PT1M')",
            params={"u": q},
        )

        rig.fork.handler = attempt2
        rig.start_worker(q)
        await gate.wait_reached()
        row = await rig.wait_enriching(q, attempts=2)
        _require(len(rig.fork.calls) == 2, "attempt 2 never reached add_episode (reconcile adopted the twin?)")
        _require(f"Episodic:{twin1}" in rig.carriers(), "attempt-1 twin missing before the erase")

        out = await rig.erase(q)

        left = rig.carriers()  # worker still parked pre-publish; no sweep has run
        assert not left, (
            f"erase_memory replied reason={out.get('reason')!r} (q attempts={row['attempts']}) but the "
            f"attempt-1 twin survives: {left}"
        )
    finally:
        await rig.shutdown()
        rig.cleanup()


# ------------------------------------------------------------------ T3
@pytest.mark.online
@p0("T3/A-4: a worker whose claim was reaped and whose queue node was then erased still publishes a twin")
async def test_t3_reaped_then_erased_then_late_publish_leaves_no_twin(test_neo4j_repo, tmp_path) -> None:
    """Breaks (passes) once the late publish is refused (or compensated) although the queue node it
    belonged to is already gone."""
    rig = Rig(test_neo4j_repo, tmp_path, "t3")
    gate = rig.gate("pre-publish")
    try:
        rig.service._max_enrichment_attempts = 1
        q = rig.seed_q()

        async def handler(fork: FakeFork, call: SimpleNamespace) -> Any:
            await gate.park()
            return await fork.publish(call)

        rig.fork.handler = handler
        task = rig.start_worker(q)
        await gate.wait_reached()
        await rig.wait_enriching(q, attempts=1)

        _expire_lease(test_neo4j_repo, q)
        reaped = await asyncio.to_thread(rig.adapter.reset_stale_enriching_episodes, max_attempts=1)
        row = rig.q_row(q)
        _require(reaped >= 1 and row and row["state"] == "FAILED", f"reaper did not fail the claim: {reaped} {row}")

        out = await rig.erase(q)
        gate.open()
        await _bounded(task, "worker to finish after release")

        left = rig.carriers()
        assert not left, f"erase replied reason={out.get('reason')!r}; the late publish left: {left}"
    finally:
        await rig.shutdown()
        rig.cleanup()


# ------------------------------------------------------------------ T4a
@pytest.mark.online
@p0("T4a/B-1: a publish whose claim was sealed by an erase between Graphiti's reads and its commit still commits")
async def test_t4a_publish_after_seal_aborts(test_neo4j_repo, tmp_path) -> None:
    """Breaks (passes) once the worker passes a G-publish fence into ``add_episode`` that aborts the
    publish transaction when the claim has been sealed."""
    rig = Rig(test_neo4j_repo, tmp_path, "t4a")
    gate = rig.gate("pre-commit")
    snap: dict[str, list[str]] = {}
    try:
        q = rig.seed_q()

        async def handler(fork: FakeFork, call: SimpleNamespace) -> Any:
            await gate.park()
            try:
                return await fork.publish(call)
            finally:  # the instant after the commit attempt, before the worker resumes
                snap["after"] = rig.carriers()

        rig.fork.handler = handler
        task = rig.start_worker(q)
        await gate.wait_reached()
        await rig.wait_enriching(q, attempts=1)
        _require(rig.carriers() == [f"Episodic:{q}"], f"canary precondition: {rig.carriers()}")

        out = await rig.erase(q)
        gate.open()
        await _bounded(task, "worker to finish")

        _require("after" in snap, "the fake never attempted the publish")
        assert snap["after"] == [], (
            f"erase replied reason={out.get('reason')!r}, yet the sealed claim's publish committed: {snap['after']}"
        )
    finally:
        await rig.shutdown()
        rig.cleanup()


# ------------------------------------------------------------------ T4b
@pytest.mark.online
@p0("T4b: a twin committed by Graphiti but not yet linked is invisible to an erase that lands next")
async def test_t4b_erase_after_publish_commit_sees_the_twin(test_neo4j_repo, tmp_path) -> None:
    """Breaks (passes) once the link (or reservation) is durable inside the publish transaction, so an
    erase that lands after the commit resolves and removes the twin."""
    rig = Rig(test_neo4j_repo, tmp_path, "t4b")
    gate = rig.gate("post-commit")
    try:
        q = rig.seed_q()

        async def handler(fork: FakeFork, call: SimpleNamespace) -> Any:
            published = await fork.publish(call)
            await gate.park()
            return published

        rig.fork.handler = handler
        rig.start_worker(q)
        await gate.wait_reached()
        await rig.wait_enriching(q, attempts=1)
        _require(rig.fork.calls[0].published, "the fake did not commit before parking")
        _require(len(rig.carriers()) >= 3, f"twin/entity/edge not committed: {rig.carriers()}")

        out = await rig.erase(q)

        left = rig.carriers()  # worker still parked after its commit
        assert not left or out.get("reason") not in SUCCESS_REASONS, (
            f"erase replied reason={out.get('reason')!r} but the committed twin survives: {left}"
        )
    finally:
        await rig.shutdown()
        rig.cleanup()


# ------------------------------------------------------------------ T5
@pytest.mark.online
@p0("T5/B-F6: a worker resumed after an erase still dispatches the erased text and publishes it (embedding re-stamp checked, see docstring)")
async def test_t5_worker_resuming_after_erase_writes_nothing_back(test_neo4j_repo, tmp_path) -> None:
    """Breaks (passes) once every post-claim write (``set_episode_content_embedding``, the dispatch to
    Graphiti) is fenced on the claim. NOTE: the plan says "pause after publish"; the pause here is at
    the embed call instead, because on today's code the erase seals the claim and
    ``stamp_and_finalize`` exits on ownership loss before any post-publish write, so the writes that
    can resurrect erased text are the embedding and the extraction dispatch, which come earlier."""
    rig = Rig(test_neo4j_repo, tmp_path, "t5")
    gate = rig.gate("embedding")

    async def hold_embedding() -> None:
        await gate.park()

    try:
        q = rig.seed_q()
        rig.embedder.before_return = hold_embedding
        snaps: dict[str, Any] = {}

        async def handler(fork: FakeFork, call: SimpleNamespace) -> Any:
            row_now = rig.q_row(q)
            snaps["entry"] = {"embedding": bool(row_now and row_now["has_embedding"])}
            published = await fork.publish(call)
            snaps["after_publish"] = rig.carriers()  # before the worker's own compensation runs
            return published

        rig.fork.handler = handler
        task = rig.start_worker(q)
        await gate.wait_reached()
        row = await rig.wait_enriching(q, attempts=1)
        _require(not row["has_embedding"], "embedding already written before the erase")
        _require(f"Episodic:{q}" in rig.carriers(), "canary precondition")

        await rig.erase(q)
        _require(rig.q_row(q) is None or not rig.q_row(q)["has_embedding"], "erase left an embedding")
        gate.open()
        await _bounded(task, "worker to finish after the erase")

        _require("entry" in snaps, "the resumed worker never reached add_episode; nothing to judge")
        violations: list[str] = []
        if snaps["entry"]["embedding"]:
            violations.append("content_embedding re-added on the erased queue node before dispatch")
        violations += [f"text carrier right after publish: {c}" for c in snaps["after_publish"]]
        violations += [f"text carrier at end: {c}" for c in rig.carriers()]
        assert not violations, f"worker resumed after the erase and wrote back: {violations}"
    finally:
        await rig.shutdown()
        rig.cleanup()


# ------------------------------------------------------------------ T11
@pytest.mark.online
@p0("T11/B-F7: a stale FAILED retry row reconciles onto a sealed (deleted_by_user) tombstone and flips it READY")
async def test_t11_stale_retry_row_cannot_resurrect_a_sealed_queue_node(test_neo4j_repo, tmp_path) -> None:
    """Breaks (passes) once ``mark_episode_ready(required_state='FAILED')`` (and the finalize-from-link
    that replaces it) refuses an erased / marked queue node."""
    from menhir.services.scheduler_tasks import retry_process_candidate

    rig = Rig(test_neo4j_repo, tmp_path, "t11")

    async def publish_then_fail(fork: FakeFork, call: SimpleNamespace) -> Any:
        await fork.publish(call)
        raise RuntimeError("p0: graphiti crashed after the publish commit")

    async def failed_row(q: str) -> dict[str, Any]:
        rows = await asyncio.to_thread(rig.adapter.fetch_failed_episode_retry_candidates, limit=500)
        return next((dict(r) for r in rows if r.get("uuid") == q), {})

    try:
        rig.fork.handler = publish_then_fail
        now = datetime.now(timezone.utc)

        # Control: the same scheduler path DOES reconcile an unerased FAILED node, so a "not READY"
        # outcome below cannot be an artefact of the harness.
        # NOTE: both queue nodes live in the DEFAULT namespace because EPISODE_RETRY_FIELDS
        # (cypher.py:487) omits ``namespace`` and retry_process_candidate then assumes "default"
        # (scheduler_tasks.py:277); a named namespace makes record_retention_sources reject the
        # source and the job returns "waiting" (a separate defect, reported).
        control = rig.seed_q(namespace="default")
        await rig.run_worker(control)
        _force_failed(test_neo4j_repo, control)
        crow = await failed_row(control)
        _require(crow, "control row not listed")
        action = await retry_process_candidate(rig.adapter, rig.service, crow, 10, now)
        crow_after = rig.q_row(control)
        _require(
            action == "reconciled" and crow_after and crow_after["state"] == "READY",
            f"control did not reconcile: action={action} row={crow_after}",
        )

        q = rig.seed_q(namespace="default")
        await rig.run_worker(q)
        _force_failed(test_neo4j_repo, q)
        stale = await failed_row(q)  # the scheduler lists q while it is FAILED
        _require(stale, "q not listed as a retry candidate")
        claimed = await asyncio.to_thread(
            rig.adapter.claim_pending_episode, q, max_attempts=10, worker_id="p0-parked", lease_seconds=900
        )
        _require(claimed is not None, "could not claim q")
        await rig.erase(q)
        sealed = rig.q_row(q)
        _require(
            sealed is None or (sealed["state"] == "FAILED" and sealed["substage"] == "deleted_by_user"),
            f"erase did not seal q: {sealed}",
        )

        action = await retry_process_candidate(rig.adapter, rig.service, stale, 10, now)

        after = rig.q_row(q)
        assert after is None or (after["state"] != "READY" and after["resolved"] is None), (
            f"stale retry row (action={action!r}) resurrected the sealed queue node: {after}"
        )
    finally:
        await rig.shutdown()
        rig.cleanup()


# ------------------------------------------------------------------ T12
@pytest.mark.online
@p0("T12/bypass 1: reconcile in namespace Y adopts (links and re-stamps) a same-name twin of namespace X")
async def test_t12_reconcile_does_not_cross_namespaces(test_neo4j_repo, tmp_path) -> None:
    """Breaks (passes) once ``find_completed_episode_artifact`` is scoped to the claimant's namespace."""
    from menhir.domain.namespace import namespace_to_group_id

    rig = Rig(test_neo4j_repo, tmp_path, "t12")
    ns_x, ns_y = f"{rig.tag}-x", f"{rig.tag}-y"

    def seed_twin(twin: str, name: str, group_id: str) -> None:
        test_neo4j_repo.execute(
            """
            CREATE (t:Episodic {uuid:$t, group_id:$g, name:$n, content:'prior twin text',
                                valid_at:datetime(), created_at:datetime()})
            CREATE (e:Entity {uuid:$e, name:$en, group_id:$g, summary:'prior', created_at:datetime()})
            CREATE (t)-[:MENTIONS {uuid:$m, group_id:$g, created_at:datetime()}]->(e)
            """,
            params={"t": twin, "g": group_id, "n": name, "e": f"{twin}-e", "en": f"{twin}-ent", "m": f"{twin}-m"},
        )

    async def never_publish(_fork: FakeFork, _call: SimpleNamespace) -> Any:
        raise HarnessError("reconcile should have handled this claim without add_episode")

    try:
        rig.fork.handler = never_publish

        # Control: same namespace, same name -> the worker adopts the twin (the machinery works here).
        ctl_name, ctl_twin = f"{rig.tag}-ctl-name", f"{rig.tag}-ctl-twin"
        seed_twin(ctl_twin, ctl_name, namespace_to_group_id(ns_x))
        qc = rig.seed_q(namespace=ns_x, name=ctl_name)
        await rig.run_worker(qc)
        crow = rig.q_row(qc)
        _require(crow and crow["state"] == "READY" and crow["resolved"] == ctl_twin, f"control not adopted: {crow}")

        # The counterexample: X owns the only same-name twin; the claimant lives in Y.
        name, twin_x = f"{rig.tag}-shared-name", f"{rig.tag}-x-twin"
        seed_twin(twin_x, name, namespace_to_group_id(ns_x))
        qy = rig.seed_q(namespace=ns_y, name=name)
        await rig.run_worker(qy)

        row = rig.q_row(qy)
        stamped = test_neo4j_repo.execute(
            "MATCH (n) WHERE n.uuid IN [$t, $e] AND n.namespace IS NOT NULL RETURN n.uuid AS uuid, n.namespace AS ns",
            params={"t": twin_x, "e": f"{twin_x}-e"},
        )
        assert (row is None or row["resolved"] != twin_x) and not [r for r in stamped if r["ns"] != ns_x], (
            f"namespace {ns_y!r} reconciled onto namespace {ns_x!r}'s twin: q={row}, "
            f"foreign nodes re-stamped: {[dict(r) for r in stamped]}"
        )
    finally:
        await rig.shutdown()
        rig.cleanup()


# ------------------------------------------------------------------ T16
@pytest.mark.online
@p0("T16: an erase that meets a claim without publish_protocol=2 replies success instead of erased_incomplete{unfenced_inflight}")
async def test_t16_unfenced_inflight_claim_is_reported_incomplete(test_neo4j_repo, tmp_path) -> None:
    """Breaks (passes) once the erase reply distinguishes a sealed ENRICHING claim that cannot be
    fenced. Invented reply key: ``incomplete_reasons`` (falls back to ``reasons``); the plan names the
    class ``erased_incomplete{unfenced_inflight}`` without fixing the field name."""
    rig = Rig(test_neo4j_repo, tmp_path, "t16")
    gate = rig.gate("pre-publish")
    try:
        q = rig.seed_q()

        async def handler(fork: FakeFork, call: SimpleNamespace) -> Any:
            await gate.park()
            return await fork.publish(call)

        rig.fork.handler = handler
        rig.start_worker(q)
        await gate.wait_reached()
        row = await rig.wait_enriching(q, attempts=1)
        _require(row["publish_protocol"] is None, f"claim already carries publish_protocol: {row}")

        out = await rig.erase(q)

        reasons = out.get("incomplete_reasons") or out.get("reasons") or []
        assert out.get("reason") == "erased_incomplete" and "unfenced_inflight" in reasons, (
            f"erase of an unfenceable in-flight claim replied {out!r}"
        )
    finally:
        await rig.shutdown()
        rig.cleanup()


# ------------------------------------------------------------------ T18
class _InterleavingAdapter:
    """Delegates to the real adapter; runs ``hook`` once, just before the first graph delete (after the
    coordinator resolved its targets), the window in which a worker finishes."""

    def __init__(self, inner: Any, hook: Callable[[], None]) -> None:
        self._inner = inner
        self._hook = hook
        self._fired = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def delete_memory_detailed(self, *args: Any, **kwargs: Any) -> Any:
        if not self._fired:
            self._fired = True
            self._hook()
        return self._inner.delete_memory_detailed(*args, **kwargs)


@pytest.mark.online  # NOT xfail: PASSES on e574abdf, see docstring. Regression guard for the rebuild.
async def test_t18_late_twin_does_not_survive_and_reply_is_honest(test_neo4j_repo, tmp_path) -> None:
    """Breaks (passes) once the cascade/follow-up removes a twin linked inside the window and the reply
    is a success class only if nothing holds the phrase. Ported from B-1 of test_erasure_inflight_race
    with the REAL worker finishing inside the window, and WITHOUT running the compensation sweep.

    PASSES TODAY (verified: reply reason 'erased', late_twin_erased 'erased', no carrier left): the
    follow-up pass (erasure_coordinator.py:158-170) plus the worker's own _compensate_if_erased remove
    the twin, so no honest failing variant exists at this window. It is kept as a guard that the
    rebuild (which deletes the follow-up) does not regress it. Not marked xfail on purpose."""
    rig = Rig(test_neo4j_repo, tmp_path, "t18")
    gate = rig.gate("pre-publish")
    loop = asyncio.get_running_loop()
    try:
        q = rig.seed_q()

        async def handler(fork: FakeFork, call: SimpleNamespace) -> Any:
            await gate.park()
            return await fork.publish(call)

        rig.fork.handler = handler
        task = rig.start_worker(q)
        await gate.wait_reached()
        await rig.wait_enriching(q, attempts=1)

        async def worker_finishes() -> None:
            gate.open()
            await _bounded(task, "worker to finish inside the erase window")

        def hook() -> None:
            asyncio.run_coroutine_threadsafe(worker_finishes(), loop).result(60)

        rig.coord.graph_adapter = _InterleavingAdapter(rig.adapter, hook)
        out = await rig.erase(q)

        _require(rig.fork.calls and rig.fork.calls[0].published, "the worker never published inside the window")
        left = rig.carriers()
        assert not left or out.get("reason") not in SUCCESS_REASONS, (
            f"reply reason={out.get('reason')!r} (late_twin_erased={out.get('late_twin_erased')!r}) "
            f"while the phrase is still held by {left}"
        )
        assert not left, f"late twin survives the erase: {left} (reply {out.get('reason')!r})"
    finally:
        await rig.shutdown()
        rig.cleanup()


# ------------------------------------------------------------------ T19
@pytest.mark.online
@p0("T19: a runtime that does not hold the scheduler lease still claims and publishes")
async def test_t19_only_the_scheduler_lease_holder_claims(test_neo4j_repo, tmp_path) -> None:
    """Breaks (passes) once ``_ensure_enrichment_worker`` / lease recovery run only in the process that
    holds the scheduler lease. Uses only existing entry points (``_ensure_enrichment_worker``,
    ``_enqueue_pending_episode``, ``SchedulerLeaseStore``); no lease-gating symbol is invented. The
    takeover half of the plan's T19 (claims handed to the next holder without a double publish) is NOT
    covered: the handover API does not exist yet."""
    from menhir.services.scheduler_lease import SchedulerLeaseStore

    rig = Rig(test_neo4j_repo, tmp_path, "t19")
    store = SchedulerLeaseStore()
    owner = f"{rig.tag}-runtime-a"
    try:
        _require(
            store.try_acquire(lease_name="maintenance_scheduler", owner_id=owner, owner_pid=os.getpid(), lease_duration_s=300),
            "could not take the scheduler lease for the holder runtime",
        )
        q = rig.seed_q()

        # Runtime B: a second service on the same store that does NOT hold the lease.
        await rig.service._ensure_enrichment_worker()
        await rig.service._enqueue_pending_episode(q)
        for _ in range(60):  # up to 3s for a claim to appear
            row = rig.q_row(q)
            if rig.fork.calls or (row and row["state"] != "PENDING"):
                break
            await asyncio.sleep(0.05)

        row = rig.q_row(q)
        assert not rig.fork.calls and row and row["state"] == "PENDING" and not row["attempts"], (
            f"non-holder runtime claimed/published: row={row} add_episode calls={len(rig.fork.calls)}"
        )
    finally:
        store.release(lease_name="maintenance_scheduler", owner_id=owner)
        await rig.shutdown()
        rig.cleanup()


# ------------------------------------------------------------------ T25
@pytest.mark.online
@p0("T25/Adv F5: an erase in the namespace does not fence an in-flight publish of another memory (it completes READY)")
async def test_t25_erase_fences_inflight_publish_without_burning_attempts(test_neo4j_repo, tmp_path) -> None:
    """Breaks (passes) once an erase bumps the namespace erasure epoch, the in-flight publish of the
    other memory aborts with ``PublishFenced``, and the requeue undoes the claim's attempt increment."""
    rig = Rig(test_neo4j_repo, tmp_path, "t25")
    gate = rig.gate("m2-pre-publish")
    canary1 = f"{rig.canary}-M1"
    try:
        m1 = rig.seed_q(canary=canary1)
        m2 = rig.seed_q()
        before = rig.q_row(m2)
        _require(before and before["attempts"] in (None, 0), f"M2 already has attempts: {before}")

        async def handler(fork: FakeFork, call: SimpleNamespace) -> Any:
            await gate.park()
            return await fork.publish(call)

        rig.fork.handler = handler
        task = rig.start_worker(m2)
        await gate.wait_reached()
        await rig.wait_enriching(m2, attempts=1)

        out = await rig.erase(m1)
        _require(out.get("reason") in SUCCESS_REASONS | {"erased_pending_physical"}, f"M1 erase failed: {out}")
        gate.open()
        await _bounded(task, "M2 worker to finish")

        after = rig.q_row(m2)
        assert after and after["state"] == "PENDING" and not after["attempts"], (
            f"M2's in-flight publish was not fenced by the erase in its namespace: {after}"
        )
    finally:
        await rig.shutdown()
        rig.cleanup()


# ------------------------------------------------------------------ T26
@pytest.mark.online
@p0("T26: a publish into the default namespace (group_id '') is not fenced by an erase of a default-namespace memory")
async def test_t26_default_namespace_publish_is_fenced_under_default_key(test_neo4j_repo, tmp_path) -> None:
    """Breaks (passes) once G-publish locks the fence keyed ``'default'`` for the physical group_id
    ``''`` (the normalisation the cascade does at memory_queries.py:1351-1355). The default namespace
    cannot be made unique, so isolation rests on the unique canary and uuid prefix."""
    rig = Rig(test_neo4j_repo, tmp_path, "t26")
    gate = rig.gate("pre-commit")
    snap: dict[str, list[str]] = {}
    try:
        q = rig.seed_q(namespace="default")

        async def handler(fork: FakeFork, call: SimpleNamespace) -> Any:
            _require(call.group_id == "", f"default namespace did not map to group_id '': {call.group_id!r}")
            await gate.park()
            try:
                return await fork.publish(call)
            finally:
                snap["after"] = rig.carriers()

        rig.fork.handler = handler
        task = rig.start_worker(q)
        await gate.wait_reached()
        await rig.wait_enriching(q, attempts=1)
        _require(rig.carriers() == [f"Episodic:{q}"], f"canary precondition: {rig.carriers()}")

        out = await rig.erase(q)
        gate.open()
        await _bounded(task, "worker to finish")

        _require("after" in snap, "the fake never attempted the publish")
        assert snap["after"] == [], (
            f"erase replied reason={out.get('reason')!r}; the default-namespace publish was not fenced: {snap['after']}"
        )
        fence = test_neo4j_repo.execute(
            "MATCH (f:EvidenceNamespaceFence {namespace_key:'default'}) RETURN f.erasure_epoch AS epoch"
        )
        assert fence and (fence[0]["epoch"] or 0) >= 1, f"no erasure epoch on fence 'default': {fence}"
    finally:
        await rig.shutdown()
        rig.cleanup()
