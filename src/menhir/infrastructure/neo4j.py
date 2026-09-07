"""Neo4j repository adapter."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar


class _Neo4jDriverUnavailable(Exception):
    """Stand-in for the driver's exception types when the `neo4j` package is absent.

    Bound to the three driver exception names so `except (...)` clauses in this module stay
    catchable. Nothing ever raises it, so the real ModuleNotFoundError from `_get_driver`
    propagates with its intended message instead of a TypeError (CF-162).
    """


try:
    from neo4j import GraphDatabase, Driver, Query
    from neo4j.exceptions import ServiceUnavailable, SessionExpired, TransientError
except ModuleNotFoundError as exc:  # pragma: no cover - import guard
    GraphDatabase = None  # type: ignore[assignment]
    Driver = Any  # type: ignore[assignment]
    Query = None  # type: ignore[assignment]
    ServiceUnavailable = _Neo4jDriverUnavailable  # type: ignore[assignment]
    SessionExpired = _Neo4jDriverUnavailable  # type: ignore[assignment]
    TransientError = _Neo4jDriverUnavailable  # type: ignore[assignment]
    _NEO4J_IMPORT_ERROR = exc
else:
    _NEO4J_IMPORT_ERROR = None

_TRANSIENT_RETRIES = 3
_TRANSIENT_BACKOFF_BASE = 0.5

#: Transaction timeout applied to the four SAGA MUTATIONS only (CF-211). Not a global default:
#: saga mutations should not occupy the server indefinitely, while unrelated long-running reads,
#: scans and maintenance queries must not start failing because of it.
#:
#: **Recovery does not depend on this value.** Ownership is not aged out on elapsed time -- an
#: abandonment requires positive evidence that the writer process is dead (see
#: operation_owner.classify_ownership). An earlier version of this comment said recovery "needs a
#: provable bound on the mutations whose ownership it ages out"; that theorem was withdrawn, and
#: nothing in the safety argument now rests on this number.
#:
#: 30s is well above any observed saga mutation. Raising it is safe (recompute the ownership TTL
#: from mutation_window_seconds() so healthy writers are not asked to renew unnecessarily);
#: lowering it risks aborting legitimate work on a large hub.
SAGA_MUTATION_TIMEOUT_S = 30.0

#: Driver-level acquisition budget (neo4j WorkspaceConfig default, 6.2.0). Named here because the
#: saga's ownership TTL is sized against the WHOLE mutation window, and acquisition is part of that
#: window: a call can wait this long before its transaction timeout even starts counting. Sizing
#: only -- see mutation_window_seconds; none of this proves when a writer has stopped.
_CONNECTION_ACQUISITION_TIMEOUT_S = 60.0


def mutation_window_seconds(timeout_s: float, *, statements: int = 1) -> float:
    """Budgeted wall time a bounded mutation is EXPECTED to occupy, across every retry attempt.

    **Not a proven upper bound, and recovery must not treat it as one.** An earlier version of this
    docstring said the ownership TTL had to exceed this figure so that expiry could be read as
    ABANDONED. That reasoning was withdrawn: ``Query(timeout=...)`` bounds the SERVER transaction,
    while the client materialises a lazy result over a socket with no comparable read deadline, so
    no figure computed here establishes that a writer has stopped executing. Recovery now requires
    positive evidence of writer death instead (see ``operation_owner.classify_ownership``), and this
    number is not part of that argument.

    What it is still good for: sizing the ownership TTL so a HEALTHY writer is not asked to renew
    more often than necessary, and so a lease comfortably outlives ordinary work. Getting it wrong
    costs renewal churn or delayed recovery -- it can no longer cost a double-apply.

    Budget per attempt: connection acquisition + the bounded transaction. ``_TRANSIENT_RETRIES - 1``
    sleeps now occur, because the final attempt does not sleep before raising (CF-162). The sum
    below deliberately keeps the extra term as slack: the number bounds a lease, so the slack is
    retained on purpose rather than shrunk to match the reduced sleep count. Deliberately
    pessimistic -- it assumes every attempt burns its full acquisition wait and its full timeout,
    because a TTL derived from an optimistic estimate is the failure this calculation exists to
    prevent.

    ``statements`` is why this is not simply "one timeout": a saga mutation is not always one
    statement. The METRIC_WRITE path issues two (``_write_version`` then ``_link_episodes``), each
    separately bounded and separately retried, so its window is twice a single statement's. Passing
    1 for a genuinely single-statement mutation and the real count otherwise is the difference
    between a TTL that holds and one that expires under a writer still doing legitimate work.

    **Assumes the driver's own auto-commit retry is disabled**, which ``_get_driver`` enforces. With
    it enabled, ``Session.run`` retries once more per attempt entirely outside this loop, so the
    real window would be double what this returns and any TTL derived from it would be too short.
    ``test_neo4j_mutation_budget.py`` asserts the driver is built with it off, so this assumption
    cannot rot silently.
    """
    per_attempt = _CONNECTION_ACQUISITION_TIMEOUT_S + float(timeout_s)
    backoff = sum(_TRANSIENT_BACKOFF_BASE * (2 ** n) for n in range(_TRANSIENT_RETRIES))
    per_statement = per_attempt * _TRANSIENT_RETRIES + backoff
    return per_statement * max(1, int(statements))


logger = logging.getLogger(__name__)
_T = TypeVar("_T")


def _record_and_backoff(exc: Exception, attempt: int, *, label: str) -> None:
    """Log a transient failure and sleep before the next attempt, if one will happen.

    On the final attempt there is no next retry to announce and no sleep to take -- sleeping
    would be pure dead latency before the exception ``execute()`` is about to raise anyway
    (CF-162). The final attempt still logs at WARNING, but the wording says attempts are
    exhausted rather than promising a retry.
    """
    is_last = attempt >= _TRANSIENT_RETRIES - 1
    if is_last:
        logger.warning(
            "Neo4j %s (attempt %d/%d); attempts exhausted, no further retry: %s",
            label, attempt + 1, _TRANSIENT_RETRIES, exc,
        )
        return
    wait = _TRANSIENT_BACKOFF_BASE * (2 ** attempt)
    logger.warning(
        "Neo4j %s (attempt %d/%d), retrying in %.1fs: %s",
        label, attempt + 1, _TRANSIENT_RETRIES, wait, exc,
    )
    time.sleep(wait)


#: Ambient revocation predicate for the saga mutation currently in flight on this context.
#:
#: A ContextVar rather than a parameter threaded through the adapter and repository layers, and that
#: is a correctness argument, not a convenience one. Reaching `execute` from a coordinator means
#: crossing eight signatures across three layers; every one of them is a place a future change can
#: forget to forward the predicate, and a forgotten forward is a SILENT loss of protection that no
#: test of that layer would notice. Set once at the coordinator boundary, the signal cannot be
#: dropped by an intermediate layer that does not know it exists.
#:
#: Copied into worker threads by asyncio.to_thread (which copies the context), so a coordinator
#: dispatched off the event loop still carries it. Deliberately NOT inherited by the heartbeat
#: thread, which is started directly and must not be governed by its own predicate.
_revocation: ContextVar[Any] = ContextVar("menhir_saga_revocation", default=None)


@contextmanager
def revocation_scope(should_continue: Any) -> Any:
    """Publish a revocation predicate for every ``execute`` on this context.

    Restores the previous value on exit rather than clearing it, so nesting is safe and an inner
    saga cannot silently un-protect an outer one.
    """
    token = _revocation.set(should_continue)
    try:
        yield
    finally:
        _revocation.reset(token)


class SagaOwnershipRevoked(RuntimeError):
    """A statement was not dispatched because the caller lost ownership of its operation.

    Lives here rather than in the services layer so infrastructure does not have to import upward.
    Deliberately NOT a Neo4j error: nothing went wrong with the database, and a caller catching
    driver exceptions to retry must not swallow this one -- retrying is the exact thing it exists
    to stop.
    """


@dataclass(frozen=True)
class Neo4jTransaction:
    """Transaction-scoped execute adapter for atomic multi-repository work."""

    _tx: Any = field(repr=False)

    def execute(
        self,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        result = self._tx.run(query, **(params or {}))
        return [record.data() for record in result]


@dataclass
class Neo4jRepository:
    """Thin adapter boundary around a Neo4j driver.

    Lazily creates a single driver instance and reuses it across calls.
    Call ``close()`` when done (or use as a context manager).
    """

    uri: str
    database: str
    user: str
    #: repr=False because this renders through str()/f-strings and at any nesting depth,
    #: and every dataclass holding this one inherits the leak (EpisodeRepository does).
    password: str = field(repr=False)
    _driver: Driver | None = field(default=None, init=False, repr=False)
    _driver_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def _get_driver(self) -> Driver:
        if _NEO4J_IMPORT_ERROR is not None:
            raise ModuleNotFoundError(
                "neo4j is required to create a Neo4jRepository driver."
            ) from _NEO4J_IMPORT_ERROR
        if self._driver is None:
            with self._driver_lock:
                if self._driver is None:
                    # disable_auto_commit_retries: Session.run() otherwise retries once on its own
                    # (MAX_AUTO_COMMIT_RETRIES = 1, for errors the server marks _idempotent), which
                    # is a retry layer OUTSIDE this class's loop and therefore outside the budget
                    # mutation_window_seconds() computes -- a "3 attempt" mutation could execute up
                    # to 6 times. Menhir's own loop already covers those transient errors, so this
                    # removes a duplicate layer rather than removing resilience, and keeps the
                    # attempt count matching the one this class reports.
                    #
                    # NOT a safety control: an earlier version of this comment said a TTL derived
                    # from an under-counted budget would be "too short -- so a live writer's row
                    # could be aged out and replayed underneath it". Rows are no longer aged out on
                    # elapsed time at all, so a miscounted budget costs renewal churn, not a
                    # double-apply.
                    self._driver = GraphDatabase.driver(
                        self.uri,
                        auth=(self.user, self.password),
                        disable_auto_commit_retries=True,
                    )
        return self._driver

    def close(self) -> None:
        """Shut down the underlying driver if open."""
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def __enter__(self) -> "Neo4jRepository":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def client_read_timeout_seconds(self) -> float | None:
        """The client-side socket read deadline actually in force, or None if there is none.

        CF-211's remaining half. ``execute()`` sets a SERVER transaction timeout and then
        materialises a lazy result over a socket; if that read has no deadline, a stalled or
        black-holed connection hangs the caller with its operation PREPARED and its participants
        fenced.

        The driver does apply a socket read timeout -- but ONLY from a hint the server volunteers
        during the handshake (``connection.recv_timeout_seconds``, applied in ``Bolt.hello``).
        Nothing in the driver's own configuration sets one, so whether Menhir has any client
        bound at all is a property of the SERVER, not of this code. That is worth measuring
        rather than assuming: against a server that sends the hint the bound is real, and against
        one that does not there is no client deadline whatsoever.

        Returns the effective timeout in seconds, or None when the connection has no read
        deadline. Returns None on any probe failure too -- an unmeasurable bound is not a bound.
        """
        try:
            with self._get_driver().session(database=self.database) as session:
                result = session.run("RETURN 1 AS ok")
                next(iter(result), None)  # force a live connection to be attached
                connection = getattr(session, "_connection", None)
                socket = getattr(connection, "socket", None)
                raw = getattr(socket, "_socket", None)
                timeout = raw.gettimeout() if raw is not None else None
                result.consume()
                return float(timeout) if timeout else None
        except Exception:  # noqa: BLE001
            logger.warning("Could not probe the client read timeout", exc_info=True)
            return None

    def ping(self) -> bool:
        """Quick connectivity check used by scaffolded startup checks."""
        try:
            with self._get_driver().session(database=self.database) as session:
                result = session.run("RETURN 1 AS ok").single()
                return bool(result and result.get("ok") == 1)
        except Exception as exc:  # pragma: no cover
            logger.warning("Neo4j ping failed: %s", exc)
            return False

    def execute(
        self,
        query: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
        should_continue: Any = None,
        safe_to_reexecute: bool = False,
    ) -> list[dict[str, Any]]:
        """Execute a Cypher statement and return materialized rows.

        Retries on transient Neo4j errors with exponential backoff, but NOT uniformly -- the
        retryable errors fall into two classes with different safety properties (CF-158):

        * ``TransientError`` is raised BY THE SERVER, which has already rolled the transaction
          back. Re-executing is sound, so it is always retried.
        * ``ServiceUnavailable`` and ``SessionExpired`` are CONNECTION failures. They can be
          raised after the server committed -- what was lost is the acknowledgement, not the
          write. The outcome is AMBIGUOUS, and re-executing a statement that already applied is
          a double-apply.

        ``safe_to_reexecute`` is the caller's assertion that running this statement twice is
        indistinguishable from running it once -- a pure read, or a genuinely idempotent write
        (MERGE on a stable key, SET to an absolute value). It defaults to False, so an ambiguous
        failure propagates rather than silently double-applying. Pass True only when re-execution
        is provably harmless; when in doubt, leave it.

        This is deliberately an explicit flag rather than an inference from the query text. A
        heuristic looking for CREATE/SET/MERGE tokens is not a proof: ``CALL`` into a procedure
        can mutate with none of them present, and a safety control that guesses is not one.

        **What this does not fix.** Non-idempotent statements still exist -- counter increments
        (``coalesce(n.hot_count, 0) + 1``) and unguarded node creates carrying a client-generated
        uuid, which duplicate because the Entity uuid property carries no uniqueness constraint.
        This stops the retry loop from re-running them; it does not make them idempotent, and it
        says nothing about retries originating above this layer.

        ``timeout_s`` attaches a SERVER-side transaction timeout (CF-211). Opt-in per call rather
        than a blanket default: saga mutations want their server work bounded, while unrelated
        long-running reads and maintenance scans must not start failing because recovery wanted a
        bound.

        **It does not bound this call.** The server stops the transaction; the client then
        materialises a lazy result over a socket with no comparable read deadline, so nothing here
        establishes when ``execute`` returns. An earlier version of this docstring said saga
        mutations "need a provable upper bound so an ownership claim can be aged out safely" --
        that theorem was withdrawn, and ownership is no longer aged out on elapsed time at all.
        Recovery requires positive evidence that the writer process is dead; see
        ``operation_owner.classify_ownership``.

        It is passed through ``neo4j.Query(query, timeout=...)``, NOT as a keyword to
        ``session.run``. ``Session.run(query, parameters=None, **kwargs)`` treats keywords as CYPHER
        PARAMETERS, so a ``timeout=`` kwarg would be sent as a query parameter named ``timeout`` --
        silently bounding nothing, and colliding with any real parameter of that name.

        The timeout bounds ONE server attempt. The retry loop can spend it up to
        ``_TRANSIENT_RETRIES`` times; see :func:`mutation_window_seconds` for how that budget is
        used to SIZE a lease -- sizing only, never as proof of when a writer stopped.

        ``should_continue`` is a zero-argument predicate consulted **before every attempt**. A saga
        writer passes its ownership heartbeat here so that, once its claim is lost, the retry loop
        stops dispatching new statements instead of continuing to mutate a row another process may
        already be replaying (CF-211 part 2). A statement already in flight cannot be recalled --
        that is what the ``timeout_s`` bound is for; this stops the loop from starting more.

        Raising ``SagaOwnershipRevoked`` rather than returning empty rows is deliberate: an empty
        result is a legitimate query outcome and would be indistinguishable from "the mutation
        matched nothing", which several coordinators interpret as a benign abstention.
        """
        statement: Any = query
        if timeout_s is not None:
            if Query is None:  # pragma: no cover - import guard
                raise RuntimeError("neo4j driver unavailable; cannot apply a transaction timeout")
            statement = Query(query, timeout=float(timeout_s))

        last_exc: Exception | None = None
        for attempt in range(_TRANSIENT_RETRIES):
            # Checked before EVERY attempt, including the first: ownership can be lost between the
            # caller's own check and this call, and the first statement is as capable of a
            # double-apply as the third.
            revoked = should_continue if should_continue is not None else _revocation.get()
            if revoked is not None and not revoked():
                raise SagaOwnershipRevoked(
                    "ownership was lost before attempt "
                    f"{attempt + 1}/{_TRANSIENT_RETRIES}; refusing to dispatch further statements"
                )
            try:
                with self._get_driver().session(database=self.database) as session:
                    result = session.run(statement, **(params or {}))
                    return [record.data() for record in result]
            except (ServiceUnavailable, SessionExpired) as exc:
                # Ambiguous: the server may have committed before the connection died. Retrying
                # is a double-apply unless the caller has asserted re-execution is harmless.
                if not safe_to_reexecute:
                    logger.warning(
                        "Neo4j ambiguous failure (attempt %d/%d); NOT retrying because the "
                        "statement is not declared safe to re-execute: %s",
                        attempt + 1, _TRANSIENT_RETRIES, exc,
                    )
                    raise
                last_exc = exc
                _record_and_backoff(exc, attempt, label="ambiguous failure")
            except TransientError as exc:
                # Server-side and already rolled back, so re-execution is sound regardless of
                # what the statement does.
                last_exc = exc
                _record_and_backoff(exc, attempt, label="transient error")
        raise last_exc  # type: ignore[misc]

    def execute_write(self, work: Callable[[Neo4jTransaction], _T]) -> _T:
        """Run replay-safe graph work in one explicit write transaction.

        This deliberately does not use ``session.execute_write``. Managed transaction functions may
        retry the whole callback after connection loss, but a connection failure at commit time is
        ambiguous: the server may already have committed. Current Menhir's auto-commit path fails
        closed on exactly that ambiguity, and projection lifecycle work must preserve the same rule.

        Server ``TransientError`` values are safe to retry because Neo4j has rolled that transaction
        back. ``ServiceUnavailable`` and ``SessionExpired`` are never retried here. The callback must
        contain graph-local work only; all mutations performed through the supplied adapter commit or
        roll back with the lifecycle fence.
        """
        if not callable(work):
            raise TypeError("execute_write requires a callable transaction body")

        last_exc: Exception | None = None
        for attempt in range(_TRANSIENT_RETRIES):
            revoked = _revocation.get()
            if revoked is not None and not revoked():
                raise SagaOwnershipRevoked(
                    "ownership was lost before transactional attempt "
                    f"{attempt + 1}/{_TRANSIENT_RETRIES}; refusing to dispatch further statements"
                )
            try:
                with self._get_driver().session(database=self.database) as session:
                    tx = session.begin_transaction()
                    try:
                        result = work(Neo4jTransaction(tx))
                        tx.commit()
                        return result
                    except Exception:
                        try:
                            tx.rollback()
                        except Exception:  # noqa: BLE001
                            logger.warning(
                                "Neo4j rollback failed while preserving the original transaction error",
                                exc_info=True,
                            )
                        raise
            except (ServiceUnavailable, SessionExpired) as exc:
                logger.warning(
                    "Neo4j transactional ambiguous failure (attempt %d/%d); NOT retrying: %s",
                    attempt + 1,
                    _TRANSIENT_RETRIES,
                    exc,
                )
                raise
            except TransientError as exc:
                last_exc = exc
                _record_and_backoff(exc, attempt, label="transactional transient error")
        raise last_exc  # type: ignore[misc]
