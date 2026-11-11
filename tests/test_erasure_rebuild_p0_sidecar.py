"""P0 counterexample tests for the erasure rebuild, lane A (sidecar and unit level).

Plan: projects/archolith/.agent/plans/menhir-erasure-rebuild-plan-v2-2026-11-10.md (section 6).
Every test is ``xfail(strict=True, raises=AssertionError)`` against e574abdf. Each later PR removes
the marker for the tests it fixes. Entry points that do not exist yet are looked up with
``getattr`` / ``importlib.util.find_spec`` and asserted, never imported. Today's exceptions are
caught by exact class and re-raised as ``AssertionError``.

Real SQLite under ``tmp_path``; a fake graph adapter stands in for Neo4j.
"""

from __future__ import annotations

import importlib
import importlib.util
import sqlite3
import uuid
from pathlib import Path

import pytest

from menhir.infrastructure.erasure_subjects import ErasureSubjectStore
from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.telemetry.recorders import record_failure_event
from menhir.infrastructure.telemetry.store import McpTelemetryStore
from menhir.services.erasure_coordinator import (
    DELETION_SUCCEEDED_REASONS,
    ErasureCoordinator,
)

pytestmark = [pytest.mark.unit]

P0 = pytest.mark.xfail(strict=True, raises=AssertionError)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _id() -> str:
    return str(uuid.uuid4())


class FakeAdapter:
    """Graph side: present targets delete cleanly; optional twins, derived uuids and failures."""

    def __init__(self, *, twins=None, derived_on=None, fail_on=None, members=None) -> None:
        self.twins = list(twins or [])
        self.derived_on = dict(derived_on or {})  # target -> derived entity uuids it deletes
        self.fail_on = set(fail_on or ())
        self.members = list(members or [])
        self.deleted: list[str] = []

    def episodic_twin_uuids(self, node_uuid):
        return list(self.twins)

    def delete_memory_detailed(self, node_uuid, *, erased_uuids=None, deletable_entity_uuids=None,
                               **_kw):
        if node_uuid in self.fail_on:
            raise RuntimeError("graph delete failed mid-saga")
        self.deleted.append(node_uuid)
        derived = self.derived_on.pop(node_uuid, [])
        return {"touched": True, "derived_entities_deleted_uuids": derived}

    def delete_memory(self, node_uuid):
        return True

    def capture_namespace_uuids(self, group_id, namespace=None):
        return list(self.members)

    def delete_namespace(self, group_id, namespace=None):
        return 1


def _coordinator(tmp_path, adapter) -> tuple[ErasureCoordinator, Path]:
    db = tmp_path / "t.db"
    McpTelemetryStore(db_path=db)._ensure_ready()
    coord = ErasureCoordinator(
        graph_adapter=adapter,
        journal=GraphOperationsJournal(db_path=db),
        subjects=ErasureSubjectStore(db_path=db),
    )
    return coord, db


def _seed_failure(db: Path, episode_uuid: str, text: str) -> None:
    McpTelemetryStore(db_path=db).record_failure(
        recorded_at="2026-01-01T00:00:00+00:00",
        operation="enrich",
        episode_uuid=episode_uuid,
        failure_stage="publish",
        classification="x",
        retryable=False,
        processing_attempt=1,
        queue_depth=None,
        worker_id=None,
        error_type="E",
        error=text,
        details_json=None,
    )


def _failure_rows(db: Path, episode_uuid: str) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            "SELECT id, error FROM failure_events WHERE episode_uuid = ?", (episode_uuid,)
        ).fetchall()


def _file_bytes(db: Path) -> bytes:
    out = b""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists():
            out += p.read_bytes()
    return out


# ---------------------------------------------------------------------------------- T7 (unit)


@P0
def test_t7_replay_purges_derived_item_deleted_before_second_delete_raised(tmp_path):
    """Two targets; the first delete removes derived entity X, the second raises; replay must
    purge X's sidecar row. Breaks today because X is discovered only in memory (derived
    ``extra_uuids``) and is lost when the graph delete raises; fixed when deleted uuids are
    durably appended to the op marker inside the deleting statement (A-8 / C-F9)."""
    canary = f"CANARY-T7-{uuid.uuid4()}"
    target, twin, derived_x = _id(), _id(), _id()
    adapter = FakeAdapter(twins=[twin], derived_on={target: [derived_x]}, fail_on={twin})
    coord, db = _coordinator(tmp_path, adapter)
    _seed_failure(db, derived_x, canary)
    assert _failure_rows(db, derived_x)  # precondition: the row exists

    with pytest.raises(RuntimeError):  # the induced mid-saga crash (not AssertionError)
        coord.erase_memory(target)
    assert target in adapter.deleted and derived_x not in adapter.deleted
    with sqlite3.connect(db) as conn:
        (op_id,) = conn.execute(
            "SELECT op_id FROM graph_operations WHERE operation_kind = 'EXPLICIT_ERASURE'"
        ).fetchone()

    adapter.fail_on.clear()  # the replay's graph delete succeeds; X is already gone, so
    # the real graph reports no derived uuids for it any more.
    coord.replay_prepared_row({"op_id": op_id, "operation_kind": "EXPLICIT_ERASURE"})

    rows = _failure_rows(db, derived_x)
    assert not rows, f"derived item's sidecar row survived the replayed erase: {rows!r}"


# ---------------------------------------------------------------------------------- T9


@P0
def test_t9_erase_pending_physical_then_compaction_leaves_no_bytes(tmp_path):
    """Erase -> ``erased_pending_physical``; the scheduled compaction job runs; ``erase_status``
    is still not ``erased`` (no Neo4j step); db, -wal and -shm bytes hold no phrase, including a
    phrase a legacy non-secure connection wrote and deleted (free-page residue). Breaks today:
    the reply is ``erased``, there is no ``erase_status`` and no compaction job (C-F1 / C-F2)."""
    canary = f"CANARY-T9-{uuid.uuid4()}"
    legacy = f"CANARY-T9-LEGACY-{uuid.uuid4()}"
    target = _id()
    coord, db = _coordinator(tmp_path, FakeAdapter())
    _seed_failure(db, target, canary)
    # Content written AND deleted through a plain (non-secure_delete) connection: its bytes sit
    # in a free page until something rewrites the file.
    _seed_failure(db, _id(), legacy * 20)
    with sqlite3.connect(db) as plain:
        plain.execute("DELETE FROM failure_events WHERE error LIKE ?", (f"%{legacy}%",))
        plain.commit()

    reply = coord.erase_memory(target)
    assert reply.get("reason") == "erased_pending_physical", reply.get("reason")

    erase_status = getattr(coord, "erase_status", None)
    assert callable(erase_status), "ErasureCoordinator.erase_status does not exist"
    # Compaction job: module/function name is a guess (the plan names no symbol); see report.
    spec = importlib.util.find_spec("menhir.services.sidecar_compaction")
    assert spec is not None, "scheduled sidecar compaction job module does not exist"
    job = getattr(importlib.import_module("menhir.services.sidecar_compaction"),
                  "run_sidecar_compaction", None)
    assert callable(job), "run_sidecar_compaction does not exist"
    job(db_path=db)

    status = erase_status(reply["op_id"])
    assert status != "erased" and "erased_pending_physical" in str(status), status
    blob = _file_bytes(db)
    assert canary.encode() not in blob, "erased phrase present in sidecar file bytes"
    assert legacy.encode() not in blob, "legacy free-page phrase present after compaction"


# ---------------------------------------------------------------------------------- T10


def test_t10_late_failure_event_after_commit_is_not_stored(tmp_path):
    """A failure event for an erased episode recorded after COMMITTED must not persist.
    Breaks today: nothing denylists erased subjects, so the late writer inserts the row."""
    canary = f"CANARY-T10-{uuid.uuid4()}"
    target = _id()
    coord, db = _coordinator(tmp_path, FakeAdapter())
    reply = coord.erase_memory(target)
    with sqlite3.connect(db) as conn:
        (state,) = conn.execute(
            "SELECT state FROM graph_operations WHERE op_id = ?", (reply["op_id"],)
        ).fetchone()
    assert state == "COMMITTED", state

    record_failure_event(
        operation="enrich", error=canary, episode_uuid=target, processing_attempt=1,
        store=McpTelemetryStore(db_path=db),
    )

    rows = _failure_rows(db, target)
    assert not rows, f"late writer re-created content for an erased subject: {rows!r}"


# ---------------------------------------------------------------------------------- T17


def test_t17_namespace_erase_with_40k_members_purges_and_succeeds(tmp_path):
    """Namespace erase with 40k captured members must purge and report success. Breaks today if
    the purge binds one SQL variable per member (C-F11); the SQLite error is re-raised as an
    AssertionError."""
    canary = f"CANARY-T17-{uuid.uuid4()}"
    members = [_id() for _ in range(40_000)]
    coord, db = _coordinator(tmp_path, FakeAdapter(members=members))
    _seed_failure(db, members[-1], canary)

    try:
        reply = coord.erase_namespace("t17-ns", namespace="t17-ns")
    except sqlite3.OperationalError as exc:
        raise AssertionError(f"namespace erase raised {exc!r}") from exc

    assert reply.get("reason") in DELETION_SUCCEEDED_REASONS, reply
    # The purge blanks content in place (rows are kept); the canary must be gone.
    leftover = [r for r in _failure_rows(db, members[-1]) if r[1] and canary in str(r[1])]
    assert not leftover, f"member's sidecar content was not purged: {leftover!r}"


# ---------------------------------------------------------------------------------- T24


def test_t24_erase_own_journal_updates_survive_content_triggers(tmp_path):
    """Content-table triggers must exist (generated from CONTENT_COLUMNS), none on the journal
    tables, and an erase of a denylisted target must still drive its own graph_operations row to
    COMMITTED. Breaks today: no denylist and no triggers exist (Adv F3)."""
    target = _id()
    coord, db = _coordinator(tmp_path, FakeAdapter())
    reply = coord.erase_memory(target)

    with sqlite3.connect(db) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        triggers = conn.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
        state = conn.execute(
            "SELECT state FROM graph_operations WHERE op_id = ?", (reply["op_id"],)
        ).fetchone()[0]
        subject_rows = conn.execute(
            "SELECT COUNT(*) FROM graph_operation_subjects WHERE op_id = ?", (reply["op_id"],)
        ).fetchone()[0]
    assert "erased_subject_denylist" in tables, "no erased_subject_denylist table"
    trigger_tables = {t for _n, t in triggers}
    assert "failure_events" in trigger_tables, f"no content-table trigger: {triggers!r}"
    assert not trigger_tables & {"graph_operations", "graph_operation_subjects"}, triggers
    assert state == "COMMITTED", state  # the op's own updates were not dropped
    assert subject_rows > 0  # the journal's own subject rows were not dropped either


# ---------------------------------------------------------------------------------- T14


@P0
def test_t14_text_inventory_lint():
    """The Neo4j text inventory must exist and the lint invariants hold. Breaks today on the
    find_spec assert: ``graph_text_inventory`` does not exist. The checks after it (no DELETE of
    EvidenceNamespaceFence, triggers per content table and none on journals) are the lint parts
    expressible without the new inventory API; G-q/G-copy/G-create coverage and the
    erasure_epoch-only gate check need that API and are not expressed (see report)."""
    spec = importlib.util.find_spec("menhir.infrastructure.graph_text_inventory")
    assert spec is not None, "menhir.infrastructure.graph_text_inventory does not exist"

    offenders = []
    for root in (REPO_ROOT / "src" / "menhir", REPO_ROOT / "scripts"):
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "EvidenceNamespaceFence" in text and "DELETE" in text:
                for line in text.splitlines():
                    if "EvidenceNamespaceFence" in line and "DELETE" in line.upper():
                        offenders.append(f"{path}: {line.strip()}")
    assert not offenders, offenders

    from menhir.infrastructure.telemetry import erasure_inventory

    content_tables = {c.table for c in getattr(erasure_inventory, "CONTENT_COLUMNS", ())}
    assert content_tables, "CONTENT_COLUMNS not readable"
