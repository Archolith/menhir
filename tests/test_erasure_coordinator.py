"""Unit tests for the explicit-erasure saga (CF-165 Phase G).

Uses a fake graph adapter and a real SQLite sidecar under ``tmp_path``. Never touches the real
telemetry database or a live graph.
"""

from __future__ import annotations

import sqlite3

import pytest

from menhir.infrastructure.erasure_subjects import ErasureSubjectStore
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.services.erasure_coordinator import (
    ERASED,
    GRAPH_ALREADY_ABSENT,
    NOTHING_TO_ERASE,
    ErasureCoordinator,
)

pytestmark = [pytest.mark.unit]


class FakeAdapter:
    """Minimal graph stand-in. Records what erasure asked it to destroy."""

    def __init__(self, *, present: bool = True, members: list[str] | None = None) -> None:
        self.present = present
        self.members = members or []
        self.deleted_nodes: list[str] = []
        self.deleted_namespaces: list[str] = []

    def node_exists(self, node_uuid: str) -> bool:
        return self.present

    def delete_memory(self, node_uuid: str) -> bool:
        self.deleted_nodes.append(node_uuid)
        return self.present

    def capture_namespace_uuids(self, group_id: str, *, namespace: str | None = None):
        return list(self.members)

    def delete_namespace(self, group_id: str, *, namespace: str | None = None) -> int:
        self.deleted_namespaces.append(group_id)
        return len(self.members)


def _coordinator(tmp_path, adapter: FakeAdapter) -> ErasureCoordinator:
    db = tmp_path / "t.db"
    McpTelemetryStore(db_path=db)._ensure_ready()
    return ErasureCoordinator(
        graph_adapter=adapter,
        journal=GraphOperationsJournal(db_path=db),
        subjects=ErasureSubjectStore(db_path=db),
    )


def _seed_revision(db, node_uuid: str, content: str = "secret") -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO memory_revisions "
            "(recorded_at, node_uuid, field, old_value, new_value, changed_by) "
            "VALUES (?,?,?,?,?,?)",
            ("t", node_uuid, "content", content, content, "test"),
        )
        conn.commit()


def _revision_values(db, node_uuid: str) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT old_value, new_value FROM memory_revisions WHERE node_uuid = ?",
            (node_uuid,),
        ).fetchall()


def test_erase_memory_purges_sidecar_content(tmp_path):
    adapter = FakeAdapter(present=True)
    coord = _coordinator(tmp_path, adapter)
    db = tmp_path / "t.db"
    _seed_revision(db, "n-1")

    out = coord.erase_memory("n-1")

    assert out["reason"] == ERASED
    assert adapter.deleted_nodes == ["n-1"]
    assert _revision_values(db, "n-1") == [(None, None)]


def test_erasure_proceeds_when_graph_node_already_absent(tmp_path):
    """The gap this closes: the old delete path journaled nothing when the node was gone.

    A merge intentionally removes the absorbed node while keeping its recovery snapshot, so
    "already absent from the graph" is exactly the state in which sidecar content most needs
    erasing. The outcome must say which case it was.
    """
    adapter = FakeAdapter(present=False)
    coord = _coordinator(tmp_path, adapter)
    db = tmp_path / "t.db"
    _seed_revision(db, "n-gone")

    out = coord.erase_memory("n-gone")

    assert out["reason"] == GRAPH_ALREADY_ABSENT
    # The delete IS attempted and reports it touched nothing. Skipping it on a prior probe was
    # the earlier design and it did not survive a live graph: the real adapter has no
    # node_exists, so the probe's fallback claimed "present" every time and this outcome could
    # never be reported outside tests.
    assert adapter.deleted_nodes == ["n-gone"]
    assert _revision_values(db, "n-gone") == [(None, None)]


def test_erase_memory_leaves_other_subjects_untouched(tmp_path):
    coord = _coordinator(tmp_path, FakeAdapter(present=True))
    db = tmp_path / "t.db"
    _seed_revision(db, "n-1", "erase-me")
    _seed_revision(db, "n-2", "keep-me")

    coord.erase_memory("n-1")

    assert _revision_values(db, "n-2") == [("keep-me", "keep-me")]


def test_prepare_records_subjects_and_commits_the_operation(tmp_path):
    coord = _coordinator(tmp_path, FakeAdapter(present=True))
    db = tmp_path / "t.db"
    _seed_revision(db, "n-1")

    out = coord.erase_memory("n-1")
    op_id = out["op_id"]

    rows = coord.subjects.fetch_subjects(op_id)
    assert [(r["subject_type"], r["subject_value"]) for r in rows] == [("NODE_UUID", "n-1")]
    # Subjects are stamped purged, so the read veto stops suppressing them once the content
    # is actually gone.
    assert coord.subjects.count_unpurged(op_id) == 0
    with sqlite3.connect(db) as conn:
        state = conn.execute(
            "SELECT state FROM graph_operations WHERE op_id = ?", (op_id,)
        ).fetchone()
    assert state[0] == "COMMITTED"


def test_namespace_erasure_captures_members_before_deleting(tmp_path):
    adapter = FakeAdapter(present=True, members=["m-1", "m-2"])
    coord = _coordinator(tmp_path, adapter)
    db = tmp_path / "t.db"
    _seed_revision(db, "m-1")
    _seed_revision(db, "m-2")

    out = coord.erase_namespace("ns-1")

    assert out["reason"] == ERASED
    assert adapter.deleted_namespaces == ["ns-1"]
    # uuid-keyed rows for members are reachable only because membership was captured while the
    # graph could still be asked.
    assert _revision_values(db, "m-1") == [(None, None)]
    assert _revision_values(db, "m-2") == [(None, None)]

    subjects = {
        (r["subject_type"], r["subject_value"])
        for r in coord.subjects.fetch_subjects(out["op_id"])
    }
    assert ("NAMESPACE", "ns-1") in subjects
    assert ("NODE_UUID", "m-1") in subjects


def test_namespace_request_json_does_not_enumerate_members(tmp_path):
    """Membership belongs in the inventory table, not in request_json.

    A namespace erase must stay bounded, so the journal row records a count while the
    normalized subject rows carry the identities.
    """
    adapter = FakeAdapter(present=True, members=[f"m-{i}" for i in range(25)])
    coord = _coordinator(tmp_path, adapter)

    out = coord.erase_namespace("ns-big")

    with sqlite3.connect(tmp_path / "t.db") as conn:
        request_json = conn.execute(
            "SELECT request_json FROM graph_operations WHERE op_id = ?", (out["op_id"],)
        ).fetchone()[0]
    assert "m-7" not in request_json
    assert '"member_count":25' in request_json.replace(" ", "")


def test_dry_run_reports_without_mutating(tmp_path):
    adapter = FakeAdapter(present=True)
    coord = _coordinator(tmp_path, adapter)
    db = tmp_path / "t.db"
    _seed_revision(db, "n-1")

    out = coord.erase_memory("n-1", dry_run=True)

    assert out["dry_run"] is True
    assert out["would_purge"]["memory_revisions.old_value"] == 1
    assert adapter.deleted_nodes == []
    assert _revision_values(db, "n-1") == [("secret", "secret")]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM graph_operations").fetchone()[0] == 0


def test_blank_subject_is_a_noop(tmp_path):
    coord = _coordinator(tmp_path, FakeAdapter(present=True))
    assert coord.erase_memory("  ")["reason"] == NOTHING_TO_ERASE
    assert coord.erase_namespace("")["reason"] == NOTHING_TO_ERASE


def test_erasure_is_idempotent(tmp_path):
    """Re-erasing the same subject must not fail; the second pass simply finds nothing."""
    coord = _coordinator(tmp_path, FakeAdapter(present=True))
    db = tmp_path / "t.db"
    _seed_revision(db, "n-1")

    first = coord.erase_memory("n-1")
    second = coord.erase_memory("n-1")

    assert first["reason"] == ERASED
    assert second["reason"] in (ERASED, GRAPH_ALREADY_ABSENT)
    assert _revision_values(db, "n-1") == [(None, None)]


def test_two_party_merge_recovery_is_erased_for_either_side(tmp_path):
    """A merge snapshot must not survive erasure of either participant.

    merge_audit.snapshot_json is NOT NULL, so the purge redacts it to an empty string; the
    content is gone either way while the audit row keeps its shape.
    """
    coord = _coordinator(tmp_path, FakeAdapter(present=True))
    db = tmp_path / "t.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO merge_audit "
            "(recorded_at, survivor_uuid, absorbed_uuid, similarity, snapshot_json) "
            "VALUES (?,?,?,?,?)",
            ("t", "surv-1", "abs-1", 0.9, '{"content":"secret"}'),
        )
        conn.commit()

    coord.erase_memory("abs-1")

    with sqlite3.connect(db) as conn:
        snapshot = conn.execute(
            "SELECT snapshot_json FROM merge_audit WHERE absorbed_uuid = ?", ("abs-1",)
        ).fetchone()[0]
    assert "secret" not in (snapshot or "")


def test_crashed_erasure_is_resumed_from_the_inventory(tmp_path):
    """The reason the inventory exists: resume without asking the graph anything.

    Simulates a crash after PREPARE by journaling the intent and subjects, then leaving the row
    PREPARED. Replay must purge the recorded subjects and commit, using only the inventory --
    the namespace members are deliberately absent from request_json.
    """
    adapter = FakeAdapter(present=True, members=["m-1"])
    coord = _coordinator(tmp_path, adapter)
    db = tmp_path / "t.db"
    _seed_revision(db, "m-1", "survived-the-crash")

    op_id = "crashed-op"
    with sqlite3.connect(db) as conn:
        coord.journal.prepare(
            operation_kind="EXPLICIT_ERASURE",
            request_json='{"namespace":"ns-1","member_count":1,"targets":[]}',
            target_key="erasure:namespace:ns-1",
            op_id=op_id,
            conn=conn,
        )
        coord.subjects.record_subjects(
            op_id, [("NAMESPACE", "ns-1"), ("NODE_UUID", "m-1")], conn=conn
        )
        conn.commit()

    outcome, diagnostics = coord.replay_prepared_row({"op_id": op_id})

    assert outcome == "REPLAYED"
    assert _revision_values(db, "m-1") == [(None, None)]
    with sqlite3.connect(db) as conn:
        state = conn.execute(
            "SELECT state FROM graph_operations WHERE op_id = ?", (op_id,)
        ).fetchone()[0]
    assert state == "COMMITTED"
    assert coord.subjects.count_unpurged(op_id) == 0


def test_replay_of_an_already_purged_row_just_commits(tmp_path):
    """Purge finished but the commit did not land: nothing to erase, only a state to settle."""
    coord = _coordinator(tmp_path, FakeAdapter(present=True))
    db = tmp_path / "t.db"
    op_id = "done-but-uncommitted"
    with sqlite3.connect(db) as conn:
        coord.journal.prepare(
            operation_kind="EXPLICIT_ERASURE",
            request_json='{"targets":["n-9"]}',
            target_uuid="n-9",
            op_id=op_id,
            conn=conn,
        )
        coord.subjects.record_subjects(op_id, [("NODE_UUID", "n-9")], conn=conn)
        conn.commit()
    coord.subjects.mark_purged(op_id)

    outcome, _ = coord.replay_prepared_row({"op_id": op_id})

    assert outcome == "SKIPPED"
    with sqlite3.connect(db) as conn:
        state = conn.execute(
            "SELECT state FROM graph_operations WHERE op_id = ?", (op_id,)
        ).fetchone()[0]
    assert state == "COMMITTED"


def test_erasure_kind_is_registered_so_recovery_never_blocks_boot(tmp_path):
    """An unmapped kind reports UNKNOWN_KIND and blocks write-readiness.

    With live recovery armed that means refusing to start, so the erasure handler must be
    registered by the same default wiring startup uses.
    """
    from menhir.services.saga_reconcile_dispatcher import build_handlers

    handlers = build_handlers(erasure=_coordinator(tmp_path, FakeAdapter()))
    assert "EXPLICIT_ERASURE" in handlers


def test_in_flight_erasure_suppresses_the_archive_read(tmp_path):
    """The window CF-165 named: content readable after the graph node is gone.

    get_original_content is keyed on node_uuid alone with no graph-existence check, so it
    answers for a deleted node. Between a committed erasure intent and a finished purge the
    row still holds the text, so the veto -- not the purge -- is what closes that window.
    """
    from menhir.infrastructure.telemetry.recall_store import TelemetryRecallStoreMixin  # noqa: F401

    coord = _coordinator(tmp_path, FakeAdapter(present=True))
    db = tmp_path / "t.db"
    _seed_revision(db, "n-live", "still-here")
    store = McpTelemetryStore(db_path=db)

    # Readable before any erasure exists.
    assert store.get_original_content("n-live") == "still-here"

    # Intent committed, purge deliberately NOT run: this is the in-flight window.
    coord.subjects.record_subjects("op-x", [("NODE_UUID", "n-live")])

    assert store.get_original_content("n-live") is None
    # The row is genuinely still there -- suppression is doing the work, not deletion.
    assert _revision_values(db, "n-live") == [("still-here", "still-here")]

    # Once the purge completes the subject stops being suppressed, and the content is gone
    # for real rather than merely hidden.
    coord.subjects.mark_purged("op-x")
    assert store.get_original_content("n-live") == "still-here"


def test_completed_erasure_leaves_nothing_for_the_archive_read(tmp_path):
    """End to end: after a real erasure the archive read returns nothing because the content
    is gone, not because it is being hidden."""
    coord = _coordinator(tmp_path, FakeAdapter(present=True))
    db = tmp_path / "t.db"
    _seed_revision(db, "n-erased", "secret")
    store = McpTelemetryStore(db_path=db)

    coord.erase_memory("n-erased")

    assert store.get_original_content("n-erased") is None
    assert _revision_values(db, "n-erased") == [(None, None)]


def test_every_sidecar_content_reader_honours_the_veto(tmp_path):
    """All four readers that can return memory text must suppress an in-flight erasure.

    Each is keyed on a uuid with no graph-existence check, so each answers for a node that is
    already deleted. Installing the veto on one of them would only narrow the window.
    """
    coord = _coordinator(tmp_path, FakeAdapter(present=True))
    db = tmp_path / "t.db"
    store = McpTelemetryStore(db_path=db)
    _seed_revision(db, "n-hot", "still-readable")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO merge_audit "
            "(recorded_at, survivor_uuid, absorbed_uuid, similarity, snapshot_json) "
            "VALUES (?,?,?,?,?)",
            ("t", "surv-hot", "n-hot", 0.9, '{"content":"absorbed secret"}'),
        )
        conn.commit()

    # Visible before any erasure intent exists.
    assert store.get_original_content("n-hot") == "still-readable"
    assert any(r["node_uuid"] == "n-hot" for r in store.fold_revisions_by_field(min_count=1))
    assert store.fold_revisions_bounded()["groups"]
    assert store.fetch_merge_audit(absorbed_uuid="n-hot")

    # Intent committed, purge deliberately not run.
    coord.subjects.record_subjects("op-hot", [("NODE_UUID", "n-hot")])

    assert store.get_original_content("n-hot") is None
    assert not [r for r in store.fold_revisions_by_field(min_count=1) if r["node_uuid"] == "n-hot"]
    assert not [
        g for g in store.fold_revisions_bounded()["groups"] if g["node_uuid"] == "n-hot"
    ]
    # Two-party: erasing the ABSORBED side must hide the row the survivor also points at.
    assert store.fetch_merge_audit(absorbed_uuid="n-hot") == []
    assert store.fetch_merge_audit(survivor_uuid="surv-hot") == []

    # The rows are all still present -- suppression, not deletion, is doing the work.
    assert _revision_values(db, "n-hot") == [("still-readable", "still-readable")]


def test_erased_merge_snapshot_reads_as_erased_not_corrupt(tmp_path):
    """The plan's requirement: erasure must be distinguishable from corruption.

    An empty string made the parser fail exactly as it does on a malformed row, sending an
    operator after a data-integrity bug that does not exist.
    """
    from menhir.domain.legacy_snapshot import LegacySnapshotError, SnapshotErasedError, parse

    coord = _coordinator(tmp_path, FakeAdapter(present=True))
    db = tmp_path / "t.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO merge_audit "
            "(recorded_at, survivor_uuid, absorbed_uuid, similarity, snapshot_json) "
            "VALUES (?,?,?,?,?)",
            ("t", "surv-p", "abs-p", 0.9, '{"content":"secret"}'),
        )
        conn.commit()

    coord.erase_memory("abs-p")

    with sqlite3.connect(db) as conn:
        snapshot = conn.execute(
            "SELECT snapshot_json FROM merge_audit WHERE absorbed_uuid = ?", ("abs-p",)
        ).fetchone()[0]

    assert "secret" not in snapshot
    with pytest.raises(SnapshotErasedError):
        parse(snapshot)
    # A genuinely corrupt row still reports as malformed, so the two stay separable.
    with pytest.raises(LegacySnapshotError) as corrupt:
        parse("{not json")
    assert not isinstance(corrupt.value, SnapshotErasedError)


def test_backend_reports_which_erasure_outcome_occurred(tmp_path, monkeypatch):
    """The distinction a bool cannot carry, end to end through the backend.

    graph_already_absent means the node was gone but content WAS erased. Collapsing that into
    "not found" tells the caller nothing happened, when in fact their content was destroyed.
    """
    import asyncio

    from menhir.core.backend_runtime_data_ops import RuntimeProviderDataOpsMixin

    class Provider(RuntimeProviderDataOpsMixin):
        def __init__(self, coord):
            self._erasure_coordinator = coord

        async def _off_loop(self, fn, *args, **kwargs):
            return fn(*args, **kwargs)

    db = tmp_path / "t.db"
    McpTelemetryStore(db_path=db)._ensure_ready()
    coord = ErasureCoordinator(
        graph_adapter=FakeAdapter(present=False),
        journal=GraphOperationsJournal(db_path=db),
        subjects=ErasureSubjectStore(db_path=db),
    )
    provider = Provider(coord)
    _seed_revision(db, "n-absent", "residue")

    outcome = asyncio.run(provider.erase_memory("n-absent"))
    assert outcome["reason"] == GRAPH_ALREADY_ABSENT
    assert outcome["purged"]["memory_revisions.old_value"] == 1

    # delete_memory still answers True: something WAS erased, so "not found" would be a lie.
    assert asyncio.run(provider.delete_memory("n-absent")) is True

    # And a subject with nothing anywhere is the genuine negative.
    assert asyncio.run(provider.delete_memory("n-never-existed")) is False
    assert asyncio.run(provider.erase_memory("n-never-existed"))["reason"] == NOTHING_TO_ERASE
