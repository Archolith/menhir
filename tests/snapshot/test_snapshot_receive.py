"""P2A staging receiver: ownership, bounds, replay, quotas, TTL, restart.

Plan: `.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md`.

P2A's gate is "adversarial bundles can consume only configured resources; no archive is extracted
and no graph operation is reachable." These tests are written as the adversary where they can be:
a caller who guesses an id, replays a chunk with different content, lies about a length, opens
more uploads than allowed, or dies mid-transfer and comes back.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from menhir.snapshot.protocol import SnapshotLimits, sha256_hex
from menhir.snapshot.receive import (
    ERR_CHUNK_CONFLICT,
    ERR_CHUNK_DIGEST,
    ERR_CHUNK_ENCODING,
    ERR_CHUNK_INDEX,
    ERR_CHUNK_LENGTH,
    ERR_DISK_BUDGET,
    ERR_NOT_FOUND,
    ERR_PROJECT_IDENTITY,
    ERR_SIZE,
    ERR_STAGING_LOST,
    ERR_TOO_MANY_PER_PRINCIPAL,
    ERR_TOO_MANY_PER_PROJECT,
    ERR_WRONG_STATE,
    ReceiveError,
    StagingQuotas,
    StagingReceiver,
    UploadState,
)

pytestmark = pytest.mark.unit

_LIMITS = SnapshotLimits(chunk_bytes=8, max_chunk_bytes=16, max_compressed_bytes=1024)


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def receiver(tmp_path: Path, clock: FakeClock) -> StagingReceiver:
    return StagingReceiver(tmp_path / "staging", limits=_LIMITS, clock=clock)


def _chunk(receiver: StagingReceiver, record, index: int, data: bytes):
    return receiver.put_chunk(
        upload_id=record.upload_id,
        principal=record.principal,
        index=index,
        data_b64=base64.b64encode(data).decode("ascii"),
        declared_len=len(data),
        digest=sha256_hex(data),
    )


def _begin(receiver: StagingReceiver, *, principal="alice", project="proj", size=16):
    return receiver.begin(principal=principal, project_key=project, declared_bytes=size)


# --- the happy path, and where it stops ---------------------------------------------------------


def test_a_complete_upload_reaches_sealed_and_stops_there(receiver: StagingReceiver) -> None:
    """SEALED is the end of P2A. Extraction and the graph are P3's, and must be unreachable."""
    record = _begin(receiver, size=16)
    assert record.state is UploadState.RECEIVING
    assert record.total_chunks == 2

    record = _chunk(receiver, record, 0, b"01234567")
    assert record.state is UploadState.RECEIVING
    record = _chunk(receiver, record, 1, b"89abcdef")

    assert record.state is UploadState.SEALED
    assert record.received_bytes == 16
    assert record.missing == []
    assert not hasattr(receiver, "extract")
    assert not hasattr(receiver, "commit")


def test_chunks_land_at_their_offsets_regardless_of_arrival_order(receiver: StagingReceiver) -> None:
    record = _begin(receiver, size=16)
    _chunk(receiver, record, 1, b"second--")
    record = _chunk(receiver, record, 0, b"first---")

    blob = (receiver.root / record.upload_id / "blob").read_bytes()
    assert blob == b"first---second--"


def test_status_reports_what_is_missing(receiver: StagingReceiver) -> None:
    record = _begin(receiver, size=24)
    _chunk(receiver, record, 1, b"bbbbbbbb")

    status = receiver.status(upload_id=record.upload_id, principal="alice")

    assert status.missing == [0, 2]
    assert status.received_bytes == 8


# --- ownership ----------------------------------------------------------------------------------


def test_another_principal_cannot_see_or_touch_an_upload(receiver: StagingReceiver) -> None:
    """Invariant 2: the id is a token, not authorization, and is re-checked at every load."""
    record = _begin(receiver, principal="alice")

    for call in (
        lambda: receiver.status(upload_id=record.upload_id, principal="mallory"),
        lambda: receiver.abort(upload_id=record.upload_id, principal="mallory"),
        lambda: receiver.put_chunk(
            upload_id=record.upload_id, principal="mallory", index=0,
            data_b64=base64.b64encode(b"x").decode(), declared_len=1, digest=sha256_hex(b"x"),
        ),
    ):
        with pytest.raises(ReceiveError) as excinfo:
            call()
        assert excinfo.value.code == ERR_NOT_FOUND


def test_a_foreign_upload_is_not_found_rather_than_forbidden(receiver: StagingReceiver) -> None:
    """"Exists but is not yours" is itself information; an id is not worth confirming."""
    record = _begin(receiver, principal="alice")

    with pytest.raises(ReceiveError) as excinfo:
        receiver.status(upload_id=record.upload_id, principal="mallory")

    assert excinfo.value.code == ERR_NOT_FOUND
    assert "forbidden" not in str(excinfo.value).lower()


@pytest.mark.parametrize("bogus", ["../../etc/passwd", "a/b", "..", "", "NOTHEX", "x" * 32])
def test_a_crafted_upload_id_cannot_select_a_path(receiver: StagingReceiver, bogus: str) -> None:
    with pytest.raises(ReceiveError) as excinfo:
        receiver.status(upload_id=bogus, principal="alice")
    assert excinfo.value.code == ERR_NOT_FOUND


# --- bounds -------------------------------------------------------------------------------------


def test_an_oversized_chunk_is_refused_before_it_is_decoded(receiver: StagingReceiver) -> None:
    """Invariant 6: the declared length alone refuses it, with no buffer allocated."""
    record = _begin(receiver, size=16)
    payload = b"x" * 64

    with pytest.raises(ReceiveError) as excinfo:
        receiver.put_chunk(
            upload_id=record.upload_id, principal="alice", index=0,
            data_b64=base64.b64encode(payload).decode(), declared_len=len(payload),
            digest=sha256_hex(payload),
        )

    # Every refusal leaving the receiver is a ReceiveError with a stable code; the protocol
    # layer's own error type must not surface here.
    assert excinfo.value.code == ERR_CHUNK_LENGTH


def test_a_lie_about_the_length_is_caught_after_the_decode(receiver: StagingReceiver) -> None:
    """The declaration is the caller's claim; the decode is the fact. Both are checked."""
    record = _begin(receiver, size=16)
    payload = b"x" * 8

    with pytest.raises(ReceiveError) as excinfo:
        receiver.put_chunk(
            upload_id=record.upload_id, principal="alice", index=0,
            data_b64=base64.b64encode(payload).decode(), declared_len=4,
            digest=sha256_hex(payload),
        )

    assert excinfo.value.code == ERR_CHUNK_LENGTH


def test_a_wrong_digest_is_refused(receiver: StagingReceiver) -> None:
    record = _begin(receiver, size=16)
    payload = b"x" * 8

    with pytest.raises(ReceiveError) as excinfo:
        receiver.put_chunk(
            upload_id=record.upload_id, principal="alice", index=0,
            data_b64=base64.b64encode(payload).decode(), declared_len=8,
            digest=sha256_hex(b"different"),
        )

    assert excinfo.value.code == ERR_CHUNK_DIGEST


def test_non_base64_is_refused(receiver: StagingReceiver) -> None:
    record = _begin(receiver, size=16)

    with pytest.raises(ReceiveError) as excinfo:
        receiver.put_chunk(
            upload_id=record.upload_id, principal="alice", index=0,
            data_b64="not base64 !!", declared_len=8, digest=sha256_hex(b"x"),
        )

    assert excinfo.value.code == ERR_CHUNK_ENCODING


@pytest.mark.parametrize("index", [-1, 2, 99])
def test_an_out_of_range_index_is_refused(receiver: StagingReceiver, index: int) -> None:
    record = _begin(receiver, size=16)  # two chunks: 0 and 1

    with pytest.raises(ReceiveError) as excinfo:
        _chunk(receiver, record, index, b"x" * 8)

    assert excinfo.value.code == ERR_CHUNK_INDEX


def test_a_declared_size_over_the_limit_is_refused_at_begin(receiver: StagingReceiver) -> None:
    with pytest.raises(ReceiveError) as excinfo:
        receiver.begin(principal="alice", project_key="proj", declared_bytes=10_000)
    assert excinfo.value.code == ERR_SIZE


# --- replay -------------------------------------------------------------------------------------


def test_an_exact_replay_is_a_no_op(receiver: StagingReceiver) -> None:
    """A retry after a dropped response is normal and must not double-count."""
    record = _begin(receiver, size=16)
    first = _chunk(receiver, record, 0, b"01234567")
    again = _chunk(receiver, first, 0, b"01234567")

    assert again.received_bytes == first.received_bytes == 8
    assert again.state is UploadState.RECEIVING


def test_a_conflicting_replay_fails_the_upload(receiver: StagingReceiver) -> None:
    """Same index, different bytes: client and server disagree about what is being uploaded,
    and continuing would assemble a bundle neither of them chose."""
    record = _begin(receiver, size=16)
    record = _chunk(receiver, record, 0, b"01234567")

    with pytest.raises(ReceiveError) as excinfo:
        _chunk(receiver, record, 0, b"OTHERBYT")

    assert excinfo.value.code == ERR_CHUNK_CONFLICT
    after = receiver.status(upload_id=record.upload_id, principal="alice")
    assert after.state is UploadState.FAILED
    with pytest.raises(ReceiveError) as second:
        _chunk(receiver, record, 1, b"89abcdef")
    assert second.value.code == ERR_WRONG_STATE


# --- quotas -------------------------------------------------------------------------------------


def test_a_principal_is_capped_at_two_concurrent_uploads(tmp_path: Path, clock: FakeClock) -> None:
    receiver = StagingReceiver(
        tmp_path / "s", limits=_LIMITS, clock=clock,
        quotas=StagingQuotas(max_receiving_per_principal=2),
    )
    _begin(receiver)
    _begin(receiver)

    with pytest.raises(ReceiveError) as excinfo:
        _begin(receiver)

    assert excinfo.value.code == ERR_TOO_MANY_PER_PRINCIPAL


def test_a_project_is_capped_across_principals(tmp_path: Path, clock: FakeClock) -> None:
    """The per-project cap must not be evadable by using more identities."""
    receiver = StagingReceiver(
        tmp_path / "s", limits=_LIMITS, clock=clock,
        quotas=StagingQuotas(max_receiving_per_principal=5, max_receiving_per_project=2),
    )
    _begin(receiver, principal="alice", project="shared")
    _begin(receiver, principal="bob", project="shared")

    with pytest.raises(ReceiveError) as excinfo:
        _begin(receiver, principal="carol", project="shared")

    assert excinfo.value.code == ERR_TOO_MANY_PER_PROJECT


def test_only_a_server_registered_project_identity_can_be_reused(
    tmp_path: Path, clock: FakeClock
) -> None:
    receiver = StagingReceiver(tmp_path / "s", limits=_LIMITS, clock=clock)
    first = _begin(receiver, principal="alice", project="display-a", size=8)
    _chunk(receiver, first, 0, b"12345678")
    receiver.ensure_project_identity(upload_id=first.upload_id, principal="alice")
    reused = receiver.begin(
        principal="bob",
        project_key="renamed-display",
        project_id=first.project_id,
        declared_bytes=8,
    )

    assert reused.project_id == first.project_id
    with pytest.raises(ReceiveError) as excinfo:
        receiver.begin(
            principal="alice",
            project_key="display-a",
            project_id="project-" + ("f" * 32),
            declared_bytes=8,
        )
    assert excinfo.value.code == ERR_PROJECT_IDENTITY


def test_a_principal_reservation_bound_includes_sealed_bytes(
    tmp_path: Path, clock: FakeClock
) -> None:
    receiver = StagingReceiver(
        tmp_path / "s",
        limits=_LIMITS,
        clock=clock,
        quotas=StagingQuotas(
            max_receiving_per_principal=10,
            max_reserved_bytes_per_principal=16,
        ),
    )
    first = _begin(receiver, size=16)
    _chunk(receiver, first, 0, b"01234567")
    assert _chunk(receiver, first, 1, b"89abcdef").state is UploadState.SEALED

    with pytest.raises(ReceiveError) as excinfo:
        _begin(receiver, size=1)

    assert excinfo.value.code == ERR_DISK_BUDGET


def test_aborting_releases_the_quota_slot(receiver: StagingReceiver) -> None:
    first = _begin(receiver)
    _begin(receiver)
    receiver.abort(upload_id=first.upload_id, principal="alice")

    assert _begin(receiver).state is UploadState.RECEIVING


def test_a_begin_is_refused_before_the_disk_budget_is_exhausted(
    tmp_path: Path, clock: FakeClock
) -> None:
    """The gate's wording: refusal must happen ahead of exhaustion, not at it."""
    receiver = StagingReceiver(
        tmp_path / "s", limits=_LIMITS, clock=clock,
        quotas=StagingQuotas(disk_budget_bytes=32),
    )

    with pytest.raises(ReceiveError) as excinfo:
        receiver.begin(principal="alice", project_key="proj", declared_bytes=1000)

    assert excinfo.value.code == ERR_DISK_BUDGET


# --- lifetime -----------------------------------------------------------------------------------


def test_an_idle_upload_expires_and_drops_its_bytes(receiver: StagingReceiver, clock: FakeClock) -> None:
    record = _begin(receiver, size=16)
    _chunk(receiver, record, 0, b"01234567")

    clock.advance(24 * 60 * 60 + 1)
    counts = receiver.sweep()

    assert counts["expired"] == 1
    assert receiver.status(upload_id=record.upload_id, principal="alice").state is UploadState.EXPIRED
    assert not (receiver.root / record.upload_id / "blob").exists()


def test_activity_postpones_expiry(receiver: StagingReceiver, clock: FakeClock) -> None:
    record = _begin(receiver, size=24)
    clock.advance(23 * 60 * 60)
    record = _chunk(receiver, record, 0, b"01234567")

    clock.advance(2 * 60 * 60)
    receiver.sweep()

    assert receiver.status(upload_id=record.upload_id, principal="alice").state is UploadState.RECEIVING


def test_a_terminal_record_is_reclaimed_after_its_retention_window(
    receiver: StagingReceiver, clock: FakeClock
) -> None:
    record = _begin(receiver, size=16)
    receiver.abort(upload_id=record.upload_id, principal="alice")

    clock.advance(60 * 60 + 1)
    counts = receiver.sweep()

    assert counts["reclaimed"] == 1
    with pytest.raises(ReceiveError):
        receiver.status(upload_id=record.upload_id, principal="alice")


def test_a_terminal_record_survives_until_its_window_closes(
    receiver: StagingReceiver, clock: FakeClock
) -> None:
    """Status has to stay answerable for a while, or a client that aborted cannot confirm it."""
    record = _begin(receiver, size=16)
    receiver.abort(upload_id=record.upload_id, principal="alice")

    clock.advance(30 * 60)
    receiver.sweep()

    assert receiver.status(upload_id=record.upload_id, principal="alice").state is UploadState.ABORTED


# --- restart ------------------------------------------------------------------------------------


def test_an_upload_resumes_across_a_restart(tmp_path: Path, clock: FakeClock) -> None:
    """Invariant 11: the record is the authority, so a new process continues the same upload."""
    root = tmp_path / "staging"
    first = StagingReceiver(root, limits=_LIMITS, clock=clock)
    record = _begin(first, size=16)
    _chunk(first, record, 0, b"01234567")

    reopened = StagingReceiver(root, limits=_LIMITS, clock=clock)
    status = reopened.status(upload_id=record.upload_id, principal="alice")
    assert status.missing == [1]

    finished = _chunk(reopened, status, 1, b"89abcdef")
    assert finished.state is UploadState.SEALED
    assert (root / record.upload_id / "blob").read_bytes() == b"0123456789abcdef"


def test_bytes_with_no_record_are_reclaimed_not_adopted(tmp_path: Path, clock: FakeClock) -> None:
    """A directory's existence is not state. Unattributable bytes are garbage that would
    otherwise sit in the disk budget forever."""
    root = tmp_path / "staging"
    receiver = StagingReceiver(root, limits=_LIMITS, clock=clock)
    orphan = root / "deadbeefdeadbeefdeadbeefdeadbeef"
    orphan.mkdir(parents=True)
    (orphan / "blob").write_bytes(b"x" * 100)

    counts = receiver.sweep()

    assert counts["orphans"] == 1
    assert not orphan.exists()


def test_an_unreadable_record_is_not_silently_trusted(tmp_path: Path, clock: FakeClock) -> None:
    root = tmp_path / "staging"
    receiver = StagingReceiver(root, limits=_LIMITS, clock=clock)
    record = _begin(receiver, size=16)
    (root / record.upload_id / "record.json").write_text("{ truncated", encoding="utf-8")

    with pytest.raises(ReceiveError) as excinfo:
        receiver.status(upload_id=record.upload_id, principal="alice")

    assert excinfo.value.code == ERR_NOT_FOUND


# --- redaction ----------------------------------------------------------------------------------


def test_no_record_or_error_carries_content(receiver: StagingReceiver) -> None:
    """Invariant 5: records and errors hold ids, counts, digests and timestamps -- nothing else."""
    secret = b"SUPER-SECRET-SOURCE-LINE"
    record = _begin(receiver, size=len(secret))
    record = receiver.put_chunk(
        upload_id=record.upload_id, principal="alice", index=0,
        data_b64=base64.b64encode(secret[:8]).decode(), declared_len=8,
        digest=sha256_hex(secret[:8]),
    )

    stored = json.loads((receiver.root / record.upload_id / "record.json").read_text())
    serialized = json.dumps(stored)
    assert "SUPER" not in serialized
    assert base64.b64encode(secret[:8]).decode() not in serialized
    assert set(stored) == {
        "upload_id", "principal", "project_key", "state", "declared_bytes", "chunk_bytes",
            "project_id", "snapshot_id", "total_chunks", "created_at", "updated_at",
            "received", "received_bytes", "failure_code", "result",
        }


def test_a_refusal_message_never_echoes_the_payload(receiver: StagingReceiver) -> None:
    record = _begin(receiver, size=16)
    secret = b"SECRET-PAYLOAD!!"

    with pytest.raises(ReceiveError) as excinfo:
        receiver.put_chunk(
            upload_id=record.upload_id, principal="alice", index=0,
            data_b64=base64.b64encode(secret).decode(), declared_len=len(secret),
            digest=sha256_hex(secret),
        )

    assert "SECRET" not in str(excinfo.value)
    assert base64.b64encode(secret).decode() not in str(excinfo.value)


# --- concurrency: two processes on one staging root ----------------------------------------------
#
# Everything above drives ONE receiver. That is the single-event-loop deployment, where `begin`
# runs start to finish without interleaving and the caps hold trivially. These force the case the
# P2A soak never produced: two receivers over the same root, which is what a second uvicorn worker,
# a second replica, or a future threadpool dispatch actually is.
#
# `begin` is a check-then-act -- scan the directory, count RECEIVING records against the caps, then
# write a new one -- with nothing reserved in between. The interleaving is forced deterministically
# by pausing the first receiver at that seam rather than by threads and sleeps, so these tests
# either describe real behaviour or fail; they never flake.


def _pause_before_write(receiver: StagingReceiver, during):
    """Run `during()` in the window between the quota check and the record write.

    That window is the whole defect if there is one: both callers have passed their checks and
    neither has written yet.
    """
    original = receiver._write_record
    fired = False

    def patched(record):
        nonlocal fired
        if not fired:
            fired = True
            during()
        return original(record)

    receiver._write_record = patched  # type: ignore[method-assign]
    return lambda: fired


def test_two_processes_cannot_exceed_the_per_principal_cap(tmp_path: Path, clock: FakeClock) -> None:
    """Two concurrent begins at the cap boundary must not both be admitted."""
    root = tmp_path / "staging"
    quotas = StagingQuotas(max_receiving_per_principal=2)
    a = StagingReceiver(root, limits=_LIMITS, quotas=quotas, clock=clock)
    b = StagingReceiver(root, limits=_LIMITS, quotas=quotas, clock=clock)

    _begin(a)  # one slot used; one remains

    # Receiver B slips a full begin into A's check-then-act window. Both saw one active upload and
    # both believed a slot was free.
    fired = _pause_before_write(a, lambda: _begin(b))

    try:
        _begin(a)
    except ReceiveError as exc:
        assert exc.code == ERR_TOO_MANY_PER_PRINCIPAL
    assert fired(), "the interleaving never happened; this test proved nothing"

    active = [r for r in a._all_records() if r.state is UploadState.RECEIVING]
    assert len(active) <= quotas.max_receiving_per_principal, (
        f"{len(active)} concurrent uploads admitted for one principal, cap is "
        f"{quotas.max_receiving_per_principal}"
    )


def test_two_processes_cannot_exceed_the_per_project_cap(tmp_path: Path, clock: FakeClock) -> None:
    """The project cap spans principals, so the racing callers here are different people."""
    root = tmp_path / "staging"
    quotas = StagingQuotas(max_receiving_per_principal=8, max_receiving_per_project=2)
    a = StagingReceiver(root, limits=_LIMITS, quotas=quotas, clock=clock)
    b = StagingReceiver(root, limits=_LIMITS, quotas=quotas, clock=clock)

    _begin(a, principal="alice", project="shared")

    fired = _pause_before_write(a, lambda: _begin(b, principal="bob", project="shared"))
    try:
        _begin(a, principal="carol", project="shared")
    except ReceiveError as exc:
        assert exc.code == ERR_TOO_MANY_PER_PROJECT
    assert fired(), "the interleaving never happened; this test proved nothing"

    active = [
        r for r in a._all_records()
        if r.state is UploadState.RECEIVING and r.project_key == "shared"
    ]
    assert len(active) <= quotas.max_receiving_per_project, (
        f"{len(active)} concurrent uploads admitted for one project, cap is "
        f"{quotas.max_receiving_per_project}"
    )


def test_two_processes_cannot_overcommit_the_disk_budget(tmp_path: Path, clock: FakeClock) -> None:
    """The budget must bound what is admitted, not merely what was already present.

    This is the case the existing budget test cannot reach: it refuses a begin against an
    already-full budget, whereas the dangerous one is two begins that each fit alone and together
    do not.
    """
    root = tmp_path / "staging"
    # Room for one 512-byte upload, not two.
    quotas = StagingQuotas(disk_budget_bytes=900)
    a = StagingReceiver(root, limits=_LIMITS, quotas=quotas, clock=clock)
    b = StagingReceiver(root, limits=_LIMITS, quotas=quotas, clock=clock)

    fired = _pause_before_write(a, lambda: _begin(b, principal="bob", size=512))
    try:
        _begin(a, principal="alice", size=512)
    except ReceiveError as exc:
        assert exc.code == ERR_DISK_BUDGET
    assert fired(), "the interleaving never happened; this test proved nothing"

    committed = sum(
        r.declared_bytes for r in a._all_records() if r.state is UploadState.RECEIVING
    )
    assert committed <= quotas.disk_budget_bytes, (
        f"{committed} bytes admitted against a {quotas.disk_budget_bytes} byte budget"
    )


# --- crash-torn states: what a real kill leaves behind -------------------------------------------
#
# `test_an_upload_resumes_across_a_restart` builds a second receiver over the same root, which
# tests the resume LOGIC but not the states a kill actually leaves. Every write here is two steps,
# and the interesting moment is between them:
#
#   begin      writes the record, THEN creates the blob
#   put_chunk  writes the blob, THEN updates the record
#   abort      writes the record, THEN drops the blob
#
# A crash in any of those windows leaves durable state that no caller ever produced. These force
# each one directly rather than hoping a killed container lands there.


def test_a_chunk_written_but_not_recorded_is_simply_missing(receiver: StagingReceiver) -> None:
    """Crash between the blob write and the record update.

    The bytes are on disk and the record does not know. This ordering is the safe one -- the
    chunk reports missing, the client resends, and the resend is an ordinary first delivery -- so
    the test pins the ordering rather than the recovery.
    """
    record = _begin(receiver, size=16)
    blob = receiver.root / record.upload_id / "blob"
    with open(blob, "r+b") as handle:  # what put_chunk would have written
        handle.seek(0)
        handle.write(b"AAAAAAAA")

    resumed = StagingReceiver(receiver.root, limits=_LIMITS, clock=receiver._clock)
    status = resumed.status(upload_id=record.upload_id, principal="alice")
    assert 0 in status.missing, "unrecorded bytes must not be mistaken for a received chunk"

    # The resend is accepted normally and is not treated as a conflicting replay.
    updated = _chunk(resumed, record, 0, b"AAAAAAAA")
    assert 0 in updated.received


def test_a_record_whose_blob_never_existed_refuses_cleanly(receiver: StagingReceiver) -> None:
    """Crash between `begin`'s record write and its blob creation.

    The record is durable and the blob is not. A caller resuming against it must get a refusal
    carrying a code, not an unhandled OSError escaping the receiver -- an upload id that produces
    a 500 is indistinguishable, from the client, from the server being broken.
    """
    record = _begin(receiver, size=16)
    (receiver.root / record.upload_id / "blob").unlink()

    resumed = StagingReceiver(receiver.root, limits=_LIMITS, clock=receiver._clock)
    with pytest.raises(ReceiveError) as excinfo:
        _chunk(resumed, record, 0, b"AAAAAAAA")
    assert excinfo.value.code == ERR_STAGING_LOST

    # Durable, not merely raised: the upload is FAILED, so `status` tells the truth and a client
    # that keeps sending chunks is refused by the state check rather than re-running this path.
    after = resumed.status(upload_id=record.upload_id, principal="alice")
    assert after.state is UploadState.FAILED
    assert after.failure_code == ERR_STAGING_LOST

    # And it must never silently recover by recreating the blob: that would zero-fill indices the
    # record still claims and SEAL over a bundle whose digests were never re-checked.
    assert not (receiver.root / record.upload_id / "blob").exists()


def test_a_half_written_record_is_not_adopted_as_state(tmp_path: Path, clock: FakeClock) -> None:
    """Crash during `begin`'s very first record write, leaving only the temp file.

    Invariant 11: a directory's existence is not state. The half-written upload must be reclaimed
    as garbage, not resumed -- bytes that cannot be attributed to an owner still occupy the disk
    budget.
    """
    root = tmp_path / "staging"
    root.mkdir(parents=True)
    torn = root / ("a" * 32)
    torn.mkdir()
    (torn / "record.json.tmp").write_text('{"upload_id": "incomp', encoding="utf-8")

    receiver = StagingReceiver(root, limits=_LIMITS, clock=clock)
    receiver.sweep()

    assert not torn.exists(), "a half-written upload was kept as if it were state"


def test_an_abort_that_crashed_before_dropping_bytes_still_frees_them(
    receiver: StagingReceiver, clock: FakeClock
) -> None:
    """Crash between `abort`'s record write and its blob drop.

    The upload is terminal but its bytes are still on disk, occupying the disk budget. Retention
    is allowed to hold the RECORD; it is not allowed to hold the payload forever.
    """
    record = _begin(receiver, size=16)
    _chunk(receiver, record, 0, b"AAAAAAAA")
    aborted = replace_state_to_aborted(receiver, record)
    assert (receiver.root / aborted.upload_id / "blob").exists(), "test setup did not leave bytes"

    clock.advance(receiver.quotas.terminal_retention_s + 1)
    receiver.sweep()

    blob = receiver.root / aborted.upload_id / "blob"
    assert not blob.exists(), "payload bytes outlived the terminal retention window"


def replace_state_to_aborted(receiver: StagingReceiver, record):
    """Abort without dropping the blob -- i.e. exactly where a crash mid-abort leaves things."""
    from dataclasses import replace as _replace

    from menhir.snapshot.receive import UploadState as _State

    aborted = _replace(record, state=_State.ABORTED, updated_at=receiver._clock())
    receiver._write_record(aborted)
    return aborted
