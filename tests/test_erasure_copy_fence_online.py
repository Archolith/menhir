"""Online counterexample tests for G-copy (``execute_copy_fenced``), plan v2 section 2.2.

Real Neo4j through ``test_neo4j_repo``. The copier's ``work(tx)`` writes a ``CopyProbe`` node, so each
test can assert "written" / "not written". Seal and release are the real erase-side primitives.
"""

from __future__ import annotations

import concurrent.futures as cf
import threading
import uuid as uuidlib
from typing import Any

import pytest

from menhir.infrastructure import publish_fence
from menhir.infrastructure.erasure_fence import (
    release_memory,
    seal_memory,
    seal_namespace,
)

WAIT_S = 20.0


class _Rig:
    def __init__(self, repo: Any) -> None:
        from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

        self.repo = repo
        self.tag = f"gcopy-{uuidlib.uuid4().hex[:8]}"
        self.ns = f"{self.tag}-ns"
        self.adapter = MemoryGraphAdapter(neo4j=repo)
        self.n = 0
        self.ops: list[str] = []

    def op(self) -> str:
        o = uuidlib.uuid4().hex
        self.ops.append(o)
        return o

    def seed_q(self) -> str:
        self.n += 1
        q = f"{self.tag}-q{self.n}"
        self.adapter.create_pending_episode(
            episode_uuid=q,
            name=f"anchor-{q}",
            content=f"content of {q}",
            session_id=f"{self.tag}-session",
            user_id="gcopy-user",
            source="claude-code",
            source_confidence=0.5,
            namespace=self.ns,
        )
        return q

    def epoch(self) -> int:
        return publish_fence.read_copy_epoch(self.repo, self.ns)

    def fence_rows(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.repo.execute(
                "MATCH (f:EvidenceNamespaceFence {namespace_key: $k}) "
                "RETURN f.erasure_epoch AS epoch, coalesce(f.erasing_ops, []) AS ops",
                params={"k": self.ns},
            )
        ]

    def probe_work(self, key: str, text: str = "copied") -> Any:
        def work(tx: Any) -> str:
            tx.execute(
                "MERGE (p:CopyProbe {k: $k}) SET p.text = $t", {"k": key, "t": text}
            )
            return f"ret-{key}"

        return work

    def probe_count(self, key: str) -> int:
        rows = self.repo.execute(
            "MATCH (p:CopyProbe {k: $k}) RETURN count(p) AS c", params={"k": key}
        )
        return int(rows[0]["c"])

    def copy(self, read_epoch: int, sources: list[str], key: str, ns: str | None = None) -> Any:
        return publish_fence.execute_copy_fenced(
            self.repo,
            self.ns if ns is None else ns,
            read_epoch,
            sources,
            self.probe_work(key),
        )

    def cleanup(self) -> None:
        for op in self.ops:
            try:
                release_memory(self.repo, op)
            except Exception:  # noqa: BLE001
                pass
        self.repo.execute(
            "MATCH (n) WHERE n.uuid STARTS WITH $tag OR n.k STARTS WITH $tag "
            "OR n.group_id = $ns OR n.namespace = $ns OR n.namespace_key = $ns DETACH DELETE n",
            params={"tag": self.tag, "ns": self.ns},
        )


def _refused(rig: _Rig, reason: str, read_epoch: int, sources: list[str], key: str) -> None:
    with pytest.raises(publish_fence.CopyFenced) as exc:
        rig.copy(read_epoch, sources, key)
    assert exc.value.reason == reason
    assert rig.probe_count(key) == 0, "a refused copy must write nothing"


@pytest.mark.online
def test_happy_path_unmarked_source_runs_work_and_passes_result(test_neo4j_repo) -> None:
    rig = _Rig(test_neo4j_repo)
    try:
        src = rig.seed_q()
        epoch = rig.epoch()
        out = rig.copy(epoch, [src], f"{rig.tag}-ok")
        assert out == f"ret-{rig.tag}-ok"
        assert rig.probe_count(f"{rig.tag}-ok") == 1
    finally:
        rig.cleanup()


@pytest.mark.online
def test_namespace_without_fence_is_epoch_zero_and_leaves_fence(test_neo4j_repo) -> None:
    rig = _Rig(test_neo4j_repo)
    try:
        assert rig.fence_rows() == []
        assert rig.epoch() == 0
        rig.copy(0, [], f"{rig.tag}-nofence")
        assert rig.probe_count(f"{rig.tag}-nofence") == 1
        rows = rig.fence_rows()
        assert len(rows) == 1 and rows[0]["epoch"] == 0 and rows[0]["ops"] == []
    finally:
        rig.cleanup()


@pytest.mark.online
def test_epoch_moved_by_unrelated_erase_refuses_copy(test_neo4j_repo) -> None:
    rig = _Rig(test_neo4j_repo)
    try:
        src = rig.seed_q()
        other = rig.seed_q()
        stale = rig.epoch()
        op = rig.op()
        seal_memory(test_neo4j_repo, op, [other])
        release_memory(test_neo4j_repo, op)
        assert rig.epoch() > stale  # precondition: the epoch really moved
        _refused(rig, "epoch_moved", stale, [src], f"{rig.tag}-moved")
        # control: re-reading the epoch makes the same copy succeed (nothing else blocks it)
        rig.copy(rig.epoch(), [src], f"{rig.tag}-moved-ctl")
        assert rig.probe_count(f"{rig.tag}-moved-ctl") == 1
    finally:
        rig.cleanup()


@pytest.mark.online
def test_erasing_ops_in_flight_refuses_even_with_current_epoch(test_neo4j_repo) -> None:
    rig = _Rig(test_neo4j_repo)
    try:
        src = rig.seed_q()
        op = rig.op()
        seal_namespace(test_neo4j_repo, op, rig.ns)  # held, not released
        current = rig.epoch()  # read AFTER the seal: the epoch check alone passes
        _refused(rig, "erasing_ops", current, [src], f"{rig.tag}-ops")
        release_memory(test_neo4j_repo, op)
        rig.copy(rig.epoch(), [src], f"{rig.tag}-ops-ctl")
        assert rig.probe_count(f"{rig.tag}-ops-ctl") == 1
    finally:
        rig.cleanup()


@pytest.mark.online
def test_marked_source_refuses_but_unmarked_source_copies(test_neo4j_repo) -> None:
    rig = _Rig(test_neo4j_repo)
    try:
        q = rig.seed_q()
        clean = rig.seed_q()
        op = rig.op()
        seal_memory(test_neo4j_repo, op, [q])
        release_memory(test_neo4j_repo, op)
        fresh = rig.epoch()  # epoch and erasing_ops are fine; only the marker remains
        _refused(rig, "source_marked", fresh, [clean, q], f"{rig.tag}-marked")
        rig.copy(fresh, [clean], f"{rig.tag}-marked-ctl")
        assert rig.probe_count(f"{rig.tag}-marked-ctl") == 1
    finally:
        rig.cleanup()


@pytest.mark.online
def test_seal_blocks_behind_in_flight_copy_and_stale_epoch_is_then_refused(
    test_neo4j_repo,
) -> None:
    rig = _Rig(test_neo4j_repo)
    parked = threading.Event()
    proceed = threading.Event()
    pool = cf.ThreadPoolExecutor(max_workers=2)
    try:
        src = rig.seed_q()
        victim = rig.seed_q()
        rig.copy(rig.epoch(), [src], f"{rig.tag}-warm")  # make sure the fence node exists
        read_epoch = rig.epoch()
        key = f"{rig.tag}-parked"

        def parked_work(tx: Any) -> str:
            tx.execute("MERGE (p:CopyProbe {k: $k}) SET p.text = 'x'", {"k": key})
            parked.set()
            if not proceed.wait(WAIT_S):
                raise TimeoutError("test never released the parked copy")
            return "done"

        a = pool.submit(
            publish_fence.execute_copy_fenced,
            test_neo4j_repo, rig.ns, read_epoch, [src], parked_work,
        )
        assert parked.wait(WAIT_S), "copy A never reached its work"
        op = rig.op()
        b = pool.submit(seal_memory, test_neo4j_repo, op, [victim])
        done, _ = cf.wait([b], timeout=1.0)
        assert not done, "seal returned while a copy held the fence lock inside its transaction"
        assert not a.done()
        proceed.set()
        assert a.result(timeout=WAIT_S) == "done"
        sealed = b.result(timeout=WAIT_S)
        assert sealed.sealed_epoch > read_epoch
        assert rig.probe_count(key) == 1  # A committed before the seal proceeded
        release_memory(test_neo4j_repo, op)
        _refused(rig, "epoch_moved", read_epoch, [src], f"{rig.tag}-after-seal")
    finally:
        proceed.set()
        pool.shutdown(wait=True, cancel_futures=True)
        rig.cleanup()


@pytest.mark.online
@pytest.mark.parametrize("blank", ["", None, "   "])
def test_blank_namespace_key_maps_to_default(test_neo4j_repo, blank) -> None:
    repo = test_neo4j_repo
    tag = f"gcopy-{uuidlib.uuid4().hex[:8]}"
    key = f"{tag}-blank"
    try:
        # The wipe fixture clears the graph, so the 'default' fence is ours for this test.
        base = publish_fence.read_copy_epoch(repo, "default")
        publish_fence.execute_copy_fenced(
            repo, blank, base, [], lambda tx: tx.execute(
                "MERGE (p:CopyProbe {k: $k})", {"k": key}
            ),
        )
        fences = repo.execute(
            "MATCH (f:EvidenceNamespaceFence {namespace_key: 'default'}) RETURN count(f) AS c"
        )
        assert int(fences[0]["c"]) == 1
        empty = repo.execute(
            "MATCH (f:EvidenceNamespaceFence) WHERE f.namespace_key IN ['', 'None'] "
            "RETURN count(f) AS c"
        )
        assert int(empty[0]["c"]) == 0
        # a stale default epoch is refused through the blank spelling too
        op = uuidlib.uuid4().hex
        seal_namespace(repo, op, "default")
        try:
            with pytest.raises(publish_fence.CopyFenced):
                publish_fence.execute_copy_fenced(
                    repo, blank, base, [], lambda tx: tx.execute(
                        "MERGE (p:CopyProbe {k: $k})", {"k": key + "-2"}
                    ),
                )
        finally:
            release_memory(repo, op)
        assert repo.execute(
            "MATCH (p:CopyProbe {k: $k}) RETURN count(p) AS c", params={"k": key + "-2"}
        )[0]["c"] == 0
    finally:
        repo.execute("MATCH (p:CopyProbe) WHERE p.k STARTS WITH $t DETACH DELETE p", params={"t": tag})


@pytest.mark.online
def test_transient_error_retry_rolls_back_first_attempt_and_writes_once(
    test_neo4j_repo, monkeypatch
) -> None:
    from neo4j.exceptions import TransientError

    monkeypatch.setattr("menhir.infrastructure.neo4j.time.sleep", lambda _s: None)
    rig = _Rig(test_neo4j_repo)
    attempts: list[int] = []
    try:
        key = f"{rig.tag}-retry"

        def work(tx: Any) -> str:
            attempts.append(1)
            # CREATE (not MERGE): a first attempt that was not rolled back would leave a duplicate.
            tx.execute("CREATE (p:CopyProbe {k: $k})", {"k": key})
            if len(attempts) == 1:
                raise TransientError("transient deadlock (test)")
            return "ok"

        out = publish_fence.execute_copy_fenced(
            test_neo4j_repo, rig.ns, rig.epoch(), [], work
        )
        assert out == "ok"
        assert len(attempts) == 2
        assert rig.probe_count(key) == 1
    finally:
        rig.cleanup()
