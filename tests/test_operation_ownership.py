"""CF-20b: per-operation ownership metadata and the exhaustive PREPARED scan.

Two independent mechanisms are under test.

Ownership answers "is the process that PREPARED this row still executing it?", which is the one
question a reconciliation lease cannot answer -- the racing writer is not a reconciler. Every
ambiguous case must resolve to OWNER_UNKNOWN rather than ABANDONED, because reading a live writer
as abandoned is what produces a double-apply.

The exhaustive scan removes the deterministic starvation in ``list_by_state(limit=500)``: a row
that never leaves PREPARED pins the oldest page forever and hides every newer row behind it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from menhir.infrastructure import operation_owner as oo
from menhir.infrastructure import process_liveness
from menhir.infrastructure.graph_operations import (
    GraphOperationsJournal,
    GraphOperationError,
)

# The column set as it existed BEFORE the CF-20b ownership migration.
_PRE_MIGRATION_COLUMNS = """
    op_id                 TEXT PRIMARY KEY,
    batch_id              TEXT,
    operation_kind        TEXT NOT NULL,
    target_uuid           TEXT,
    target_key            TEXT,
    request_json          TEXT NOT NULL,
    before_snapshot_json  TEXT,
    expected_after_sha256 TEXT,
    state                 TEXT NOT NULL,
    attempt_count         INTEGER NOT NULL DEFAULT 0,
    last_error            TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    committed_at          TEXT,
    reverses_op_id        TEXT
"""


@pytest.fixture()
def journal(tmp_path):
    j = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    j._ensure_ready()
    return j


def _prepare(journal, op_id, **kw):
    return journal.prepare(
        operation_kind="ENTITY_DELETE",
        request_json=json.dumps({"op_id": op_id, "targets": [f"n-{op_id}"]}),
        target_key=f"key-{op_id}",
        op_id=op_id,
        **kw,
    )


# --------------------------------------------------------------------------- token identity


@pytest.mark.unit
def test_owner_token_is_unique_when_instance_id_is_unset(monkeypatch):
    """MENHIR_INSTANCE_ID defaults to "" and is routinely unset.

    A token derived from it alone would be identical in every process, which is exactly the
    collision that would let one process claim another's live operation.
    """
    monkeypatch.delenv("MENHIR_INSTANCE_ID", raising=False)
    token = oo.process_owner_token()

    assert token.count(":") == 3, "token must carry instance, host, pid and nonce"
    label, host, pid, nonce = token.split(":")
    assert label == "instance-unset", "an unset instance must be named, not blank"
    assert host, "the hostname is what makes the PID usable as death evidence"
    assert pid.isdigit() and nonce, "pid and nonce must both be populated"


@pytest.mark.unit
def test_owner_token_is_stable_within_a_process():
    assert oo.process_owner_token() == oo.process_owner_token()


@pytest.mark.unit
def test_owner_token_uses_instance_label_when_set(monkeypatch):
    monkeypatch.setenv("MENHIR_INSTANCE_ID", "menhir-prod-1")
    assert oo.process_owner_token().startswith("menhir-prod-1:")


# --------------------------------------------------------------------------- classification


@pytest.mark.unit
def test_fresh_claim_classifies_as_live_owner():
    row = {
        "owner_token": "someone-else:host:99:abc",
        "owner_lease_expires_at": oo.lease_expiry_iso(seconds=300),
    }
    assert oo.classify_ownership(row) == oo.LIVE_OWNER


@pytest.mark.unit
def test_expired_claim_classifies_as_abandoned():
    # Built from an explicitly old `now`, not a negative lease: lease_expiry_iso clamps the
    # duration to >= 1s on purpose, so a negative value still yields a FUTURE expiry.
    long_ago = datetime(2020, 1, 1, tzinfo=timezone.utc)
    row = {
        # Same host, PID 999999 is not running -> death provable by inspection.
        "owner_token": f"someone-else:{process_liveness.hostname()}:999999:abc",
        "owner_lease_expires_at": oo.lease_expiry_iso(seconds=60, now=long_ago),
    }
    assert oo.classify_ownership(row) == oo.ABANDONED


@pytest.mark.unit
def test_a_nonpositive_lease_is_never_minted_already_dead():
    """A clamped-to-1s claim still reads live for a moment, rather than inviting instant replay."""
    row = {"owner_token": "t:h:1:a", "owner_lease_expires_at": oo.lease_expiry_iso(seconds=0)}
    assert oo.classify_ownership(row) == oo.LIVE_OWNER


@pytest.mark.unit
def test_naive_expiry_is_read_as_utc_not_crashed_on():
    """Every writer here stores aware UTC, so a naive value means a legacy or hand-edited row."""
    row = {
        "owner_token": f"t:{process_liveness.hostname()}:999999:a",
        "owner_lease_expires_at": "2020-01-01T00:00:00",
    }
    assert oo.classify_ownership(row) == oo.ABANDONED


@pytest.mark.unit
@pytest.mark.parametrize(
    "row, why",
    [
        ({}, "no ownership fields at all"),
        ({"owner_token": None}, "explicit null token (a legacy row)"),
        ({"owner_token": "   "}, "blank token"),
        ({"owner_token": "t:h:1:a"}, "token but no lease expiry to compare against"),
        ({"owner_token": "t:h:1:a", "owner_lease_expires_at": "not-a-date"}, "unparseable expiry"),
        ({"owner_token": "t:h:1:a", "owner_lease_expires_at": ""}, "empty expiry"),
        (None, "not a row at all"),
    ],
)
def test_unprovable_liveness_fails_closed_to_owner_unknown(row, why):
    """None of these may be read as ABANDONED: we cannot prove the writer is gone.

    Ownerless rows matter most. During a mixed-version rollout an older binary with no ownership
    support may still be running and may still own the row, so "ownerless" must never be
    shorthand for "safe to replay".
    """
    assert oo.classify_ownership(row) == oo.OWNER_UNKNOWN, why


@pytest.mark.unit
def test_is_own_claim_distinguishes_this_process_from_another():
    mine = {"owner_token": oo.process_owner_token()}
    theirs = {"owner_token": "other-instance:h:1:deadbeef"}
    assert oo.is_own_claim(mine) is True
    assert oo.is_own_claim(theirs) is False


# --------------------------------------------------------------------------- journal integration


@pytest.mark.unit
def test_prepare_stamps_a_live_claim(journal):
    _prepare(journal, "op-1")
    row = journal.get("op-1")

    assert row["owner_token"] == oo.process_owner_token()
    assert row["owner_heartbeat_at"], "heartbeat must be stamped at PREPARE"
    assert oo.classify_ownership(row) == oo.LIVE_OWNER, (
        "a just-prepared row must look live, or recovery would replay work in flight"
    )


@pytest.mark.unit
def test_leaving_prepared_retires_the_claim(journal):
    _prepare(journal, "op-1")
    journal.mark_committed("op-1")
    row = journal.get("op-1")

    assert row["owner_token"] is None
    assert row["owner_lease_expires_at"] is None
    assert oo.classify_ownership(row) == oo.OWNER_UNKNOWN


@pytest.mark.unit
def test_quarantining_also_retires_the_claim(journal):
    """NEEDS_REVIEW keeps its participant locks but must NOT keep a live-looking heartbeat.

    The fence and the claim answer different questions; a quarantined row has no writer in
    flight, and leaving the claim would make it read LIVE_OWNER indefinitely.
    """
    _prepare(journal, "op-1")
    journal.mark_needs_review("op-1", observed_error="drift")
    row = journal.get("op-1")

    assert row["state"] == "NEEDS_REVIEW"
    assert row["owner_token"] is None
    locks = sqlite3.connect(journal.db_path).execute(
        "SELECT COUNT(*) FROM graph_operation_locks WHERE op_id = ?", ("op-1",)
    ).fetchone()[0]
    assert locks == 1, "the participant fence must still be held while under review"


@pytest.mark.unit
def test_heartbeat_renewal_extends_our_own_claim(journal):
    _prepare(journal, "op-1")
    before = journal.get("op-1")["owner_lease_expires_at"]

    assert journal.renew_owner_heartbeat("op-1", seconds=600) is True
    after = journal.get("op-1")["owner_lease_expires_at"]
    assert after > before, "renewal must push the expiry out"


@pytest.mark.unit
def test_heartbeat_renewal_fails_when_the_claim_is_not_ours(journal):
    """Long-running saga code must be able to discover it LOST its claim and stop.

    Returning True here would let a writer keep mutating while believing it still owns work
    another process may already be replaying.
    """
    _prepare(journal, "op-1", owner_token="other-instance:h:1:deadbeef")
    assert journal.renew_owner_heartbeat("op-1") is False


@pytest.mark.unit
def test_heartbeat_renewal_fails_once_the_row_is_no_longer_prepared(journal):
    _prepare(journal, "op-1")
    journal.mark_committed("op-1")
    assert journal.renew_owner_heartbeat("op-1") is False


# --------------------------------------------------------------------------- migration


@pytest.mark.unit
def test_existing_database_without_owner_columns_is_migrated(tmp_path):
    """CREATE TABLE IF NOT EXISTS does nothing to an existing table.

    The live sidecar predates this fence, so without an explicit ALTER every ownership read
    would raise against it.
    """
    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as conn:
        conn.execute(f"CREATE TABLE graph_operations ({_PRE_MIGRATION_COLUMNS})")
        conn.execute(
            "INSERT INTO graph_operations (op_id, operation_kind, request_json, state, "
            "created_at, updated_at) VALUES ('legacy-1', 'ENTITY_DELETE', '{}', 'PREPARED', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()

    journal = GraphOperationsJournal(db_path=db)
    journal._ensure_ready()

    row = journal.get("legacy-1")
    assert "owner_token" in row, "migration must add the ownership columns"
    assert row["owner_token"] is None, (
        "a pre-existing row must stay ownerless; backfilling a claim would fabricate the very "
        "liveness evidence recovery reasons about"
    )
    assert oo.classify_ownership(row) == oo.OWNER_UNKNOWN


@pytest.mark.unit
def test_migration_is_idempotent(tmp_path):
    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as conn:
        conn.execute(f"CREATE TABLE graph_operations ({_PRE_MIGRATION_COLUMNS})")
        conn.commit()

    for _ in range(3):
        GraphOperationsJournal(db_path=db)._ensure_ready()

    columns = [
        r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(graph_operations)")
    ]
    assert columns.count("owner_token") == 1


# --------------------------------------------------------------------------- exhaustive scan


def _insert_prepared(db_path, op_id, created_at):
    """Insert directly so created_at can be controlled (and made to collide)."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO graph_operations (op_id, operation_kind, request_json, state, "
            "created_at, updated_at) VALUES (?, 'ENTITY_DELETE', '{}', 'PREPARED', ?, ?)",
            (op_id, created_at, created_at),
        )
        conn.commit()


@pytest.mark.unit
def test_iter_by_state_traverses_more_than_the_old_500_row_horizon(journal):
    """The plan's bar: >500 PREPARED rows must all be reached, with the DEFAULT batch size."""
    for i in range(600):
        _insert_prepared(journal.db_path, f"op-{i:04d}", f"2026-01-01T00:00:{i % 60:02d}+00:00")

    seen = [row["op_id"] for row in journal.iter_by_state("PREPARED")]

    assert len(seen) == 600
    assert len(set(seen)) == 600, "no row may be yielded twice"
    assert len(journal.list_by_state("PREPARED")) == 500, (
        "control: the old horizon really does stop at 500, so this test is meaningful"
    )


@pytest.mark.unit
def test_iter_by_state_handles_identical_created_at_without_skipping(journal):
    """created_at is not unique, so the keyset needs op_id to be a total order.

    With a page boundary landing inside a group of ties, a created_at-only cursor either skips
    the rest of the tie or loops on it.
    """
    for i in range(10):
        _insert_prepared(journal.db_path, f"op-{i:02d}", "2026-01-01T00:00:00+00:00")

    seen = [row["op_id"] for row in journal.iter_by_state("PREPARED", batch_size=3)]

    assert len(seen) == 10 and len(set(seen)) == 10
    assert seen == sorted(seen), "ties must still yield in a deterministic total order"


@pytest.mark.unit
def test_a_permanently_stuck_row_does_not_hide_newer_rows(journal):
    """The starvation CF-20 exists to remove.

    The oldest row here is never resolved. Re-querying ``list_by_state`` would return it forever
    and never reach the newer rows; the cursor must step past it.
    """
    _insert_prepared(journal.db_path, "op-stuck", "2026-01-01T00:00:00+00:00")
    for i in range(5):
        _insert_prepared(journal.db_path, f"op-new-{i}", f"2026-06-01T00:00:0{i}+00:00")

    seen = [row["op_id"] for row in journal.iter_by_state("PREPARED", batch_size=1)]

    assert seen[0] == "op-stuck"
    assert len(seen) == 6, "every newer row must still be reached"


@pytest.mark.unit
def test_iter_by_state_rejects_an_unknown_state(journal):
    with pytest.raises(GraphOperationError):
        list(journal.iter_by_state("NOT_A_STATE"))


@pytest.mark.unit
def test_iter_by_state_is_empty_on_a_clean_journal(journal):
    assert list(journal.iter_by_state("PREPARED")) == []


@pytest.mark.unit
def test_an_expired_remote_owner_is_owner_unknown_not_abandoned():
    """The core of the redesign: expiry alone no longer authorises recovery.

    The previous rule read "lease expired" as "writer is gone", which depended on an unproven
    premise -- that an already-dispatched graph mutation must have returned within a bounded time.
    A remote PID cannot be inspected, so death is unprovable and the row must fence.
    """
    row = {
        "owner_token": "inst:some-other-host:4242:nonce",
        "owner_lease_expires_at": "2020-01-01T00:00:00+00:00",
    }
    assert oo.classify_ownership(row) == oo.OWNER_UNKNOWN


@pytest.mark.unit
def test_an_expired_local_owner_whose_pid_is_alive_is_owner_unknown():
    """A recycled PID reads as alive, which is the SAFE direction: cannot prove death."""
    import os

    row = {
        "owner_token": f"inst:{process_liveness.hostname()}:{os.getpid()}:nonce",
        "owner_lease_expires_at": "2020-01-01T00:00:00+00:00",
    }
    assert oo.classify_ownership(row) == oo.OWNER_UNKNOWN


@pytest.mark.unit
def test_an_operator_attestation_outranks_the_clock():
    """Independent evidence is the sanctioned path for an owner this process cannot inspect."""
    row = {
        "owner_token": "inst:some-other-host:4242:nonce",
        "owner_lease_expires_at": "2020-01-01T00:00:00+00:00",
        "owner_death_attested_by": "ctharvey",
    }
    assert oo.classify_ownership(row) == oo.ABANDONED


@pytest.mark.unit
def test_a_fresh_lease_still_vetoes_even_with_an_attestation_absent():
    row = {
        "owner_token": "inst:any-host:1:nonce",
        "owner_lease_expires_at": oo.lease_expiry_iso(seconds=300),
    }
    assert oo.classify_ownership(row) == oo.LIVE_OWNER
