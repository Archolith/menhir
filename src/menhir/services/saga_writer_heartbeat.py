"""Writer-side ownership heartbeat for an in-flight saga (CF-211, part 2).

CF-211 part 1 bounds how long a saga mutation may execute. That is necessary but not sufficient:
a bound of 30s does not tell a reconciler whether a PREPARED operation is alive at second 10 or
abandoned at second 10. Only the writer can answer that, and only by continuing to say so.

The heartbeat must run **independently of the blocking Neo4j call**. The saga coordinators are
synchronous and block inside the driver, so a writer cannot renew its own claim inline -- by the
time control returns, the lease it needed to renew has already lapsed. Hence a thread.

The half that actually protects the graph is the *negative* one. Renewing is only bookkeeping;
what makes recovery safe is that a writer which has LOST its claim stops initiating new work:

    fresh heartbeat            -> a reconciler never replays the row
    heartbeat lost             -> the writer starts nothing new
    bounded in-flight mutation -> the already-dispatched statement must finish within its bound
    expiry after that bound    -> ABANDONED is now safe to replay

An already-dispatched statement cannot be recalled, which is exactly why part 1 exists: recovery
waits out an expiry chosen to exceed the maximum time such a statement could still be running.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from menhir.infrastructure import operation_owner as oo

logger = logging.getLogger(__name__)

#: Renew at a third of the lease, so two consecutive renewals can fail before the claim lapses.
#: Renewing at half-life leaves no margin: one missed tick and the row is already claimable.
_RENEW_DIVISOR = 3

#: Consecutive renewal EXCEPTIONS tolerated before the claim is treated as lost. A raised renewal
#: is ambiguous (the sidecar might be briefly locked), unlike a returned False, which is proof
#: another owner holds the row. Ambiguity gets a small budget; proof gets none.
_MAX_RENEW_ERRORS = 3


class SagaOwnershipLost(RuntimeError):
    """This writer no longer owns the operation it is executing, and must start nothing new.

    Raised into the writer's own path rather than merely logged: a writer that keeps issuing
    statements after losing its claim is precisely the double-apply the ownership model exists to
    prevent, and a return value can be ignored by accident.
    """


class WriterHeartbeat:
    """Keeps this process's claim on one PREPARED operation fresh while it executes.

    Not reusable: one instance per in-flight operation. ``lost`` latches -- once a claim is gone it
    is never silently reacquired, because the row may already belong to a reconciler that has begun
    replaying it.
    """

    def __init__(
        self,
        journal: Any,
        op_id: str,
        *,
        lease_seconds: int = oo.DEFAULT_LEASE_SECONDS,
        owner_token: str | None = None,
    ) -> None:
        self._journal = journal
        self._op_id = str(op_id)
        self._lease_seconds = int(lease_seconds)
        self._owner_token = owner_token or oo.process_owner_token()
        self._interval = max(1.0, self._lease_seconds / _RENEW_DIVISOR)
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None
        self._renew_errors = 0

    # ------------------------------------------------------------------ state

    @property
    def lost(self) -> bool:
        """Whether the claim is known to be gone. Latches once set."""
        return self._lost.is_set()

    def should_continue(self) -> bool:
        """False once ownership is lost. Passed to the driver so retries stop starting."""
        return not self._lost.is_set()

    def raise_if_lost(self) -> None:
        """Abort the writer's path if the claim is gone. Call before any new side effect."""
        if self._lost.is_set():
            raise SagaOwnershipLost(
                f"operation {self._op_id} is no longer owned by {self._owner_token!r}; "
                "refusing to start further work"
            )

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("heartbeat already started")
        self._thread = threading.Thread(
            target=self._loop,
            name=f"menhir-saga-heartbeat-{self._op_id[:12]}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Stop renewing. Safe to call more than once, and safe if never started."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                # Daemon, so it cannot hold up interpreter exit; report rather than hang.
                logger.warning(
                    "saga heartbeat thread for %s did not stop within %ss", self._op_id, timeout
                )

    def _loop(self) -> None:
        # Event.wait doubles as the sleep and the stop signal, so stop() is immediate rather than
        # waiting out a full interval.
        while not self._stop.wait(self._interval):
            try:
                renewed = self._journal.renew_owner_heartbeat(
                    self._op_id, seconds=self._lease_seconds, owner_token=self._owner_token
                )
            except Exception as exc:  # noqa: BLE001
                self._renew_errors += 1
                logger.warning(
                    "saga heartbeat renewal for %s raised (%d/%d): %s",
                    self._op_id, self._renew_errors, _MAX_RENEW_ERRORS, exc,
                )
                if self._renew_errors >= _MAX_RENEW_ERRORS:
                    # Fail closed: repeated ambiguity is treated as loss, because continuing to
                    # mutate on an unverifiable claim is the outcome with the worse failure mode.
                    logger.error(
                        "saga heartbeat for %s could not be verified %d times; treating the claim "
                        "as LOST", self._op_id, self._renew_errors,
                    )
                    self._lost.set()
                    return
                continue

            self._renew_errors = 0
            if not renewed:
                # Proof, not ambiguity: the row is no longer PREPARED-and-ours. Someone committed
                # it, quarantined it, or claimed it.
                logger.error(
                    "saga writer LOST its claim on %s; it must start no further work", self._op_id
                )
                self._lost.set()
                return


@contextmanager
def writer_heartbeat(
    journal: Any,
    op_id: str,
    *,
    lease_seconds: int = oo.DEFAULT_LEASE_SECONDS,
    owner_token: str | None = None,
    enabled: bool = True,
) -> Iterator[WriterHeartbeat | None]:
    """Hold a writer's claim for the duration of the block, stopping the thread on any exit.

    ``enabled=False`` yields None so a caller can be written once and remain inert where a
    heartbeat is not wanted (tests, a single-process deployment that has opted out). Yielding None
    rather than a no-op object keeps "not heartbeating" visible at the call site instead of looking
    like a heartbeat that never fails.
    """
    if not enabled:
        yield None
        return

    beat = WriterHeartbeat(
        journal, op_id, lease_seconds=lease_seconds, owner_token=owner_token
    )
    beat.start()
    try:
        yield beat
    finally:
        beat.stop()


__all__ = ["SagaOwnershipLost", "WriterHeartbeat", "writer_heartbeat"]
