"""Who is allowed to extract a snapshot, and for how long.

Plan: `.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md` (P3).
Design: `.agent/plans/menhir-snapshot-p3-extraction-design-2026-09-17.md` (decision 4).

The lease stops two extractions of one project interleaving into one root. It binds **snapshot,
owner and generation** -- never a project name alone -- so that evidence is tied to the entity,
version and holder it proves, and a fact about owner A can never silently become a fact about
owner B.

**The generation is the whole mechanism**, and it is why a claim is an exclusive file create rather
than a write. Each claim writes `<generation>.json` with `O_CREAT|O_EXCL`: two workers that both
read "the last lease expired" both compute the same next generation, both attempt to create it, and
the filesystem lets exactly one succeed. That is a real compare-and-swap, and it is the same lesson
as the P2B quota fix -- reading state and then writing it is not a decision, it is a race.

**The store is the authority on time, not the holder.** `check` reads the stored lease and judges
against that, so a worker whose clock is wrong, or which was paused and resumed, is refused by
supersession before its own expiry is even considered.

**No process identity is recorded, deliberately.** A PID is reused, and a PID in another namespace
is a different process entirely, so "the holder must be dead by now" is an inference about a
distributed system dressed up as a fact. The only evidence of death this module accepts is a lease
that expired, which is durable and checkable by anyone.

**A crash must not block the phase.** There is no lock to be left held: an abandoned lease simply
expires and the project becomes claimable with no operator action. That is the argument against a
bare lock file, and it is asserted in `test_a_crashed_holder_does_not_block_the_project_forever`
rather than only claimed here.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

__all__ = [
    "ERR_LEASE_EXPIRED",
    "ERR_LEASE_HELD",
    "ERR_LEASE_NOT_HELD",
    "ERR_LEASE_SUPERSEDED",
    "ExtractionLease",
    "LeaseError",
    "LeaseStore",
]

ERR_LEASE_HELD = "snapshot.lease.held"
ERR_LEASE_EXPIRED = "snapshot.lease.expired"
ERR_LEASE_SUPERSEDED = "snapshot.lease.superseded"
ERR_LEASE_NOT_HELD = "snapshot.lease.not_held"

#: Long enough that an ordinary extraction never renews under load, short enough that a crashed
#: holder does not strand a project for an operator-visible length of time.
DEFAULT_LEASE_TTL_S = 15 * 60


class LeaseError(RuntimeError):
    """A refusal carrying a stable code. Never carries a project key or an upload id."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExtractionLease:
    """Authority to extract one snapshot, for one owner, for a bounded time.

    Deliberately carries no process identity -- see the module docstring.
    """

    project_key: str
    upload_id: str
    owner: str
    generation: int
    expires_at: float


class LeaseStore:
    def __init__(
        self,
        root: Path,
        *,
        ttl_s: float = DEFAULT_LEASE_TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root)
        self.ttl_s = ttl_s
        self._clock = clock

    # -- layout -----------------------------------------------------------------------------

    def _project_dir(self, project_key: str) -> Path:
        # Hashed, never joined raw: `project_key` is caller-supplied, so using it as a path
        # component would hand an attacker the directory layout. The hash is not a secret and
        # does not need to be -- it exists to make the name structural.
        digest = hashlib.sha256(project_key.encode("utf-8")).hexdigest()[:32]
        return self.root / digest

    def _current(self, project_key: str) -> ExtractionLease | None:
        """The highest-generation lease on record, or None.

        Highest rather than most-recently-modified: mtime is a filesystem fact and generations are
        ours, and only one of those is monotonic under a clock change.
        """
        directory = self._project_dir(project_key)
        if not directory.is_dir():
            return None
        best: ExtractionLease | None = None
        for child in directory.iterdir():
            if child.suffix != ".json":
                continue
            try:
                raw = json.loads(child.read_text(encoding="utf-8"))
                lease = ExtractionLease(
                    project_key=project_key,
                    upload_id=str(raw["upload_id"]),
                    owner=str(raw["owner"]),
                    generation=int(raw["generation"]),
                    expires_at=float(raw["expires_at"]),
                )
            except (OSError, ValueError, KeyError, TypeError):
                # An unreadable lease file is not evidence of anything, so it is ignored rather
                # than trusted or treated as a claim (invariant 11).
                continue
            if best is None or lease.generation > best.generation:
                best = lease
        return best

    def _write_exclusive(self, lease: ExtractionLease) -> None:
        """Create this generation's file, or fail because someone else already did.

        `O_CREAT | O_EXCL` is the entire concurrency control. Anything that reads and then writes
        -- including "check it is free, then claim it" -- is the check-then-act that P2B's quota
        bug was.
        """
        directory = self._project_dir(lease.project_key)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{lease.generation:012d}.json"
        payload = json.dumps(
            {
                "upload_id": lease.upload_id,
                "owner": lease.owner,
                "generation": lease.generation,
                "expires_at": lease.expires_at,
            },
            sort_keys=True,
        )
        try:
            handle = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise LeaseError(
                ERR_LEASE_HELD, "another worker claimed this project first"
            ) from exc
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)

    # -- operations -------------------------------------------------------------------------

    def acquire(
        self, *, project_key: str, upload_id: str, owner: str
    ) -> ExtractionLease:
        """Claim the project for this snapshot and owner, or refuse."""
        now = self._clock()
        current = self._current(project_key)
        if current is not None and now < current.expires_at:
            raise LeaseError(ERR_LEASE_HELD, "this project is being extracted")

        generation = (current.generation if current else 0) + 1
        lease = ExtractionLease(
            project_key=project_key,
            upload_id=upload_id,
            owner=owner,
            generation=generation,
            expires_at=now + self.ttl_s,
        )
        self._write_exclusive(lease)
        return lease

    def check(self, lease: ExtractionLease) -> None:
        """Confirm `lease` is still authority, or raise.

        Order matters. Supersession is judged BEFORE expiry, because the stored lease is the
        authority on both and a holder whose clock is wrong -- paused, migrated, resumed -- would
        otherwise conclude from its own expiry that it is still fine.
        """
        current = self._current(lease.project_key)
        if current is None:
            raise LeaseError(ERR_LEASE_NOT_HELD, "no lease is held for this project")
        if current.generation != lease.generation:
            raise LeaseError(ERR_LEASE_SUPERSEDED, "this lease has been superseded")
        if current.owner != lease.owner or current.upload_id != lease.upload_id:
            # Same generation, different holder or different snapshot: not ours. Distinct from
            # supersession because it means the caller is holding evidence about something else
            # entirely, not an older turn of the same thing.
            raise LeaseError(
                ERR_LEASE_NOT_HELD, "this lease is not held by this caller"
            )
        if self._clock() >= current.expires_at:
            raise LeaseError(ERR_LEASE_EXPIRED, "this lease has expired")

    def renew(self, lease: ExtractionLease) -> ExtractionLease:
        """Extend a lease this caller still holds.

        Renewal is NOT a second chance to acquire: `check` runs first, so a holder that expired and
        was superseded cannot renew its way back into a project another worker is extracting.
        """
        self.check(lease)
        renewed = replace(lease, expires_at=self._clock() + self.ttl_s)
        target = self._project_dir(lease.project_key) / f"{lease.generation:012d}.json"
        target.write_text(
            json.dumps(
                {
                    "upload_id": renewed.upload_id,
                    "owner": renewed.owner,
                    "generation": renewed.generation,
                    "expires_at": renewed.expires_at,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return renewed

    def release(self, lease: ExtractionLease) -> None:
        """Give up a lease this caller still holds.

        `check` first, and that is the point of this method existing at all. A release keyed on the
        project alone lets a stale holder -- expired, superseded, waking up and tidying after
        itself -- free the CURRENT holder's lease while it is mid-extraction, after which a third
        worker claims the project underneath it.
        """
        self.check(lease)
        # Expired in place, NOT deleted. Deleting it was the first implementation and the
        # release/re-acquire test caught the consequence: with the file gone, `_current` sees
        # nothing and the next claim computes generation 1 again. A generation that can be reused
        # is not a version -- a stale lease object from the previous turn would match the new one
        # on generation, and the only thing still separating them would be the owner field.
        #
        # Leaving a tombstone keeps generations monotonic for the life of the project, so evidence
        # from turn N can never be mistaken for authority in turn N+1.
        target = self._project_dir(lease.project_key) / f"{lease.generation:012d}.json"
        target.write_text(
            json.dumps(
                {
                    "upload_id": lease.upload_id,
                    "owner": lease.owner,
                    "generation": lease.generation,
                    "expires_at": 0.0,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
