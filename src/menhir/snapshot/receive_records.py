"""Data vocabulary for the P2A staging receiver: error codes, upload states, quotas, records.

Split out of ``menhir.snapshot.receive`` verbatim so that module stays under the file-size cap;
``receive`` re-exports every name here, so imports keep going through the original path. See the
receiver's docstring for the plan invariants this vocabulary serves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "ERR_CHUNK_CONFLICT",
    "ERR_CHUNK_DIGEST",
    "ERR_CHUNK_ENCODING",
    "ERR_CHUNK_INDEX",
    "ERR_CHUNK_LENGTH",
    "ERR_DISK_BUDGET",
    "ERR_NOT_FOUND",
    "ERR_SIZE",
    "ERR_STAGING_LOST",
    "ERR_TOO_MANY_PER_PRINCIPAL",
    "ERR_TOO_MANY_PER_PROJECT",
    "ERR_WRONG_STATE",
    "ReceiveError",
    "StagingQuotas",
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


class UploadState(str, Enum):
    """P2A reaches SEALED and stops. EXTRACTING and everything past it belong to P3."""

    RECEIVING = "RECEIVING"
    SEALED = "SEALED"
    ABORTED = "ABORTED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"

    @property
    def terminal(self) -> bool:
        return self in (UploadState.SEALED, UploadState.ABORTED, UploadState.EXPIRED, UploadState.FAILED)


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


@dataclass(frozen=True)
class UploadRecord:
    """Everything known about one upload. Ids, counts, digests, timestamps -- no content."""

    upload_id: str
    principal: str
    project_key: str
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
            "state": self.state.value,
            "declared_bytes": self.declared_bytes,
            "chunk_bytes": self.chunk_bytes,
            "total_chunks": self.total_chunks,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "received": {str(k): v for k, v in sorted(self.received.items())},
            "received_bytes": self.received_bytes,
            "failure_code": self.failure_code,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> UploadRecord:
        return cls(
            upload_id=str(raw["upload_id"]),
            principal=str(raw["principal"]),
            project_key=str(raw["project_key"]),
            state=UploadState(str(raw["state"])),
            declared_bytes=int(raw["declared_bytes"]),
            chunk_bytes=int(raw["chunk_bytes"]),
            total_chunks=int(raw["total_chunks"]),
            created_at=float(raw["created_at"]),
            updated_at=float(raw["updated_at"]),
            received={int(k): str(v) for k, v in (raw.get("received") or {}).items()},
            received_bytes=int(raw.get("received_bytes") or 0),
            failure_code=raw.get("failure_code"),
        )
