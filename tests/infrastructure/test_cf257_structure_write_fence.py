"""CF-257 phase 2 -- the structure-write fence.

A migration that counts rows, reconciles duplicates and switches a key has to run against a graph
nothing else is writing: a count taken at T is worthless if a write lands at T+1.

Four menhir processes run concurrently on this deployment (two `serve`, two `serve-watch`, the
latter carrying the unattended structure watcher), so the fence lives in the graph rather than in
a process. These tests use a fake Neo4j that models the fence node's state, because what is being
pinned is the ADMISSION PROTOCOL -- that a writer cannot be admitted without being counted -- not
Cypher syntax. The online lane exercises the real statements.
"""

from __future__ import annotations

import time

import pytest

from menhir.infrastructure.structure_write_fence import (
    STALE_WRITER_SECONDS,
    FenceHandle,
    StructureWritesFrozen,
    admit_structure_writer,
    fence_status,
    lower_fence,
    raise_fence,
    release_structure_writer,
)


class _FakeNeo4j:
    """Models the fence node closely enough to exercise the protocol.

    Deliberately executes the guard as ONE step, mirroring the single Cypher statement: if the
    fake let a caller observe `frozen` and register separately, it would be a friendlier model
    than reality and would hide the very race the statement exists to close.
    """

    def __init__(self):
        self.frozen = False
        self.reason = None
        self.writers: list[str] = []
        self.statements: list[str] = []

    def execute(self, cypher, params=None):
        params = params or {}
        self.statements.append(cypher)
        if "SET f.frozen = true" in cypher:
            self.frozen, self.reason = True, params.get("reason")
            return []
        if "SET f.frozen = false, f.reason = null" in cypher:
            self.frozen, self.reason = False, None
            return []
        if "f.writers = coalesce(f.writers, []) + [$entry]" in cypher:
            if self.frozen:
                return []                      # WHERE filtered the row out; SET never ran
            self.writers.append(params["entry"])
            return [{"active": len(self.writers)}]
        if "WHERE NOT w STARTS WITH $prefix" in cypher:
            self.writers = [w for w in self.writers if not w.startswith(params["prefix"])]
            return []
        if "RETURN coalesce(f.frozen, false) AS frozen" in cypher:
            return [{"frozen": self.frozen, "reason": self.reason, "writers": list(self.writers)}]
        raise AssertionError(f"unexpected statement: {cypher[:80]}")


@pytest.fixture
def neo4j():
    return _FakeNeo4j()


@pytest.mark.unit
def test_a_writer_is_admitted_and_counted_when_the_fence_is_down(neo4j):
    handle = admit_structure_writer(neo4j, label="proj")
    assert isinstance(handle, FenceHandle)
    assert len(fence_status(neo4j)["active"]) == 1


@pytest.mark.unit
def test_releasing_removes_exactly_one_writer(neo4j):
    a = admit_structure_writer(neo4j, label="a")
    admit_structure_writer(neo4j, label="b")
    release_structure_writer(neo4j, a)
    active = fence_status(neo4j)["active"]
    assert len(active) == 1 and active[0]["label"] == "b"


@pytest.mark.unit
def test_a_raised_fence_refuses_new_writers(neo4j):
    raise_fence(neo4j, reason="CF-257 phase 2a")
    with pytest.raises(StructureWritesFrozen, match="phase 2a"):
        admit_structure_writer(neo4j)


@pytest.mark.unit
def test_a_refused_writer_is_never_counted(neo4j):
    """THE INVARIANT. Admission and registration are one step, so these cannot disagree.

    If a refusal could still register, the drain would wait forever on a writer that never ran;
    if an admission could skip registering, the drain would report zero while a writer was live
    and the migration would read a moving graph. Both directions are the same bug.
    """
    raise_fence(neo4j, reason="migration")
    with pytest.raises(StructureWritesFrozen):
        admit_structure_writer(neo4j)
    status = fence_status(neo4j)
    assert status["active"] == [] and status["stale"] == []


@pytest.mark.unit
def test_writers_admitted_before_the_fence_remain_counted(neo4j):
    """The drain's whole job: raising the fence stops NEW writers, it does not stop running ones.

    `write_project_structure` is offloaded to a thread, so a writer already inside it keeps going.
    A fence that reported zero here would let the migration start mid-write.
    """
    admit_structure_writer(neo4j, label="in-flight")
    raise_fence(neo4j, reason="migration")
    assert len(fence_status(neo4j)["active"]) == 1
    assert fence_status(neo4j)["frozen"] is True


@pytest.mark.unit
def test_lowering_the_fence_admits_writers_again(neo4j):
    raise_fence(neo4j, reason="migration")
    lower_fence(neo4j)
    admit_structure_writer(neo4j)  # must not raise
    assert fence_status(neo4j)["frozen"] is False


@pytest.mark.unit
def test_an_abandoned_writer_is_reported_as_stale_not_active(neo4j):
    """A writer killed mid-write cannot release its slot, and a fence that waits forever on a
    dead process is an outage rather than a safeguard. Stale entries are separated so the drain
    can proceed while still naming what it disregarded -- the report is the safety valve."""
    neo4j.writers.append(f"9999:dead|{time.time() - STALE_WRITER_SECONDS - 60:.0f}|ghost")
    status = fence_status(neo4j)
    assert status["active"] == []
    assert len(status["stale"]) == 1 and status["stale"][0]["label"] == "ghost"


@pytest.mark.unit
def test_a_slow_but_live_writer_is_not_reaped(neo4j):
    """The converse, so the staleness rule cannot degenerate into 'reap everything'."""
    neo4j.writers.append(f"1:slow|{time.time() - 5:.0f}|big-project")
    status = fence_status(neo4j)
    assert len(status["active"]) == 1 and status["stale"] == []


@pytest.mark.unit
def test_releasing_a_missing_handle_is_harmless(neo4j):
    release_structure_writer(neo4j, None)


@pytest.mark.unit
def test_release_never_raises_into_the_caller(neo4j, monkeypatch):
    """Releasing guards a write that already succeeded; failing here would turn a completed write
    into a reported failure."""
    def _boom(*a, **k):
        raise RuntimeError("neo4j gone")
    handle = admit_structure_writer(neo4j)
    monkeypatch.setattr(neo4j, "execute", _boom)
    release_structure_writer(neo4j, handle)  # must not raise


@pytest.mark.unit
def test_the_choke_point_admits_and_releases_around_the_write():
    """The fence belongs on the ONE method all four writers funnel through.

    The REST scan path, the deprecated raw-payload path, the background symbol rescan and the
    unattended watcher all reach `MemoryGraphAdapter.write_project_structure`. Guarding the call
    sites instead would leave four places to keep in step and the next writer would miss it.
    """
    import inspect
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    src = inspect.getsource(MemoryGraphAdapter.write_project_structure)
    assert "admit_structure_writer" in src
    assert "release_structure_writer" in src
    assert "finally" in src, "the slot must be released even when the write raises"
