"""Online tests for the Menhir publish fence (G-publish, G-fenced).

Plan: menhir-erasure-rebuild-plan-v2 sections 2.2 and 3.2; P4 design note, "Fence side".

The hooks run inside ``repo.execute_write`` on Menhir's sync ``Neo4jTransaction`` (the same way the
P0 race tests' ``FakeFork`` drives them). The last test drives the REAL fork bulk writer over an
async graphiti ``Neo4jDriver`` and skips when the installed ``graphiti_core`` has no
``publish_fence`` module.
"""

from __future__ import annotations

import asyncio
import os
import uuid as uuidlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from menhir.infrastructure.erasure_fence import fence_key_cypher
from menhir.infrastructure.publish_fence import (
    ClaimFence,
    FenceLost,
    GateFence,
    MenhirPublishFence,
    PublishFenced,
    _failed_check,
    _tx_run,
    check_fence,
    read_gate_fence,
    requeue_fenced,
    run_fenced,
)

WORKER = "fence-worker"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ------------------------------------------------------------------ harness
@dataclass
class Seed:
    tag: str
    ns: str
    q: str
    twin: str
    previous: str
    turn: str
    started: datetime

    def claim(self, **overrides: Any) -> ClaimFence:
        fields: dict[str, Any] = {
            "q_uuid": self.q,
            "worker_id": WORKER,
            "claim_started_at": self.started,
            "namespace_key": self.ns,
            "reserved_twin_uuid": self.twin,
        }
        fields.update(overrides)
        return ClaimFence(**fields)


@pytest.fixture
def seed(test_neo4j_repo):
    tag = f"pf-{uuidlib.uuid4().hex[:8]}"
    s = Seed(
        tag=tag,
        ns=f"{tag}-ns",
        q=f"{tag}-q",
        twin=f"{tag}-twin",
        previous=f"{tag}-prev",
        turn=f"{tag}-turn",
        started=datetime.now(timezone.utc),
    )
    _seed_graph(test_neo4j_repo, s)
    try:
        yield s
    finally:
        test_neo4j_repo.execute(
            "MATCH (n) WHERE n.uuid STARTS WITH $tag OR n.turn_id STARTS WITH $tag "
            "OR n.namespace_key = $ns OR n.group_id = $ns OR n.op_id STARTS WITH $tag "
            "DETACH DELETE n",
            params={"tag": s.tag, "ns": s.ns},
        )


def _seed_graph(repo: Any, s: Seed) -> None:
    repo.execute(
        """
        MERGE (f:EvidenceNamespaceFence {namespace_key: $ns})
          ON CREATE SET f.generation = 0, f.created_at = datetime()
        SET f.erasure_epoch = 0, f.erasing_ops = []
        CREATE (q:Episodic {uuid: $q, group_id: $ns, namespace: $ns,
            processing_state: 'ENRICHING', processing_owner: $worker,
            processing_started_at: $started, processing_attempts: 2,
            reserved_twin_uuids: [$twin], publish_protocol: 2})
        CREATE (:Episodic {uuid: $prev, group_id: $ns, name: 'previous'})
        CREATE (:TurnEvidence {turn_id: $turn, namespace: $ns})
        """,
        params={
            "ns": s.ns,
            "q": s.q,
            "worker": WORKER,
            "started": s.started,
            "twin": s.twin,
            "prev": s.previous,
            "turn": s.turn,
        },
    )


def _drive(
    repo: Any,
    fence: MenhirPublishFence,
    s: Seed,
    *,
    previous: tuple[str, ...] = (),
    create_twin: bool = True,
    skip_before: bool = False,
    then_raise: bool = False,
) -> None:
    """before_write, the twin write, after_write: all in one ``execute_write`` transaction."""

    async def hooks(tx: Any) -> None:
        if not skip_before:
            await fence.before_write(
                tx, episode_uuid=s.twin, previous_episode_uuids=list(previous)
            )
        if create_twin:
            await _tx_run(
                tx,
                "MERGE (g:Episodic {uuid: $t}) SET g.group_id = $ns, g.name = 'twin'",
                {"t": s.twin, "ns": s.ns},
            )
        await fence.after_write(
            tx,
            episode_uuid=s.twin,
            entity_node_uuids=[],
            entity_edge_uuids=["edge-1"],
            previous_episode_uuids=list(previous),
        )
        if then_raise:
            raise RuntimeError("late failure after the hooks")

    def work(tx: Any) -> None:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(hooks(tx))
        finally:
            loop.close()

    repo.execute_write(work)


def _fence(s: Seed, gate_epoch: int = 0, **kw: Any) -> MenhirPublishFence:
    return MenhirPublishFence(s.claim(), GateFence(epoch=gate_epoch), **kw)


def _one(repo: Any, query: str, **params: Any) -> Any:
    rows = repo.execute(query, params=params)
    return rows[0] if rows else None


def _twin_count(repo: Any, s: Seed) -> int:
    return _one(repo, "MATCH (g:Episodic {uuid: $t}) RETURN count(g) AS c", t=s.twin)[
        "c"
    ]


def _q_row(repo: Any, s: Seed) -> dict[str, Any] | None:
    row = _one(
        repo,
        "MATCH (q:Episodic {uuid: $q}) RETURN q.resolved_episode_uuid AS resolved, "
        "q.lock_nonce AS nonce, q.processing_state AS state, q.processing_attempts AS attempts, "
        "q.processing_owner AS owner, q.retry_after AS retry_after, q.extra_ran AS extra_ran",
        q=s.q,
    )
    return dict(row) if row else None


def _read_context(repo: Any, s: Seed) -> list[tuple[str, str, bool]]:
    rows = repo.execute(
        "MATCH (:Episodic {uuid: $t})-[r:READ_CONTEXT]->(x) "
        "RETURN r.kind AS kind, coalesce(x.uuid, x.turn_id) AS target, r.inferred AS inferred "
        "ORDER BY kind, target",
        params={"t": s.twin},
    )
    return [(r["kind"], r["target"], r["inferred"]) for r in rows]


# ------------------------------------------------------------------ offline units
class _AsyncResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def data(self) -> list[dict[str, Any]]:
        return self._rows


class _AsyncTx:
    """The fork's shape: ``await tx.run(query, **params)`` then ``await result.data()``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run(self, query: str, **params: Any) -> _AsyncResult:
        self.calls.append((query, params))
        return _AsyncResult([{"ok": 1}])


class _SyncTx:
    def execute(
        self, query: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return [{"echo": (query, params)}]


async def test_tx_run_handles_both_transaction_flavours() -> None:
    tx = _AsyncTx()
    assert await _tx_run(tx, "RETURN 1", {"a": 1}) == [{"ok": 1}]
    assert tx.calls == [("RETURN 1", {"a": 1})]
    assert await _tx_run(_SyncTx(), "RETURN 2", {"b": 2}) == [
        {"echo": ("RETURN 2", {"b": 2})}
    ]


def test_failed_check_names_the_first_failed_check() -> None:
    claim = ClaimFence("q", "w", NOW, "ns", "twin")
    ok = {
        "q_found": True,
        "state": "ENRICHING",
        "owner": "w",
        "instant_ok": True,
        "no_marker": True,
        "epoch": 3,
        "erasing_ops": 0,
        "resolved": None,
        "reserved": ["twin"],
    }
    assert (
        _failed_check(ok, claim, GateFence(3), episode_uuid="twin", publish=True)
        is None
    )
    assert _failed_check({**ok, "epoch": 4}, claim, GateFence(3)) == "epoch_moved"
    assert (
        _failed_check({**ok, "epoch": 4}, claim, None) is None
    )  # no gate, no epoch check
    assert _failed_check({**ok, "reserved": []}, claim, None) is None  # not a publish
    assert (
        _failed_check(
            {**ok, "reserved": []}, claim, None, episode_uuid="twin", publish=True
        )
        == "uuid_not_reserved"
    )
    assert _failed_check(None, claim, None) == "queue_node_missing"


class _NativeWithoutFence:
    async def add_episode(
        self, *, name, episode_body, source_description, reference_time, group_id
    ):
        raise AssertionError("must not be called")


class _NativeWithFence:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def add_episode(
        self,
        *,
        name,
        episode_body,
        source_description,
        reference_time,
        group_id,
        new_episode_uuid=None,
        publish_fence=None,
    ):
        self.kwargs = {
            "new_episode_uuid": new_episode_uuid,
            "publish_fence": publish_fence,
        }
        return type("R", (), {"episode": None, "edges": []})()


async def _call_client(native: Any, **kw: Any) -> Any:
    from menhir.infrastructure.graphiti_client import GraphitiClient

    client = GraphitiClient(client=native)
    return await client.add_episode(
        name="n",
        episode_body="b",
        source_description="s",
        reference_time=NOW,
        group_id="g",
        **kw,
    )


async def test_client_fails_closed_when_native_cannot_take_a_fence() -> None:
    fence = MenhirPublishFence(ClaimFence("q", "w", NOW, "ns", "twin"), GateFence(0))
    with pytest.raises(RuntimeError, match="publish_fence"):
        await _call_client(
            _NativeWithoutFence(), new_episode_uuid="twin", publish_fence=fence
        )
    # Without a fence the uuid is simply not forwarded to a native client that lacks the parameter.
    native = _NativeWithoutFence()

    async def ok(**_kw: Any) -> Any:
        return None

    native.add_episode = ok  # type: ignore[method-assign]
    await _call_client(native, new_episode_uuid="twin")


async def test_client_forwards_uuid_and_a_fence_carrying_the_extras() -> None:
    native = _NativeWithFence()
    fence = MenhirPublishFence(ClaimFence("q", "w", NOW, "ns", "twin"), GateFence(0))
    await _call_client(native, new_episode_uuid="twin", publish_fence=fence)
    assert native.kwargs["new_episode_uuid"] == "twin"
    forwarded = native.kwargs["publish_fence"]
    assert isinstance(forwarded, MenhirPublishFence) and forwarded is not fence
    assert forwarded.claim == fence.claim and len(forwarded.after_write_extras) == 2
    assert fence.after_write_extras == ()

    plain = _NativeWithFence()
    await _call_client(plain)
    assert plain.kwargs == {"new_episode_uuid": None, "publish_fence": None}


# ------------------------------------------------------------------ (a) happy path
@pytest.mark.online
def test_happy_path_links_and_records_read_context(test_neo4j_repo, seed) -> None:
    seen: list[dict[str, Any]] = []

    async def extra(
        tx: Any, *, episode_uuid: str, entity_edge_uuids: list[str]
    ) -> None:
        seen.append({"episode_uuid": episode_uuid, "edges": entity_edge_uuids})
        await _tx_run(
            tx, "MATCH (q:Episodic {uuid: $q}) SET q.extra_ran = true", {"q": seed.q}
        )

    fence = _fence(
        seed,
        after_write_extras=(extra,),
        repair_turn_ids_provider=lambda: [seed.turn, f"{seed.tag}-missing-turn"],
    )
    _drive(
        test_neo4j_repo,
        fence,
        seed,
        previous=(seed.previous, f"{seed.tag}-missing-prev"),
    )

    assert _q_row(test_neo4j_repo, seed)["resolved"] == seed.twin
    assert _q_row(test_neo4j_repo, seed)["extra_ran"] is True
    assert seen == [{"episode_uuid": seed.twin, "edges": ["edge-1"]}]
    back = _one(
        test_neo4j_repo,
        "MATCH (g:Episodic {uuid: $t}) RETURN g.menhir_memory_uuid AS m",
        t=seed.twin,
    )
    assert back["m"] == seed.q
    assert _read_context(test_neo4j_repo, seed) == [
        ("previous_episode", seed.previous, False),
        ("repair_turn", seed.turn, False),
    ]


# ------------------------------------------------------------------ (b) each failed check
def _apply_failure(repo: Any, s: Seed, case: str) -> tuple[ClaimFence, GateFence]:
    claim, gate = s.claim(), GateFence(epoch=0)
    if case == "marker":
        repo.execute(
            "CREATE (:ErasureMarker {namespace_key: $ns, subject_uuid: $q, op_id: $op})",
            params={"ns": s.ns, "q": s.q, "op": f"{s.tag}-op"},
        )
    elif case == "epoch_moved":
        repo.execute(
            "MATCH (f:EvidenceNamespaceFence {namespace_key: $ns}) SET f.erasure_epoch = 1",
            params={"ns": s.ns},
        )
    elif case == "erasing_ops":
        repo.execute(
            "MATCH (f:EvidenceNamespaceFence {namespace_key: $ns}) SET f.erasing_ops = ['op-1']",
            params={"ns": s.ns},
        )
    elif case == "owner_changed":
        repo.execute(
            "MATCH (q:Episodic {uuid: $q}) SET q.processing_owner = 'someone-else'",
            params={"q": s.q},
        )
    elif case == "claim_instant_changed":
        claim = s.claim(claim_started_at=s.started + timedelta(seconds=1))
    elif case == "already_linked":
        repo.execute(
            "MATCH (q:Episodic {uuid: $q}) SET q.resolved_episode_uuid = 'an-older-twin'",
            params={"q": s.q},
        )
    elif case == "uuid_not_reserved":
        repo.execute(
            "MATCH (q:Episodic {uuid: $q}) SET q.reserved_twin_uuids = ['another-uuid']",
            params={"q": s.q},
        )
    elif case == "queue_node_missing":
        repo.execute("MATCH (q:Episodic {uuid: $q}) DETACH DELETE q", params={"q": s.q})
    elif case == "not_enriching":
        repo.execute(
            "MATCH (q:Episodic {uuid: $q}) SET q.processing_state = 'FAILED'",
            params={"q": s.q},
        )
    else:  # pragma: no cover
        raise AssertionError(case)
    return claim, gate


@pytest.mark.online
@pytest.mark.parametrize(
    "case",
    [
        "marker",
        "epoch_moved",
        "erasing_ops",
        "owner_changed",
        "claim_instant_changed",
        "already_linked",
        "uuid_not_reserved",
        "queue_node_missing",
        "not_enriching",
    ],
)
def test_each_failed_check_raises_and_rolls_back(test_neo4j_repo, seed, case) -> None:
    claim, gate = _apply_failure(test_neo4j_repo, seed, case)
    expected = {"marker": "erasure_marker"}.get(case, case)
    before = _q_row(test_neo4j_repo, seed)

    with pytest.raises(PublishFenced) as excinfo:
        _drive(
            test_neo4j_repo,
            MenhirPublishFence(
                claim, gate, repair_turn_ids_provider=lambda: [seed.turn]
            ),
            seed,
            previous=(seed.previous,),
        )

    assert excinfo.value.reason == expected
    assert _twin_count(test_neo4j_repo, seed) == 0
    assert _read_context(test_neo4j_repo, seed) == []
    # The statement-1 lock (q.lock_nonce) was rolled back with everything else.
    assert _q_row(test_neo4j_repo, seed) == before
    if before is not None:
        assert before["nonce"] is None


@pytest.mark.online
def test_a_late_failure_rolls_back_twin_link_and_context(test_neo4j_repo, seed) -> None:
    with pytest.raises(RuntimeError):
        _drive(
            test_neo4j_repo,
            _fence(seed),
            seed,
            previous=(seed.previous,),
            then_raise=True,
        )
    assert _twin_count(test_neo4j_repo, seed) == 0
    assert _q_row(test_neo4j_repo, seed)["resolved"] is None
    assert _read_context(test_neo4j_repo, seed) == []


@pytest.mark.online
def test_after_write_without_the_twin_rolls_back(test_neo4j_repo, seed) -> None:
    with pytest.raises(PublishFenced) as excinfo:
        _drive(test_neo4j_repo, _fence(seed), seed, create_twin=False)
    assert excinfo.value.reason == "link_target_missing"
    assert _q_row(test_neo4j_repo, seed)["resolved"] is None
    assert _q_row(test_neo4j_repo, seed)["nonce"] is None


# ------------------------------------------------------------------ (c) default namespace key
@pytest.mark.online
def test_blank_group_id_locks_fence_default(test_neo4j_repo, seed) -> None:
    key = _one(
        test_neo4j_repo,
        "WITH {group_id: '', namespace: null} AS n RETURN "
        + fence_key_cypher("n")
        + " AS k",
    )["k"]
    assert key == "default"
    existing = _one(
        test_neo4j_repo,
        "MATCH (f:EvidenceNamespaceFence {namespace_key: 'default'}) "
        "RETURN size(coalesce(f.erasing_ops, [])) AS ops",
    )
    if existing is not None and existing["ops"]:
        pytest.skip("the shared 'default' fence has an erase in flight")
    gate = read_gate_fence(test_neo4j_repo, "default")
    test_neo4j_repo.execute(
        "MATCH (q:Episodic {uuid: $q}) SET q.namespace = 'default', q.group_id = ''",
        params={"q": seed.q},
    )
    fence = MenhirPublishFence(seed.claim(namespace_key=key), gate)
    nonces: list[Any] = []

    async def hooks(tx: Any) -> None:
        await fence.before_write(tx, episode_uuid=seed.twin, previous_episode_uuids=[])
        rows = await _tx_run(
            tx,
            "MATCH (f:EvidenceNamespaceFence {namespace_key: 'default'}) RETURN f.lock_nonce AS n",
            {},
        )
        nonces.append(rows[0]["n"])

    def work(tx: Any) -> None:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(hooks(tx))
        finally:
            loop.close()

    test_neo4j_repo.execute_write(work)
    assert nonces and nonces[0]  # the lock landed on fence 'default', not on a '' fence
    assert (
        _one(
            test_neo4j_repo,
            "MATCH (f:EvidenceNamespaceFence {namespace_key: ''}) RETURN count(f) AS c",
        )["c"]
        == 0
    )


# ------------------------------------------------------------------ (d) run_fenced / check_fence
@pytest.mark.online
def test_run_fenced_writes_when_the_claim_holds(test_neo4j_repo, seed) -> None:
    statements = [
        (
            "MATCH (q:Episodic {uuid: $q}) SET q.fenced_write = true RETURN q.uuid AS u",
            {"q": seed.q},
        )
    ]
    out = run_fenced(test_neo4j_repo, seed.claim(), GateFence(epoch=0), statements)
    assert out == [[{"u": seed.q}]]
    check_fence(test_neo4j_repo, seed.claim(), GateFence(epoch=0))
    assert (
        _one(
            test_neo4j_repo,
            "MATCH (q:Episodic {uuid: $q}) RETURN q.fenced_write AS w",
            q=seed.q,
        )["w"]
        is True
    )


@pytest.mark.online
def test_run_fenced_raises_fence_lost_on_a_marker_and_writes_nothing(
    test_neo4j_repo, seed
) -> None:
    _apply_failure(test_neo4j_repo, seed, "marker")
    statements = [
        ("MATCH (q:Episodic {uuid: $q}) SET q.fenced_write = true", {"q": seed.q})
    ]
    with pytest.raises(FenceLost) as excinfo:
        run_fenced(test_neo4j_repo, seed.claim(), GateFence(epoch=0), statements)
    assert excinfo.value.reason == "erasure_marker"
    assert (
        _one(
            test_neo4j_repo,
            "MATCH (q:Episodic {uuid: $q}) RETURN q.fenced_write AS w",
            q=seed.q,
        )["w"]
        is None
    )
    with pytest.raises(FenceLost):
        check_fence(test_neo4j_repo, seed.claim(), None)


@pytest.mark.online
def test_run_fenced_epoch_and_erasing_ops_apply_only_with_a_gate(
    test_neo4j_repo, seed
) -> None:
    _apply_failure(test_neo4j_repo, seed, "epoch_moved")
    _apply_failure(test_neo4j_repo, seed, "erasing_ops")
    statements = [
        ("MATCH (q:Episodic {uuid: $q}) SET q.fenced_write = true", {"q": seed.q})
    ]
    with pytest.raises(FenceLost) as excinfo:
        run_fenced(test_neo4j_repo, seed.claim(), GateFence(epoch=0), statements)
    assert excinfo.value.reason == "epoch_moved"
    run_fenced(
        test_neo4j_repo, seed.claim(), None, statements
    )  # before gate entry: allowed
    assert (
        _one(
            test_neo4j_repo,
            "MATCH (q:Episodic {uuid: $q}) RETURN q.fenced_write AS w",
            q=seed.q,
        )["w"]
        is True
    )


# ------------------------------------------------------------------ (e) requeue_fenced
@pytest.mark.online
def test_requeue_fenced_refunds_the_attempt(test_neo4j_repo, seed) -> None:
    assert requeue_fenced(test_neo4j_repo, seed.claim(), "epoch_moved") is True
    row = _q_row(test_neo4j_repo, seed)
    assert row["state"] == "PENDING"
    assert row["attempts"] == 1
    assert row["owner"] is None
    assert row["retry_after"] is not None
    # A second call no longer owns the claim: nothing changes.
    assert requeue_fenced(test_neo4j_repo, seed.claim(), "epoch_moved") is False
    assert _q_row(test_neo4j_repo, seed)["attempts"] == 1


@pytest.mark.online
def test_requeue_fenced_is_silent_with_a_marker(test_neo4j_repo, seed) -> None:
    _apply_failure(test_neo4j_repo, seed, "marker")
    assert requeue_fenced(test_neo4j_repo, seed.claim(), "erasure_marker") is False
    row = _q_row(test_neo4j_repo, seed)
    assert row["state"] == "ENRICHING"
    assert row["attempts"] == 2
    assert row["owner"] == WORKER
    assert row["retry_after"] is None


# ------------------------------------------------------------------ (f) idempotence
@pytest.mark.online
def test_after_write_is_idempotent_across_transactions(test_neo4j_repo, seed) -> None:
    fence = _fence(seed, repair_turn_ids_provider=lambda: [seed.turn])
    _drive(test_neo4j_repo, fence, seed, previous=(seed.previous,))
    expected = [
        ("previous_episode", seed.previous, False),
        ("repair_turn", seed.turn, False),
    ]
    assert _read_context(test_neo4j_repo, seed) == expected

    # A replay of the post-save half (what a driver re-run does after an uncommitted attempt).
    _drive(test_neo4j_repo, fence, seed, previous=(seed.previous,), skip_before=True)
    assert _read_context(test_neo4j_repo, seed) == expected
    assert _q_row(test_neo4j_repo, seed)["resolved"] == seed.twin

    # A full second run is refused: the queue node is linked now.
    with pytest.raises(PublishFenced) as excinfo:
        _drive(test_neo4j_repo, fence, seed, previous=(seed.previous,))
    assert excinfo.value.reason == "already_linked"
    assert _read_context(test_neo4j_repo, seed) == expected


@pytest.mark.online
def test_a_rolled_back_attempt_can_be_rerun_in_full(test_neo4j_repo, seed) -> None:
    fence = _fence(seed)
    with pytest.raises(RuntimeError):
        _drive(test_neo4j_repo, fence, seed, previous=(seed.previous,), then_raise=True)
    _drive(test_neo4j_repo, fence, seed, previous=(seed.previous,))
    assert _read_context(test_neo4j_repo, seed) == [
        ("previous_episode", seed.previous, False)
    ]


# ------------------------------------------------------------------ (g) the real fork bulk writer
@pytest.mark.online
async def test_real_fork_bulk_writer_fenced_and_allowed(test_neo4j_repo, seed) -> None:
    pytest.importorskip("graphiti_core.publish_fence")
    from unittest.mock import AsyncMock, Mock

    from graphiti_core.driver.neo4j_driver import Neo4jDriver
    from graphiti_core.embedder.client import EmbedderClient
    from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
    from graphiti_core.utils.bulk_utils import add_nodes_and_edges_bulk

    uri = os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://127.0.0.1:7688")
    user = os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j")
    password = os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword")
    driver = Neo4jDriver(uri=uri, user=user, password=password)
    embedder = Mock(spec=EmbedderClient)
    embedder.create = AsyncMock(return_value=[0.1, 0.2, 0.3])

    def episode() -> EpisodicNode:
        return EpisodicNode(
            uuid=seed.twin,
            name="fenced twin",
            group_id=seed.ns,
            labels=[],
            source=EpisodeType.message,
            source_description="test",
            content="Alice likes Bob",
            created_at=NOW,
            valid_at=NOW,
            entity_edges=[],
        )

    def entity() -> EntityNode:
        return EntityNode(
            name=f"{seed.tag}-alice",
            group_id=seed.ns,
            labels=["Entity"],
            created_at=NOW,
            summary="",
        )

    async def publish(fence: MenhirPublishFence) -> None:
        await add_nodes_and_edges_bulk(
            driver,
            [episode()],
            [],
            [entity()],
            [],
            embedder,
            publish_fence=fence,
            fence_episode_uuid=seed.twin,
            previous_episode_uuids=[seed.previous],
        )

    def entity_count() -> int:
        return _one(
            test_neo4j_repo,
            "MATCH (n:Entity {group_id: $ns}) RETURN count(n) AS c",
            ns=seed.ns,
        )["c"]

    try:
        _apply_failure(test_neo4j_repo, seed, "marker")
        with pytest.raises(PublishFenced) as excinfo:
            await publish(_fence(seed))
        assert excinfo.value.reason == "erasure_marker"
        assert _twin_count(test_neo4j_repo, seed) == 0
        assert entity_count() == 0

        test_neo4j_repo.execute(
            "MATCH (m:ErasureMarker {subject_uuid: $q}) DELETE m", params={"q": seed.q}
        )
        await publish(_fence(seed, repair_turn_ids_provider=lambda: [seed.turn]))
        assert _twin_count(test_neo4j_repo, seed) == 1
        assert entity_count() == 1
        assert _q_row(test_neo4j_repo, seed)["resolved"] == seed.twin
        assert _read_context(test_neo4j_repo, seed) == [
            ("previous_episode", seed.previous, False),
            ("repair_turn", seed.turn, False),
        ]
    finally:
        await driver.close()
