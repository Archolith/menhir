"""P4 worker fence: the enrichment worker publishes through the claim's fence (online).

Plan: menhir-erasure-rebuild-plan-v2 sections 2.3 and 3.2; P4 design note, "Fence side".

The real ``IngestService._process_pending_episode`` runs against a real Neo4j. The Graphiti fork is
replaced by ``FakeFork`` under the REAL ``GraphitiClient`` wrapper; it honours ``new_episode_uuid``
and ``publish_fence`` (before_write/after_write inside one write transaction). The Rig/FakeFork parts
are copied from tests/test_erasure_rebuild_p0_races_online.py (not imported). No LLM, no paid calls.

Scenario setup problems raise ``HarnessError`` (never an AssertionError); only the plain ``assert``
decides a test.
"""

from __future__ import annotations

import asyncio
import uuid as uuidlib
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

import pytest

_WAIT_S = 25.0


class HarnessError(RuntimeError):
    """The scenario could not be set up."""


def _require(condition: object, message: str) -> None:
    if not condition:
        raise HarnessError(message)


async def _bounded(
    awaitable: Awaitable[Any], what: str, timeout_s: float = _WAIT_S
) -> Any:
    try:
        return await asyncio.wait_for(awaitable, timeout_s)
    except asyncio.TimeoutError as exc:
        raise HarnessError(f"timed out waiting for {what}") from exc


class Gate:
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
    model = "fake-embed-model"

    async def create(self, _input: Any) -> list[float]:
        return [0.25, 0.5, 0.75]


class FakeFork:
    def __init__(self, repo: Any, tag: str) -> None:
        self.repo = repo
        self.tag = tag
        self.calls: list[SimpleNamespace] = []
        self.handler: Callable[[FakeFork, SimpleNamespace], Awaitable[Any]] | None = (
            None
        )

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

    async def publish(self, call: SimpleNamespace) -> Any:
        """One real write transaction: twin + entity + fact edge, fence hooks inside it."""
        fence = call.kw.get("publish_fence")
        loop = asyncio.get_running_loop()
        t, g = call.twin_uuid, call.group_id
        e1, e2, edge, mention = (f"{t}-e1", f"{t}-e2", f"{t}-edge", f"{t}-mention")

        def work(tx: Any) -> None:
            if fence is not None:
                asyncio.run_coroutine_threadsafe(
                    fence.before_write(tx, episode_uuid=t, previous_episode_uuids=[]),
                    loop,
                ).result(30)
            tx.execute(
                "CREATE (:Episodic {uuid:$tu, group_id:$g, name:$nm, content:$body,"
                " valid_at:datetime(), created_at:datetime()})",
                {"tu": t, "g": g, "nm": call.name, "body": call.body},
            )
            tx.execute(
                """
                MATCH (t:Episodic {uuid:$tu})
                CREATE (a:Entity {uuid:$e1, name:$n1, group_id:$g, summary:'probe', created_at:datetime()})
                CREATE (b:Entity {uuid:$e2, name:$n2, group_id:$g, summary:'a colour', created_at:datetime()})
                CREATE (t)-[:MENTIONS {uuid:$mu, group_id:$g, created_at:datetime()}]->(a)
                CREATE (a)-[:RELATES_TO {uuid:$ru, name:'HAS_COLOUR', fact:'probe is a colour',
                                         episodes:[$tu], group_id:$g, created_at:datetime()}]->(b)
                """,
                {
                    "tu": t,
                    "g": g,
                    "e1": e1,
                    "e2": e2,
                    "n1": f"{t}-ent1",
                    "n2": f"{t}-ent2",
                    "mu": mention,
                    "ru": edge,
                },
            )
            if fence is not None:
                asyncio.run_coroutine_threadsafe(
                    fence.after_write(
                        tx,
                        episode_uuid=t,
                        entity_node_uuids=[e1, e2],
                        entity_edge_uuids=[edge],
                        previous_episode_uuids=[],
                    ),
                    loop,
                ).result(30)

        await asyncio.to_thread(self.repo.execute_write, work)
        call.published = True
        return SimpleNamespace(
            episode=SimpleNamespace(uuid=t),
            nodes=[
                SimpleNamespace(uuid=e1, name="", content=""),
                SimpleNamespace(uuid=e2, name="", content=""),
            ],
            edges=[SimpleNamespace(uuid=edge)],
            episodic_edges=[],
        )


class Rig:
    def __init__(self, repo: Any, tmp_path: Any, tag: str) -> None:
        from menhir.infrastructure import GraphitiClient
        from menhir.infrastructure.erasure_subjects import ErasureSubjectStore
        from menhir.infrastructure.graph_operations import GraphOperationsJournal
        from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
        from menhir.infrastructure.telemetry.store import McpTelemetryStore
        from menhir.services.erasure_coordinator import ErasureCoordinator
        from menhir.services.ingest_service import IngestService

        self.repo = repo
        self.tag = f"{tag}-{uuidlib.uuid4().hex[:6]}"
        self.ns = f"{self.tag}-ns"
        self.adapter = MemoryGraphAdapter(neo4j=repo)
        db = tmp_path / "p4-worker-fence.db"
        McpTelemetryStore(db_path=db)._ensure_ready()
        self.coord = ErasureCoordinator(
            graph_adapter=self.adapter,
            journal=GraphOperationsJournal(db_path=db),
            subjects=ErasureSubjectStore(db_path=db),
        )
        self.fork = FakeFork(repo, self.tag)
        client = GraphitiClient(client=self.fork, embedder_ref=FakeEmbedder())
        self.service = IngestService(
            graphiti_client=client, graph_adapter=self.adapter, llm=None
        )
        self.service._processing_heartbeat_interval_s = 3600.0
        self.tasks: list[asyncio.Task[Any]] = []
        self.gates: list[Gate] = []
        self.n_seeded = 0

    def gate(self, name: str) -> Gate:
        g = Gate(name)
        self.gates.append(g)
        return g

    def seed_q(self) -> str:
        self.n_seeded += 1
        q = f"{self.tag}-q{self.n_seeded}"
        self.adapter.create_pending_episode(
            episode_uuid=q,
            name=f"anchor-{q}",
            content="The probe's favourite colour is teal",
            session_id=f"{self.tag}-session",
            user_id="p4-user",
            source="claude-code",
            source_confidence=0.5,
            namespace=self.ns,
        )
        return q

    def q_row(self, q: str) -> dict[str, Any] | None:
        rows = self.repo.execute(
            """
            MATCH (n:Episodic {uuid:$u})
            RETURN n.processing_state AS state, n.processing_attempts AS attempts,
                   n.resolved_episode_uuid AS resolved, n.processing_error AS error,
                   n.processing_owner AS owner, n.reserved_twin_uuids AS reserved
            """,
            params={"u": q},
        )
        return dict(rows[0]) if rows else None

    def twins(self) -> list[str]:
        rows = self.repo.execute(
            "MATCH (n:Episodic) WHERE n.uuid STARTS WITH $p AND n.group_id = $g RETURN n.uuid AS uuid",
            params={"p": f"{self.tag}", "g": self.ns},
        )
        return sorted(r["uuid"] for r in rows)

    def start_worker(self, q: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(self.service._process_pending_episode(q))
        self.tasks.append(task)
        return task

    async def run_worker(self, q: str) -> None:
        await _bounded(self.start_worker(q), f"worker for {q} to finish")

    async def wait_enriching(self, q: str) -> dict[str, Any]:
        for _ in range(int(_WAIT_S * 20)):
            row = self.q_row(q)
            if row and row["state"] == "ENRICHING":
                return row
            await asyncio.sleep(0.05)
        raise HarnessError(f"{q} never reached ENRICHING: {self.q_row(q)}")

    async def erase(self, q: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.coord.erase_memory, q)

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


# ------------------------------------------------------------------ (a) happy path
@pytest.mark.online
async def test_worker_publishes_with_the_reserved_uuid_and_links(
    test_neo4j_repo, tmp_path
) -> None:
    rig = Rig(test_neo4j_repo, tmp_path, "p4a")
    try:
        q = rig.seed_q()
        await rig.run_worker(q)

        _require(
            len(rig.fork.calls) == 1,
            f"fake not called exactly once: {len(rig.fork.calls)}",
        )
        call = rig.fork.calls[0]
        row = rig.q_row(q)
        assert call.kw.get("new_episode_uuid") == call.twin_uuid
        assert call.kw.get("publish_fence") is not None
        assert call.twin_uuid in (row["reserved"] or [])
        assert row["state"] == "READY", row
        assert row["resolved"] == call.twin_uuid
        twin = test_neo4j_repo.execute(
            "MATCH (g:Episodic {uuid:$u}) RETURN g.menhir_memory_uuid AS m",
            params={"u": call.twin_uuid},
        )
        assert twin and twin[0]["m"] == q
    finally:
        await rig.shutdown()
        rig.cleanup()


# ------------------------------------------------------------------ (b) erase before publish
@pytest.mark.online
async def test_erase_of_another_memory_before_publish_requeues_without_burning_an_attempt(
    test_neo4j_repo, tmp_path
) -> None:
    rig = Rig(test_neo4j_repo, tmp_path, "p4b")
    gate = rig.gate("pre-publish")
    try:
        q, other = rig.seed_q(), rig.seed_q()

        async def handler(fork: FakeFork, call: SimpleNamespace) -> Any:
            await gate.park()
            return await fork.publish(call)

        rig.fork.handler = handler
        task = rig.start_worker(q)
        await gate.wait_reached()
        await rig.wait_enriching(q)

        out = await rig.erase(
            other
        )  # moves the namespace erasure epoch inside the window
        _require(
            out.get("reason") in {"erased", "graph_already_absent", "nothing_to_erase"},
            f"erase: {out}",
        )
        gate.open()
        await _bounded(task, "worker to stop")

        row = rig.q_row(q)
        assert rig.twins() == [], "the vetoed publish committed a twin"
        assert row["state"] == "PENDING", row
        assert row["attempts"] == 0, row
        assert row["error"] is None, row
        assert row["resolved"] is None, row
    finally:
        await rig.shutdown()
        rig.cleanup()


@pytest.mark.online
async def test_erase_of_the_queue_node_before_publish_stops_silently(
    test_neo4j_repo, tmp_path
) -> None:
    rig = Rig(test_neo4j_repo, tmp_path, "p4b2")
    gate = rig.gate("pre-publish")
    try:
        q = rig.seed_q()

        async def handler(fork: FakeFork, call: SimpleNamespace) -> Any:
            await gate.park()
            return await fork.publish(call)

        rig.fork.handler = handler
        task = rig.start_worker(q)
        await gate.wait_reached()
        await rig.wait_enriching(q)

        await rig.erase(q)
        gate.open()
        await _bounded(task, "worker to stop")

        row = rig.q_row(q)
        assert rig.twins() == [], "the vetoed publish committed a twin"
        # The erase itself leaves the q tombstoned FAILED/deleted_by_user; the worker must add nothing.
        assert row is None or row["error"] == "deleted_by_user", row
        assert row is None or row["resolved"] is None, row
        assert row is None or row["state"] != "READY", row
        assert rig.service._failed_enrichments == 0
    finally:
        await rig.shutdown()
        rig.cleanup()


# ------------------------------------------------------------------ (c) existing link
@pytest.mark.online
async def test_claim_with_an_existing_link_finalizes_without_add_episode(
    test_neo4j_repo, tmp_path
) -> None:
    rig = Rig(test_neo4j_repo, tmp_path, "p4c")
    try:
        q = rig.seed_q()
        twin = f"{rig.tag}-twin-linked"
        test_neo4j_repo.execute(
            """
            MATCH (n:Episodic {uuid:$q})
            CREATE (g:Episodic {uuid:$t, group_id:$ns, name:'twin', content:'x', created_at:datetime()})
            CREATE (e:Entity {uuid:$e, name:'ent', group_id:$ns, summary:'s', created_at:datetime()})
            CREATE (g)-[:MENTIONS {uuid:$m, group_id:$ns}]->(e)
            SET n.resolved_episode_uuid = $t, g.menhir_memory_uuid = n.uuid
            """,
            params={
                "q": q,
                "t": twin,
                "e": f"{twin}-e",
                "m": f"{twin}-m",
                "ns": rig.ns,
            },
        )

        async def boom(fork: FakeFork, call: SimpleNamespace) -> Any:
            raise AssertionError("add_episode must not run for a linked claim")

        rig.fork.handler = boom
        await rig.run_worker(q)

        row = rig.q_row(q)
        assert rig.fork.calls == []
        assert row["state"] == "READY", row
        assert row["resolved"] == twin
    finally:
        await rig.shutdown()
        rig.cleanup()


# ------------------------------------------------------------------ (d) FenceLost after the publish
@pytest.mark.online
async def test_fence_lost_in_a_post_publish_step_requeues_and_never_fails(
    test_neo4j_repo, tmp_path
) -> None:
    from menhir.infrastructure.erasure_fence import fence_key_cypher

    rig = Rig(test_neo4j_repo, tmp_path, "p4d")
    try:
        q = rig.seed_q()

        async def handler(fork: FakeFork, call: SimpleNamespace) -> Any:
            result = await fork.publish(call)
            # The namespace erasure epoch moves after the twin committed: the next fenced
            # post-publish step must lose the fence.
            test_neo4j_repo.execute(
                f"MATCH (n:Episodic {{uuid:$q}}) WITH {fence_key_cypher('n')} AS k "
                "MERGE (f:EvidenceNamespaceFence {namespace_key: k}) "
                "SET f.erasure_epoch = coalesce(f.erasure_epoch, 0) + 1",
                params={"q": q},
            )
            return result

        rig.fork.handler = handler
        await rig.run_worker(q)

        row = rig.q_row(q)
        _require(
            rig.fork.calls and rig.fork.calls[0].published,
            "the fake never committed the twin",
        )
        assert row["state"] == "PENDING", row
        assert row["attempts"] == 0, row
        assert row["error"] is None, row
        assert row["resolved"] == rig.fork.calls[0].twin_uuid, row
        assert rig.service._failed_enrichments == 0
    finally:
        await rig.shutdown()
        rig.cleanup()


# ------------------------------------------------------------------ (e) non-holder
@pytest.mark.online
async def test_a_process_that_is_not_the_lease_holder_never_claims(
    test_neo4j_repo, tmp_path
) -> None:
    rig = Rig(test_neo4j_repo, tmp_path, "p4e")
    try:
        q = rig.seed_q()
        rig.service._enrichment_allowed = lambda: False  # type: ignore[method-assign]

        await rig.run_worker(q)

        row = rig.q_row(q)
        assert rig.fork.calls == []
        assert row["state"] == "PENDING", row
        assert not row["attempts"], row
        assert row["owner"] is None, row
    finally:
        await rig.shutdown()
        rig.cleanup()
