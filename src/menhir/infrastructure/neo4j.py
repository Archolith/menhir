"""Neo4j repository adapter."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

try:
    from neo4j import GraphDatabase, Driver
    from neo4j.exceptions import ServiceUnavailable, SessionExpired, TransientError
except ModuleNotFoundError as exc:  # pragma: no cover - import guard
    GraphDatabase = None  # type: ignore[assignment]
    Driver = Any  # type: ignore[assignment]
    ServiceUnavailable = type(None)  # type: ignore[assignment,misc]
    SessionExpired = type(None)  # type: ignore[assignment,misc]
    TransientError = type(None)  # type: ignore[assignment,misc]
    _NEO4J_IMPORT_ERROR = exc
else:
    _NEO4J_IMPORT_ERROR = None

_TRANSIENT_RETRIES = 3
_TRANSIENT_BACKOFF_BASE = 0.5

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


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
                    self._driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))
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

    def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Execute a Cypher statement and return materialized rows.

        Retries up to ``_TRANSIENT_RETRIES`` times on transient Neo4j errors
        (connection drops, leader switches) with exponential backoff.
        """
        last_exc: Exception | None = None
        for attempt in range(_TRANSIENT_RETRIES):
            try:
                with self._get_driver().session(database=self.database) as session:
                    result = session.run(query, **(params or {}))
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

    def execute_write(self, work: Callable[[Any], _T]) -> _T:
        """Run ``work`` inside one driver-managed write transaction.

        This is the public transaction seam for operations whose correctness depends on several
        graph statements committing atomically (for example projection-generation fencing plus the
        View write it guards). The Neo4j driver may retry ``work`` on retryable transaction errors, so
        callers must keep the callback limited to transaction-local, replay-safe graph operations;
        external side effects do not belong inside it.
        """
        if not callable(work):
            raise TypeError("execute_write requires a callable transaction body")
        with self._get_driver().session(database=self.database) as session:
            return session.execute_write(work)
