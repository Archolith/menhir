"""P2A staging receiver: accept bundle chunks, reach SEALED, and touch nothing else.

Plan: ``.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md`` (P2A -- real-handler transport
measurement, staging only).

**This exists to be measured, not to serve users.** It runs the real begin/chunk/status/abort path
so P2A can time and size the actual handler, and it stops there: it never extracts an archive,
never reads a manifest, never touches the graph. P2B adds the durable SQLite store, commit, and
the registered feature mode; P3 adds extraction. Anything here that reaches further is a bug.

Five properties carry the weight, and each is a plan invariant rather than a preference:

**Ownership is bound at creation and re-checked at load (invariant 2).** The upload id is an
opaque token, but a token is not authorization: every later call re-derives the owner from the
stored record and compares. A caller who guesses an id gets the same answer as a caller who
supplies a stale one -- not found.

**Limits are enforced before allocation and again after decode (invariant 6).** The declared
length is checked before any buffer is made, and the decoded length is checked again afterwards,
because the declaration is the caller's claim and the decode is the fact.

**Nothing derived from content is written down (invariant 5).** No path, no chunk bytes, no
decoded text reaches a record, an error, or a log line. Records hold ids, counts, digests and
timestamps; that is the whole vocabulary.

**State is durable and never inferred from a directory (invariant 11).** The record is the
authority. A blob with no record is garbage to reclaim, a record with no blob is FAILED, and a
restart mid-upload resumes from the record rather than from what happens to be on disk. P2A uses
one JSON sidecar per upload instead of the SQLite store P2B specifies -- enough to survive a
restart, and deliberately not the production store.

**A conflicting replay fails the upload.** An exact replay of a chunk is a no-op, because a retry
after a dropped response is normal. The same index arriving with a DIFFERENT digest is not a
retry: it means the client and the server disagree about what is being uploaded, and continuing
would assemble bytes neither of them chose.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from menhir.snapshot.protocol import (
    PROVISIONAL_LIMITS,
    BundleFormatError,
    SnapshotLimits,
    chunk_count,
    sha256_hex,
)
from menhir.snapshot.receive_records import (
    ERR_CHUNK_CONFLICT,
    ERR_CHUNK_DIGEST,
    ERR_CHUNK_ENCODING,
    ERR_CHUNK_INDEX,
    ERR_CHUNK_LENGTH,
    ERR_DISK_BUDGET,
    ERR_NOT_FOUND,
    ERR_SIZE,
    ERR_STAGING_LOST,
    ERR_TOO_MANY_PER_PRINCIPAL,
    ERR_TOO_MANY_PER_PROJECT,
    ERR_WRONG_STATE,
    ReceiveError,
    StagingQuotas,
    UploadRecord,
    UploadState,
)

__all__ = [
    "ReceiveError",
    "StagingQuotas",
    "StagingReceiver",
    "UploadRecord",
    "UploadState",
]


class StagingReceiver:
    """Filesystem-backed staging area for P2A.

    One directory per upload holding ``record.json`` (the authority) and ``blob`` (the bytes).
    ``clock`` is injectable because every TTL and retention test would otherwise have to sleep.
    """

    def __init__(
        self,
        root: Path,
        *,
        limits: SnapshotLimits = PROVISIONAL_LIMITS,
        quotas: StagingQuotas | None = None,
        clock: Any = time.time,
    ) -> None:
        self.root = Path(root)
        self.limits = limits
        self.quotas = quotas or StagingQuotas()
        self._clock = clock
        self.root.mkdir(parents=True, exist_ok=True)

    # -- storage layout ---------------------------------------------------------------------

    def _dir(self, upload_id: str) -> Path:
        # `upload_id` is server-minted hex; it never contains a separator. Rejecting anything
        # else keeps a caller-supplied id from selecting a path outside the staging root.
        if not upload_id or not all(c in "0123456789abcdef" for c in upload_id):
            raise ReceiveError(ERR_NOT_FOUND, "no such upload")
        return self.root / upload_id

    def _write_record(self, record: UploadRecord) -> None:
        """Write the record atomically: a torn record is indistinguishable from a lost one."""
        target = self._dir(record.upload_id)
        target.mkdir(parents=True, exist_ok=True)
        tmp = target / "record.json.tmp"
        tmp.write_text(json.dumps(record.as_json(), sort_keys=True), encoding="utf-8")
        os.replace(tmp, target / "record.json")

    def _read_record(self, upload_id: str) -> UploadRecord | None:
        path = self._dir(upload_id) / "record.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        try:
            return UploadRecord.from_json(raw)
        except (KeyError, TypeError, ValueError):
            return None

    def _load_owned(self, upload_id: str, principal: str) -> UploadRecord:
        """Load and re-check ownership (invariant 2).

        A record owned by someone else reports NOT FOUND rather than forbidden: "this id exists
        but is not yours" is itself information, and an id is not a secret worth confirming.
        """
        record = self._read_record(upload_id)
        if record is None or record.principal != principal:
            raise ReceiveError(ERR_NOT_FOUND, "no such upload")
        return record

    def _all_records(self) -> list[UploadRecord]:
        records = []
        for child in sorted(self.root.iterdir()) if self.root.is_dir() else []:
            if not child.is_dir():
                continue
            record = self._read_record(child.name)
            if record is not None:
                records.append(record)
        return records

    def staged_bytes(self) -> int:
        """Bytes currently on disk under the staging root, records included."""
        total = 0
        for path in self.root.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    # -- operations -------------------------------------------------------------------------

    def begin(
        self, *, principal: str, project_key: str, declared_bytes: int,
        chunk_bytes: int | None = None,
    ) -> UploadRecord:
        """Reserve an upload. Allocates a record and a quota slot, never the declared size.

        The declared size is a claim used to compute the chunk plan and to refuse an obviously
        impossible upload early. It is not trusted: what actually lands is counted chunk by chunk.
        """
        now = self._clock()
        self.sweep(now=now)

        if declared_bytes < 0 or declared_bytes > self.limits.max_compressed_bytes:
            raise ReceiveError(
                ERR_SIZE,
                f"declared size must be between 0 and {self.limits.max_compressed_bytes} bytes",
            )
        negotiated = chunk_bytes or self.limits.chunk_bytes
        if negotiated <= 0 or negotiated > self.limits.max_chunk_bytes:
            raise ReceiveError(
                ERR_SIZE, f"chunk size must be between 1 and {self.limits.max_chunk_bytes} bytes"
            )

        # Refuse ahead of exhaustion, counting what this upload could add rather than only what
        # is already there: admitting it and discovering the wall mid-transfer wastes the whole
        # upload and leaves the staging area full. This pre-check is an optimisation -- it keeps
        # an obviously impossible upload from ever touching the disk -- and NOT the enforcement.
        # Enforcement is `_verify_reservation` below, because a check performed before the write
        # cannot bind a concurrent writer that has not written yet.
        if self.staged_bytes() + declared_bytes > self.quotas.disk_budget_bytes:
            raise ReceiveError(ERR_DISK_BUDGET, "staging disk budget exhausted; retry later")

        record = UploadRecord(
            upload_id=secrets.token_hex(16),
            principal=principal,
            project_key=project_key,
            state=UploadState.RECEIVING,
            declared_bytes=declared_bytes,
            chunk_bytes=negotiated,
            total_chunks=chunk_count(declared_bytes, negotiated),
            created_at=now,
            updated_at=now,
        )
        # Reserve FIRST, then verify. Counting the directory and then writing is a check-then-act:
        # two processes on one staging root -- a second uvicorn worker, a second replica, or a
        # future threadpool dispatch -- both read "one slot free" and both take it, and every quota
        # here becomes advisory. Writing first makes the reservation durable and visible to every
        # other process before this one decides whether it may keep it.
        self._write_record(record)
        (self._dir(record.upload_id) / "blob").touch()
        try:
            self._verify_reservation(record)
        except ReceiveError:
            # Back out our own reservation only. A racer that crashes between the write and this
            # verification leaves a RECEIVING record, which the inactivity TTL already reclaims --
            # the same path as any abandoned upload, so this adds no new class of leak. That is
            # the reason for reserve-then-verify rather than a lock file: a crashed lock holder
            # would block every future begin until someone noticed.
            self._remove(record.upload_id)
            raise
        return record

    def _verify_reservation(self, record: UploadRecord) -> None:
        """Confirm the quotas still hold with this reservation counted, and back off if not.

        Counts EVERY active reservation, this one included. An earlier attempt counted only the
        records ordered before this one -- by `(created_at, upload_id)` -- so that two racers for
        one slot would reach opposite conclusions and exactly one would keep it. That is wrong,
        and the project-cap test caught it: `created_at` ties (a coarse clock, or a fake one), so a
        record admitted earlier in real time can sort AFTER this one, fall outside the prefix, and
        go uncounted. Ordering cannot stand in for admission order without a sequence number that
        does not exist here.

        Counting everything costs liveness and buys safety: under contention BOTH racers can see
        the same over-quota total and both back off, losing a slot that one of them could have
        had. For a quota that is the right trade -- the error is retriable and `begin` is cheap,
        whereas over-admitting means a disk budget that does not bound anything. It is also
        self-correcting: once both have withdrawn, the slot is free and either retry succeeds.
        """
        active = [r for r in self._all_records() if r.state is UploadState.RECEIVING]

        if (
            sum(1 for r in active if r.principal == record.principal)
            > self.quotas.max_receiving_per_principal
        ):
            raise ReceiveError(
                ERR_TOO_MANY_PER_PRINCIPAL,
                f"at most {self.quotas.max_receiving_per_principal} concurrent uploads per caller",
            )
        if (
            sum(1 for r in active if r.project_key == record.project_key)
            > self.quotas.max_receiving_per_project
        ):
            raise ReceiveError(
                ERR_TOO_MANY_PER_PROJECT,
                f"at most {self.quotas.max_receiving_per_project} concurrent uploads per project",
            )
        if sum(r.declared_bytes for r in active) > self.quotas.disk_budget_bytes:
            raise ReceiveError(ERR_DISK_BUDGET, "staging disk budget exhausted; retry later")

    def put_chunk(
        self, *, upload_id: str, principal: str, index: int, data_b64: str,
        declared_len: int, digest: str,
    ) -> UploadRecord:
        """Accept one chunk. Bounds are checked before the decode and again after it."""
        record = self._load_owned(upload_id, principal)
        if record.state is not UploadState.RECEIVING:
            raise ReceiveError(ERR_WRONG_STATE, f"upload is {record.state.value}")
        if index < 0 or index >= record.total_chunks:
            raise ReceiveError(ERR_CHUNK_INDEX, f"index must be 0..{record.total_chunks - 1}")

        # Before allocation: the declared length alone can refuse an oversized chunk without
        # decoding a single byte of it. The protocol layer raises its own error type here, which
        # is re-wrapped so that every refusal leaving this class is a ReceiveError with a stable
        # code -- otherwise the tool layer above has two exception shapes to handle and will
        # eventually handle only one.
        try:
            self.limits.validate_chunk_bytes(declared_len)
        except BundleFormatError as exc:
            raise ReceiveError(ERR_CHUNK_LENGTH, "chunk exceeds the hard ceiling") from exc
        if declared_len > record.chunk_bytes:
            raise ReceiveError(
                ERR_CHUNK_LENGTH, f"chunk exceeds the negotiated {record.chunk_bytes} bytes"
            )

        try:
            data = base64.b64decode(data_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ReceiveError(ERR_CHUNK_ENCODING, "chunk is not valid base64") from exc

        # After decode: the declaration was a claim, this is the fact.
        if len(data) != declared_len:
            raise ReceiveError(ERR_CHUNK_LENGTH, "decoded length does not match the declared length")
        actual = sha256_hex(data)
        if actual != digest:
            raise ReceiveError(ERR_CHUNK_DIGEST, "chunk digest does not match its bytes")

        previous = record.received.get(index)
        if previous is not None:
            if previous == digest:
                # Exact replay: a retry after a dropped response is normal, so it is a no-op and
                # must not advance any counter.
                return record
            failed = replace(
                record,
                state=UploadState.FAILED,
                failure_code=ERR_CHUNK_CONFLICT,
                updated_at=self._clock(),
            )
            self._write_record(failed)
            raise ReceiveError(
                ERR_CHUNK_CONFLICT,
                "this chunk was already received with different content; the upload is failed",
            )

        blob = self._dir(upload_id) / "blob"
        try:
            with open(blob, "r+b") as handle:
                handle.seek(index * record.chunk_bytes)
                handle.write(data)
        except OSError:
            # The record survived but its bytes are not writable. `begin` writes the record and
            # THEN creates the blob, so a crash between those two leaves exactly this; deletion or
            # a full disk produces it too. Both terminal states that drop bytes -- ABORTED and
            # EXPIRED -- are already refused by the state check above, so reaching here means the
            # record and its bytes genuinely disagree.
            #
            # FAILED rather than recreating the blob. Recreating looks like recovery and is
            # corruption when `received` is non-empty: those indices would become zero-filled
            # while the record still claims them, and the upload would SEAL over a bundle whose
            # chunk digests were never re-checked. Failing is durable, so `status` tells the truth
            # instead of every later chunk raising the same error again.
            #
            # The catch covers the write as well as the open deliberately: a chunk that was only
            # partly written leaves the blob in a state no digest in the record describes, which
            # is the same disagreement arriving by a different route.
            failed = replace(
                record,
                state=UploadState.FAILED,
                failure_code=ERR_STAGING_LOST,
                updated_at=self._clock(),
            )
            self._write_record(failed)
            raise ReceiveError(
                ERR_STAGING_LOST,
                "staged bytes for this upload are gone; begin a new upload",
            ) from None

        updated = replace(
            record,
            received={**record.received, index: digest},
            received_bytes=record.received_bytes + len(data),
            updated_at=self._clock(),
        )
        if updated.complete:
            updated = replace(updated, state=UploadState.SEALED)
        self._write_record(updated)
        return updated

    def status(self, *, upload_id: str, principal: str) -> UploadRecord:
        return self._load_owned(upload_id, principal)

    def abort(self, *, upload_id: str, principal: str) -> UploadRecord:
        """Cancel an uncommitted upload and drop its bytes. Idempotent.

        A SEALED upload is still abortable here because P2A never hands it anywhere; once P2B's
        commit exists, a job past SEALED must complete or compensate instead.
        """
        record = self._load_owned(upload_id, principal)
        if record.state is UploadState.ABORTED:
            return record
        aborted = replace(record, state=UploadState.ABORTED, updated_at=self._clock())
        self._write_record(aborted)
        self._drop_blob(upload_id)
        return aborted

    def sweep(self, *, now: float | None = None) -> dict[str, int]:
        """Expire idle uploads and reclaim terminal ones. Safe to call on every operation.

        Returns counts only -- a sweep that named what it removed would put upload identifiers
        into whatever logs its caller keeps.
        """
        moment = self._clock() if now is None else now
        expired = 0
        reclaimed = 0
        for record in self._all_records():
            idle = moment - record.updated_at
            if record.state is UploadState.RECEIVING and idle >= self.quotas.inactivity_ttl_s:
                self._write_record(
                    replace(record, state=UploadState.EXPIRED, updated_at=moment)
                )
                self._drop_blob(record.upload_id)
                expired += 1
                continue
            if record.state.terminal and idle >= self.quotas.terminal_retention_s:
                self._remove(record.upload_id)
                reclaimed += 1
        orphans = self._reclaim_orphans()
        return {"expired": expired, "reclaimed": reclaimed, "orphans": orphans}

    def _reclaim_orphans(self) -> int:
        """Remove blobs with no readable record (invariant 11).

        A directory's existence is not state. Bytes whose record is missing or unparseable cannot
        be resumed, attributed to an owner, or counted against a quota, so they are garbage --
        and garbage that occupies the disk budget is how a staging area fills with nothing.
        """
        removed = 0
        for child in sorted(self.root.iterdir()) if self.root.is_dir() else []:
            if child.is_dir() and self._read_record(child.name) is None:
                self._remove(child.name, validate=False)
                removed += 1
        return removed

    def _drop_blob(self, upload_id: str) -> None:
        try:
            (self._dir(upload_id) / "blob").unlink(missing_ok=True)
        except OSError:
            pass

    def _remove(self, upload_id: str, *, validate: bool = True) -> None:
        target = self._dir(upload_id) if validate else self.root / upload_id
        for child in sorted(target.rglob("*"), reverse=True):
            try:
                child.unlink() if child.is_file() else child.rmdir()
            except OSError:
                pass
        try:
            target.rmdir()
        except OSError:
            pass
