"""Neo4j repository adapter."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

try:
    from neo4j import GraphDatabase, Driver, Query
    from neo4j.exceptions import ServiceUnavailable, SessionExpired, TransientError
except ModuleNotFoundError as exc:  # pragma: no cover - import guard
    GraphDatabase = None  # type: ignore[assignment]
    Driver = Any  # type: ignore[assignment]
    Query = None  # type: ignore[assignment]
    ServiceUnavailable = type(None)  # type: ignore[assignment,misc]
    SessionExpired = type(None)  # type: ignore[assignment,misc]
    TransientError = type(None)  # type: ignore[assignment,misc]
    _NEO4J_IMPORT_ERROR = exc
else:
    _NEO4J_IMPORT_ERROR = None

_TRANSIENT_RETRIES = 3
_TRANSIENT_BACKOFF_BASE = 0.5

#: Transaction timeout applied to the four SAGA MUTATIONS only (CF-211). Not a global default:
#: recovery needs a provable bound on the mutations whose ownership it ages out, while unrelated
#: long-running reads, scans and maintenance queries must not start failing because of it.
#:
#: 30s is well above any observed saga mutation and still finite, which is the only property the
#: safety argument needs. Raising it is safe as long as the ownership TTL is recomputed from
#: mutation_window_seconds(); lowering it risks aborting legitimate work on a large hub.
SAGA_MUTATION_TIMEOUT_S = 30.0

#: Driver-level acquisition budget (neo4j WorkspaceConfig default, 6.2.0). Named here because the
#: saga's ownership TTL must be computed against the WHOLE mutation window, and acquisition is part
#: of that window: a call can wait this long before its transaction timeout even starts counting.
_CONNECTION_ACQUISITION_TIMEOUT_S = 60.0


def mutation_window_seconds(timeout_s: float, *, statements: int = 1) -> float:
    """Worst-case wall time a bounded mutation can occupy, across every retry attempt (CF-211).

    The ownership TTL must exceed THIS, not one attempt. An operation whose lease expires while it
    is still legitimately executing would be classified ABANDONED and replayed by a reconciler,
    which is the double-apply the ownership model exists to prevent.

    Budget per attempt: connection acquisition + the bounded transaction. Between attempts Menhir
    sleeps ``_TRANSIENT_BACKOFF_BASE * 2**n``. Deliberately pessimistic -- it assumes every attempt
    burns its full acquisition wait and its full timeout, because a TTL derived from an optimistic
    estimate is the failure this calculation exists to prevent.

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


class SagaOwnershipRevoked(RuntimeError):
    """A statement was not dispatched because the caller lost ownership of its operation.

    Lives here rather than in the services layer so infrastructure does not have to import upward.
    Deliberately NOT a Neo4j error: nothing went wrong with the database, and a caller catching
    driver exceptions to retry must not swallow this one -- retrying is the exact thing it exists
    to stop.
    """


@dataclass
class Neo4jRepository:
    """Thin adapter boundary around a Neo4j driver.

    Lazily creates a single driver instance and reuses it across calls.
    Call ``close()`` when done (or use as a context manager).
    """

    uri: str
    database: str
    user: str
    password: str
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
                    # mutation_window_seconds() computes. With it on, a "3 attempt" mutation could
                    # execute up to 6 times, and an ownership TTL derived from that budget would be
                    # too short -- so a live writer's row could be aged out and replayed underneath
                    # it (CF-211). Menhir's own loop already covers the transient errors, so this
                    # removes a duplicate layer rather than removing resilience.
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
    ) -> list[dict[str, Any]]:
        """Execute a Cypher statement and return materialized rows.

        Retries up to ``_TRANSIENT_RETRIES`` times on transient Neo4j errors
        (connection drops, leader switches) with exponential backoff.

        ``timeout_s`` attaches a server-side transaction timeout (CF-211). Opt-in per call rather
        than a blanket default: the saga mutations need a provable upper bound so an ownership claim
        can be aged out safely, while unrelated long-running reads and maintenance scans must not
        start failing because recovery wanted a bound.

        It is passed through ``neo4j.Query(query, timeout=...)``, NOT as a keyword to
        ``session.run``. ``Session.run(query, parameters=None, **kwargs)`` treats keywords as CYPHER
        PARAMETERS, so a ``timeout=`` kwarg would be sent as a query parameter named ``timeout`` --
        silently bounding nothing, and colliding with any real parameter of that name.

        The timeout bounds ONE attempt. The retry loop can spend it up to ``_TRANSIENT_RETRIES``
        times; see :func:`mutation_window_seconds` for the window a lease must actually exceed.

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
            if should_continue is not None and not should_continue():
                raise SagaOwnershipRevoked(
                    "ownership was lost before attempt "
                    f"{attempt + 1}/{_TRANSIENT_RETRIES}; refusing to dispatch further statements"
                )
            try:
                with self._get_driver().session(database=self.database) as session:
                    result = session.run(statement, **(params or {}))
                    return [record.data() for record in result]
            except (ServiceUnavailable, SessionExpired, TransientError) as exc:
                last_exc = exc
                wait = _TRANSIENT_BACKOFF_BASE * (2 ** attempt)
                logger.warning(
                    "Neo4j transient error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1, _TRANSIENT_RETRIES, wait, exc,
                )
                time.sleep(wait)
        raise last_exc  # type: ignore[misc]
