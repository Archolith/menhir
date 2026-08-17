"""The reconciliation gate: exclusive backlog ownership plus a global PREPARE pause (CF-20c).

Two different hazards need two different controls, and conflating them is how a recovery pass ends
up looking safe while being wrong:

* **reconciler vs reconciler** -- two instances restarting together both see operation X as PREPARED.
  Final-state CAS does not save this, because both may perform graph side effects before either
  journal transition wins. Solved by holding a named lease, which is what this module owns.
* **reconciler vs a still-live writer** -- process A prepared X and is mid-mutation while process B
  starts recovery. Solved by per-operation ownership (`infrastructure/operation_owner.py`), NOT by
  this lease. A reconciler can hold this gate legitimately and still be wrong to replay X.

Holding this gate additionally *pauses new saga PREPARE across the deployment*: the journal checks
the same lease row inside its own BEGIN IMMEDIATE, so a writer cannot slip a new PREPARED row in
after recovery has decided what the backlog contains. Both sides contend for one SQLite write lock,
which is what makes the pause real rather than advisory.

Nothing here replays anything. Acquiring the gate is what makes replay *safe to attempt*; the
attempt itself is still gated on live activation.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from menhir.infrastructure import operation_owner as oo
from menhir.infrastructure.graph_operations import RECONCILIATION_LEASE_NAME
from menhir.services.scheduler_lease import SchedulerLeaseStore

logger = logging.getLogger(__name__)

#: Default gate TTL. Long enough that a normal recovery pass never has to renew mid-flight, short
#: enough that a hard-killed reconciler stops pausing writes on its own. Renewal exists for the
#: passes that do run long; see `renew`.
DEFAULT_GATE_SECONDS = 120.0


class ReconciliationLeaseLost(RuntimeError):
    """This process no longer owns the reconciliation gate and must stop before its next effect.

    Raised rather than returned in the paths where continuing would be a correctness violation:
    once the gate is gone, another reconciler may already be replaying the same rows, and the
    PREPARE pause this process was relying on is no longer in force.
    """


@dataclass
class ReconciliationGate:
    """Holds the named reconciliation lease for this process."""

    lease_store: SchedulerLeaseStore = field(default_factory=SchedulerLeaseStore)
    owner_id: str = field(default_factory=oo.process_owner_token)
    lease_duration_s: float = DEFAULT_GATE_SECONDS
    _held: bool = field(default=False, init=False, repr=False)

    @property
    def held(self) -> bool:
        """Whether THIS object believes it holds the gate. Not proof -- see `verify_still_held`."""
        return self._held

    def acquire(self) -> bool:
        """Take the gate. False means another reconciler already owns it.

        False is a normal outcome, not an error: the other instance is doing the work, and the
        correct response is to skip recovery rather than to contend for it.
        """
        acquired = self.lease_store.try_acquire(
            lease_name=RECONCILIATION_LEASE_NAME,
            owner_id=self.owner_id,
            owner_pid=os.getpid(),
            lease_duration_s=self.lease_duration_s,
        )
        self._held = bool(acquired)
        if acquired:
            logger.info("Acquired reconciliation gate as %s; saga PREPARE is paused", self.owner_id)
        else:
            holder = self.holder() or {}
            logger.info(
                "Reconciliation gate already held by %s (pid %s); skipping recovery this start",
                holder.get("owner_id"), holder.get("owner_pid"),
            )
        return bool(acquired)

    def renew(self) -> bool:
        """Extend the gate. False means it was LOST, which the caller must treat as fatal.

        Deliberately does not re-acquire. A gate that expired may already have been taken by another
        reconciler that has begun replaying the same backlog; silently taking it back would put two
        reconcilers on the same rows with neither aware of the other.
        """
        renewed = self.lease_store.renew(
            lease_name=RECONCILIATION_LEASE_NAME,
            owner_id=self.owner_id,
            owner_pid=os.getpid(),
            lease_duration_s=self.lease_duration_s,
        )
        if not renewed:
            self._held = False
            logger.warning(
                "Lost the reconciliation gate (owner %s); recovery must stop before its next effect",
                self.owner_id,
            )
        return bool(renewed)

    def verify_still_held(self) -> None:
        """Assert ownership before a side effect. Raises ReconciliationLeaseLost if it is gone.

        Checks the durable row rather than `self._held`, because the interesting failure is exactly
        the one this object cannot observe locally: the TTL lapsed, or an operator forced a takeover,
        while this process believed it still owned the gate.
        """
        holder = self.holder()
        if holder is None or holder.get("owner_id") != self.owner_id:
            self._held = False
            raise ReconciliationLeaseLost(
                f"reconciliation gate is no longer owned by {self.owner_id!r} "
                f"(now {(holder or {}).get('owner_id')!r}); refusing to continue"
            )

    def release(self) -> None:
        """Give up the gate, re-admitting saga writers. Safe to call when not held."""
        self.lease_store.release(
            lease_name=RECONCILIATION_LEASE_NAME, owner_id=self.owner_id
        )
        if self._held:
            logger.info("Released reconciliation gate; saga PREPARE is admitted again")
        self._held = False

    def holder(self) -> dict[str, Any] | None:
        """The current lease row, whoever owns it, or None if the gate is free."""
        row = self.lease_store.fetch(lease_name=RECONCILIATION_LEASE_NAME)
        return dict(row) if row else None


@contextmanager
def reconciliation_gate(
    *,
    lease_store: SchedulerLeaseStore | None = None,
    lease_duration_s: float = DEFAULT_GATE_SECONDS,
) -> Iterator[ReconciliationGate | None]:
    """Hold the gate for the duration of the block, releasing it even on failure.

    Yields None when the gate could not be taken, so a caller can skip recovery without
    distinguishing exception types. The release in `finally` is the important part: a leaked gate
    pauses saga PREPARE across the whole deployment until its TTL lapses, which turns a crashed
    recovery pass into an outage for every writer.
    """
    gate = ReconciliationGate(
        lease_store=lease_store or SchedulerLeaseStore(),
        lease_duration_s=lease_duration_s,
    )
    if not gate.acquire():
        yield None
        return
    try:
        yield gate
    finally:
        gate.release()


__all__ = [
    "DEFAULT_GATE_SECONDS",
    "ReconciliationGate",
    "ReconciliationLeaseLost",
    "reconciliation_gate",
]
