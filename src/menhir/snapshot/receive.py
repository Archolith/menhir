"""Durable snapshot receiver state used by the upload and processing pipeline.

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
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any

from menhir.snapshot.protocol import (
    PROVISIONAL_LIMITS,
    BundleFormatError,
    SnapshotLimits,
    chunk_count,
    sha256_hex,
)

__all__ = [
    "ReceiveError",
    "StagingQuotas",
    "StagingReceiver",
    "UploadRecord",
    "UploadState",
]

# --- stable machine-readable error codes -------------------------------------------------------
ERR_NOT_FOUND = "snapshot.upload.not_found"
ERR_WRONG_STATE = "snapshot.upload.wrong_state"
ERR_CHUNK_INDEX = "snapshot.upload.chunk_index_out_of_range"
ERR_CHUNK_LENGTH = "snapshot.upload.chunk_length_mismatch"
ERR_CHUNK_DIGEST = "snapshot.upload.chunk_digest_mismatch"
ERR_CHUNK_CONFLICT = "snapshot.upload.conflicting_replay"
ERR_CHUNK_ENCODING = "snapshot.upload.chunk_not_base64"
ERR_TOO_MANY_PER_PRINCIPAL = "snapshot.upload.too_many_for_principal"
ERR_TOO_MANY_PER_PROJECT = "snapshot.upload.too_many_for_project"
ERR_DISK_BUDGET = "snapshot.upload.staging_disk_budget"
#: The record survived but its staged bytes did not -- a crash between `begin`'s record write and
#: its blob creation, a deletion, or a disk fault. Distinct from `wrong_state` because the upload
#: is not in a state the caller can reason about or retry into; it must start a new one.
ERR_STAGING_LOST = "snapshot.upload.staged_bytes_lost"
ERR_SIZE = "snapshot.upload.declared_size_rejected"
ERR_PROJECT_KEY = "snapshot.upload.project_key_rejected"
ERR_PROJECT_IDENTITY = "snapshot.upload.project_identity_rejected"


class UploadState(str, Enum):
    """Durable upload and processing states exposed by snapshot status."""

    RECEIVING = "RECEIVING"
    SEALED = "SEALED"
    EXTRACTING = "EXTRACTING"
    SCANNING = "SCANNING"
    PROMOTING = "PROMOTING"
    READY = "READY"
    ABORTED = "ABORTED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"

    @property
    def terminal(self) -> bool:
        return self in (
            UploadState.SEALED,
            UploadState.READY,
            UploadState.ABORTED,
            UploadState.EXPIRED,
            UploadState.FAILED,
        )


class ReceiveError(RuntimeError):
    """A refusal, carrying a stable code and a message safe to return and to log."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class StagingQuotas:
    """Owner-approved pilot figures (2026-09-16), pending P2A's soak and disk-pressure runs.

    Deliberately NOT part of :class:`~menhir.snapshot.protocol.SnapshotLimits`: that dataclass
    ships to the client as the wire contract, and a client has no business knowing the server's
    retention window. Shipping them together invites a client that assumes them.
    """

    #: Concurrent RECEIVING uploads one authenticated principal may hold.
    max_receiving_per_principal: int = 2
    #: Concurrent RECEIVING uploads across all principals for one project.
    max_receiving_per_project: int = 8
    #: An upload with no chunk activity for this long is expired by the sweep.
    inactivity_ttl_s: float = 24 * 60 * 60
    #: How long a terminal record and its bytes are retained before removal.
    terminal_retention_s: float = 60 * 60
    #: Total staged bytes allowed before a new begin is refused. A begin must fail before the
    #: disk does: the gate requires proving refusal happens ahead of exhaustion, not at it.
    disk_budget_bytes: int = 4 * 1024 * 1024 * 1024
    #: One caller may reserve at most a quarter of the shared pilot budget. This is an
    #: operability bound for a multi-user company deployment, not a tenant-isolation boundary.
    max_reserved_bytes_per_principal: int = 1024 * 1024 * 1024


@dataclass(frozen=True)
class UploadRecord:
    """Everything known about one upload. Ids, counts, digests, timestamps -- no content."""

    upload_id: str
    principal: str
    project_key: str
    project_id: str
    snapshot_id: str
    state: UploadState
    declared_bytes: int
    chunk_bytes: int
    total_chunks: int
    created_at: float
    updated_at: float
    #: index -> chunk digest, so an exact replay is recognisable and a conflicting one is not.
    received: dict[int, str] = field(default_factory=dict)
    received_bytes: int = 0
    failure_code: str | None = None
    #: Bounded processing receipt: identifiers, counts and digests only. Never paths or content.
    result: dict[str, Any] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return len(self.received) == self.total_chunks

    @property
    def missing(self) -> list[int]:
        return [i for i in range(self.total_chunks) if i not in self.received]

    def as_json(self) -> dict[str, Any]:
        return {
            "upload_id": self.upload_id,
            "principal": self.principal,
            "project_key": self.project_key,
            "project_id": self.project_id,
            "snapshot_id": self.snapshot_id,
            "state": self.state.value,
            "declared_bytes": self.declared_bytes,
            "chunk_bytes": self.chunk_bytes,
            "total_chunks": self.total_chunks,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "received": {str(k): v for k, v in sorted(self.received.items())},
            "received_bytes": self.received_bytes,
            "failure_code": self.failure_code,
            "result": self.result,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> UploadRecord:
        return cls(
            upload_id=str(raw["upload_id"]),
            principal=str(raw["principal"]),
            project_key=str(raw["project_key"]),
            project_id=str(raw.get("project_id") or _project_id(str(raw["project_key"]))),
            snapshot_id=str(raw.get("snapshot_id") or f"snap-{raw['upload_id']}"),
            state=UploadState(str(raw["state"])),
            declared_bytes=int(raw["declared_bytes"]),
            chunk_bytes=int(raw["chunk_bytes"]),
            total_chunks=int(raw["total_chunks"]),
            created_at=float(raw["created_at"]),
            updated_at=float(raw["updated_at"]),
            received={int(k): str(v) for k, v in (raw.get("received") or {}).items()},
            received_bytes=int(raw.get("received_bytes") or 0),
            failure_code=raw.get("failure_code"),
            result=dict(raw.get("result") or {}),
        )


def _project_id(project_key: str) -> str:
    """Recover the legacy deterministic pilot id for pre-upgrade staged records.

    Older record JSON lacks ``project_id``. Keeping its prior derivation lets status/sweep finish
    those uploads after upgrade; all newly begun uploads use a random server-owned id below.
    """
    digest = hashlib.sha256(project_key.strip().encode("utf-8")).hexdigest()[:32]
    return f"project-{digest}"


def _new_project_id() -> str:
    """Mint server-owned identity; a display label is never a graph identity."""
    return f"project-{secrets.token_hex(16)}"


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

    def _identity_dir(self) -> Path:
        return self.root.parent / "snapshot-project-identities"

    @staticmethod
    def _valid_project_id(project_id: str) -> bool:
        return (
            project_id.startswith("project-")
            and len(project_id) == 40
            and all(c in "0123456789abcdef" for c in project_id[8:])
        )

    def _resolve_project_id(self, requested: str) -> str:
        """Return a previously registered server id or refuse the claim."""
        directory = self._identity_dir()
        directory.mkdir(parents=True, exist_ok=True)
        candidate = requested.strip()
        if not self._valid_project_id(candidate):
            raise ReceiveError(ERR_PROJECT_IDENTITY, "project identity is malformed")
        path = directory / f"{candidate}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReceiveError(
                ERR_PROJECT_IDENTITY,
                "project identity is not registered on this server",
            ) from exc
        if str(raw.get("project_id") or "") != candidate:
            raise ReceiveError(ERR_PROJECT_IDENTITY, "project identity record is invalid")
        return candidate

    def _register_project_id(self, project_id: str, display_name: str) -> None:
        directory = self._identity_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{project_id}.json"
        payload = json.dumps(
            {"project_id": project_id, "display_name": display_name}, sort_keys=True
        ).encode("utf-8")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise ReceiveError(ERR_PROJECT_IDENTITY, "project identity allocation collided") from exc
        except OSError as exc:
            raise ReceiveError(ERR_PROJECT_IDENTITY, "project identity could not be registered") from exc
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise ReceiveError(ERR_PROJECT_IDENTITY, "project identity could not be registered") from exc

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
        chunk_bytes: int | None = None, project_id: str | None = None,
    ) -> UploadRecord:
        """Reserve an upload. Allocates a record and a quota slot, never the declared size.

        The declared size is a claim used to compute the chunk plan and to refuse an obviously
        impossible upload early. It is not trusted: what actually lands is counted chunk by chunk.
        """
        now = self._clock()
        self.sweep(now=now)

        normalized_project = project_key.strip()
        if not normalized_project or len(normalized_project) > 200:
            raise ReceiveError(
                ERR_PROJECT_KEY, "project key must contain 1..200 non-whitespace characters"
            )
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

        upload_id = secrets.token_hex(16)
        resolved_project_id = (
            self._resolve_project_id(project_id) if project_id else _new_project_id()
        )
        record = UploadRecord(
            upload_id=upload_id,
            principal=principal,
            project_key=normalized_project,
            project_id=resolved_project_id,
            snapshot_id=f"snapshot-{upload_id}",
            state=(UploadState.SEALED if declared_bytes == 0 else UploadState.RECEIVING),
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

    def ensure_project_identity(self, *, upload_id: str, principal: str) -> UploadRecord:
        """Durably register the server-minted id immediately before a successful receipt."""
        record = self._load_owned(upload_id, principal)
        if record.state not in {
            UploadState.SEALED,
            UploadState.EXTRACTING,
            UploadState.SCANNING,
            UploadState.PROMOTING,
            UploadState.READY,
        }:
            raise ReceiveError(ERR_WRONG_STATE, f"upload is {record.state.value}")
        path = self._identity_dir() / f"{record.project_id}.json"
        if path.exists():
            self._resolve_project_id(record.project_id)
            return record
        self._register_project_id(record.project_id, record.project_key)
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
        records = self._all_records()
        active = [r for r in records if r.state is UploadState.RECEIVING]
        reserved = [
            r
            for r in records
            if r.state
            in {
                UploadState.RECEIVING,
                UploadState.SEALED,
                UploadState.EXTRACTING,
                UploadState.SCANNING,
                UploadState.PROMOTING,
            }
        ]

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
        if sum(r.declared_bytes for r in reserved) > self.quotas.disk_budget_bytes:
            raise ReceiveError(ERR_DISK_BUDGET, "staging disk budget exhausted; retry later")
        if (
            sum(r.declared_bytes for r in reserved if r.principal == record.principal)
            > self.quotas.max_reserved_bytes_per_principal
        ):
            raise ReceiveError(
                ERR_DISK_BUDGET,
                "this caller's snapshot reservations reached the shared-budget safety bound",
            )

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

    def blob_path(self, *, upload_id: str, principal: str) -> Path:
        """Return the owned sealed blob path to the coordinator, never to a client."""
        record = self._load_owned(upload_id, principal)
        if record.state not in {
            UploadState.SEALED,
            UploadState.EXTRACTING,
            UploadState.SCANNING,
            UploadState.PROMOTING,
        }:
            raise ReceiveError(ERR_WRONG_STATE, f"upload is {record.state.value}")
        path = self._dir(upload_id) / "blob"
        if not path.is_file():
            failed = replace(
                record,
                state=UploadState.FAILED,
                failure_code=ERR_STAGING_LOST,
                updated_at=self._clock(),
            )
            self._write_record(failed)
            raise ReceiveError(ERR_STAGING_LOST, "staged bytes for this upload are gone")
        return path

    def transition(
        self,
        *,
        upload_id: str,
        principal: str,
        expected: set[UploadState],
        state: UploadState,
        failure_code: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> UploadRecord:
        """Move one owned record through the processing state machine.

        The JSON sidecar remains the authority. The extraction lease supplies cross-process
        exclusion; this transition supplies durable progress and restart-visible outcomes.
        """
        record = self._load_owned(upload_id, principal)
        if record.state not in expected:
            raise ReceiveError(ERR_WRONG_STATE, f"upload is {record.state.value}")
        updated = replace(
            record,
            state=state,
            failure_code=failure_code,
            result=dict(record.result if result is None else result),
            updated_at=self._clock(),
        )
        self._write_record(updated)
        return updated

    def abort(self, *, upload_id: str, principal: str) -> UploadRecord:
        """Cancel an uncommitted upload and drop its bytes. Idempotent.

        A SEALED upload is still abortable before explicit commit. Once processing starts, the
        coordinator must complete or compensate; abort cannot race it and delete its input.
        """
        record = self._load_owned(upload_id, principal)
        if record.state is UploadState.ABORTED:
            return record
        if record.state not in {UploadState.RECEIVING, UploadState.SEALED}:
            raise ReceiveError(ERR_WRONG_STATE, f"upload is {record.state.value}")
        aborted = replace(record, state=UploadState.ABORTED, updated_at=self._clock())
        self._write_record(aborted)
        self._drop_blob(upload_id)
        return aborted

    def drop_blob(self, *, upload_id: str, principal: str) -> None:
        """Release staged archive bytes after a terminal processing receipt is durable."""
        record = self._load_owned(upload_id, principal)
        if record.state not in {UploadState.READY, UploadState.FAILED}:
            raise ReceiveError(ERR_WRONG_STATE, f"upload is {record.state.value}")
        self._drop_blob(upload_id)

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
            if record.state in {
                UploadState.EXTRACTING,
                UploadState.SCANNING,
            } and idle >= self.quotas.inactivity_ttl_s:
                self._write_record(
                    replace(
                        record,
                        state=UploadState.FAILED,
                        failure_code="snapshot.pipeline.abandoned",
                        updated_at=moment,
                    )
                )
                self._drop_blob(record.upload_id)
                expired += 1
                continue
            # PROMOTING is deliberately not timed out here. The graph flip may have committed
            # before the process died, and this disk-only receiver cannot safely call that a
            # failure. A repeated explicit commit reconciles the durable promotion attempt and
            # writes the truthful READY/FAILED receipt.
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
