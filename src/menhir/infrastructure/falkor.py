"""Internal FalkorDBLite repository. Native queries only; not a Neo4j compatibility layer.

Instants use Graphiti's UTC ISO string representation, with Python microsecond precision.
Callers bind canonical instants explicitly; arbitrary strings and query text are never rewritten.
This backend is not yet selectable by production configuration.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from menhir.domain.temporal import parse_iso8601
from menhir.infrastructure.neo4j import SagaOwnershipRevoked, _revocation


def decode_instant(value: datetime | str) -> datetime:
    """Decode an instant without discarding offset or sub-microsecond precision."""
    if not isinstance(value, (datetime, str)):
        raise TypeError("Falkor instant must be a datetime or ISO timestamp")
    if isinstance(value, str) and re.search(r"\.\d{7,}", value):
        raise ValueError("Falkor instant exceeds the shared microsecond precision")
    parsed = value if isinstance(value, datetime) else parse_iso8601(value)
    if parsed is None:
        raise ValueError("invalid Falkor instant")
    # Matches Graphiti ensure_utc and Menhir's existing naive-time convention.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def encode_instant(value: datetime | str) -> str:
    """Canonical UTC spelling shared with the pinned Graphiti Falkor driver."""
    return decode_instant(value).isoformat()


@dataclass
class FalkorLiteRepository:
    """Own one Lite server; direct queries and Graphiti share its socket and graph.

    Close the Graphiti client before closing this owner, as the runtime already does for Neo4j.
    Transport failures are never retried, even for callers permitting re-execution. Arbitrary
    multi-statement callbacks are refused before invocation; atomic operations need native owners.
    """

    path: Path
    database: str = "menhir"
    _db: Any = field(default=None, init=False, repr=False)
    _driver: Any = field(default=None, init=False, repr=False)
    _lock: Any = field(default_factory=RLock, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path).resolve()
        if not self.database.strip():
            raise ValueError("Falkor graph name must not be blank")

    def _get_db(self) -> Any:
        with self._lock:
            if self._closed:
                raise RuntimeError("Falkor repository is closed")
            if self._db is None:
                from redis.backoff import NoBackoff
                from redis.retry import Retry
                from redislite.falkordb_client import FalkorDB

                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._db = FalkorDB(str(self.path), retry=Retry(NoBackoff(), 0))
            return self._db

    def graphiti_driver(self) -> Any:
        """Create the existing async provider on this server, not a second embedded runtime."""
        with self._lock:
            db = self._get_db()
            if self._driver is None:
                from falkordb.asyncio import FalkorDB
                from graphiti_core.driver.falkordb_driver import FalkorDriver
                from redis.asyncio.retry import Retry
                from redis.backoff import NoBackoff
                from redis.retry import Retry as SyncRetry

                # The SDK first runs a synchronous INFO probe with the supplied connection
                # options. Install its async retry policy only after that constructor returns.
                client = FalkorDB(unix_socket_path=db.client.socket_file, retry=SyncRetry(NoBackoff(), 0))
                client.connection.set_retry(Retry(NoBackoff(), 0))
                self._driver = FalkorDriver(falkor_db=client, database=self.database)
            return self._driver

    def execute(
        self,
        query: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
        should_continue: Any = None,
        safe_to_reexecute: bool = False,
    ) -> list[dict[str, Any]]:
        """Execute one native atomic command; retain ambiguity and ownership protections."""
        timeout_ms = None
        if timeout_s is not None:
            if isinstance(timeout_s, bool) or not math.isfinite(timeout_s) or timeout_s <= 0:
                raise ValueError("Falkor timeout must be positive and finite")
            timeout_ms = math.ceil(timeout_s * 1000)
        graph = self._get_db().select_graph(self.database)
        for predicate in (_revocation.get(), should_continue):
            if predicate is not None and not predicate():
                raise SagaOwnershipRevoked("ownership lost before Falkor dispatch")
        from graphiti_core.utils.datetime_utils import convert_datetimes_to_strings

        # No retry is safer than automatic replay after an ambiguous socket failure. The permission
        # to re-execute is accepted for interface compatibility, not interpreted as a requirement.
        result = graph.query(query, convert_datetimes_to_strings(params or {}), timeout=timeout_ms)
        columns = [column[1] for column in result.header]
        return [dict(zip(columns, row, strict=True)) for row in result.result_set]

    def execute_write(self, work: Any, *, timeout_s: float | None = None) -> Any:
        raise NotImplementedError("Falkor callbacks require an operation-specific atomic native command")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._db is not None:
                self._db.client.save()
                self._db.close()
            self._closed = True
