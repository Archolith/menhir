"""Internal FalkorDBLite repository. Native queries only; not a Neo4j compatibility layer.

Instants use Graphiti's UTC ISO string representation, with Python microsecond precision.
Callers bind canonical instants explicitly; execute never translates queries or stored values.
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

                class SharedGraphDriver(FalkorDriver):
                    """Menhir tenants are group_id properties within the owned graph."""

                    def clone(self, database: str) -> Any:
                        # Graphiti requests a database clone for each logical group. Menhir's
                        # direct repositories and policy joins must see those same records.
                        return self

                self._driver = SharedGraphDriver(falkor_db=client, database=self.database)
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

    def publish_project(self, claim: Any, scan: Any, session_id: str, user_id: str) -> dict[str, int]:
        """Publish this specific structure operation in one guarded native command."""
        from menhir.infrastructure.structure_write_fence import StaleStructureScan

        query, params = _project_publication_statement(self, claim, scan, session_id, user_id)
        rows = self.execute(query, params)
        if not rows:
            raise StaleStructureScan("Project identity, scan generation or mtime inputs changed before publication")
        return {"entities": int(rows[0]["entities"]), "edges": int(rows[0]["edges"])}

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._db is not None:
                self._db.client.save()
                self._db.close()
            self._closed = True


_PROJECT_PARAMETER_TOKENS = re.compile(
    r"'(?:''|\\.|[^'\\])*'|\"(?:\"\"|\\.|[^\"\\])*\"|`(?:``|[^`])*`|//[^\n]*|/\*.*?\*/"
    r"|\$(?P<parameter>[A-Za-z_][A-Za-z0-9_]*)",
    re.DOTALL,
)
_PROJECT_TERMINAL_COUNT = re.compile(r"\bRETURN\s+count\([^)]*\)\s+AS\s+(cnt|deleted|updated)\s*$", re.IGNORECASE)


def _bind_project_parameters(query: str, parameters: dict[str, Any], name: str) -> str:
    """Namespace parameters for the fixed project operation, preserving literals/comments."""

    def replace(match: re.Match[str]) -> str:
        parameter = match.group("parameter")
        if parameter is not None:
            if parameter == "project_now":
                return match.group(0)
            if parameter not in parameters:
                raise ValueError(f"missing structure parameter {parameter}")
            return f"${name}.{parameter}"
        return match.group(0)

    return _PROJECT_PARAMETER_TOKENS.sub(replace, query)


def _project_publication_statement(
    repository: Any,
    claim: Any,
    scan: Any,
    session_id: str,
    user_id: str,
) -> tuple[str, dict[str, Any]]:
    """Prepare only StructureGraphWriter's project operation, never arbitrary callbacks.

    Mtime reads determine the shared Python incremental/partial policy. Recheck the exact
    inputs in the atomic command. Write results used only for diagnostic counters are
    deferred; batch-edge counts and the exact-one completion assertion execute server-side.
    """
    from menhir.infrastructure.project_identity_binding import root_key_for
    from menhir.infrastructure.structure_queries import StructureGraphWriter
    from menhir.infrastructure.structure_write_fence import (
        _STRUCTURE_PUBLICATION_GUARD_CYPHER,
    )

    if (
        scan.project_id != claim.project_id
        or scan.identity_generation != claim.generation
        or root_key_for(scan.root_path) != claim.root_key
    ):
        raise ValueError("scan identity disagrees with publication claim")
    if type(scan.scan_generation) is not int or scan.scan_generation < 1:
        raise ValueError("project publication requires a positive scan generation")
    reads: list[tuple[str, dict[str, Any], list[dict[str, Any]]]] = []
    paths: set[str] = set()

    class ReadCapture:
        def execute(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            rows = repository.execute(query, params)
            for row in rows:
                if not row.get("path") or row["path"] in paths:
                    raise ValueError("ambiguous structural mtime identity")
                paths.add(row["path"])
            reads.append((query, dict(params), sorted(rows, key=lambda row: (row["path"], row["mtime"]))))
            return rows

    mtimes = StructureGraphWriter(ReadCapture()).get_file_mtimes(scan.name, scan.project_id)
    steps: list[tuple[str, dict[str, Any], str, bool]] = []

    class WriteCapture:
        edge_batch = False

        def execute(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            # Unexpected reads must not be answered with fabricated application state.
            plain = _PROJECT_PARAMETER_TOKENS.sub("", query)
            if not re.search(r"\b(MERGE|CREATE|SET|DELETE|REMOVE)\b", plain):
                raise ValueError("unrecognized project read during write planning")
            match = _PROJECT_TERMINAL_COUNT.search(query.strip())
            alias = match.group(1).lower() if match else "project_rows"
            steps.append((query, dict(params), alias, self.edge_batch))
            return [{"cnt": 0, "deleted": 0, "updated": 1}]

    capture = WriteCapture()

    class Planner(StructureGraphWriter):
        @property
        def _edge_clock_expression(self) -> str:
            return "$project_now"

        def get_file_mtimes(self, project_name: str, project_id: str | None = None) -> dict[str, float]:
            if project_name != scan.name or project_id != scan.project_id:
                raise ValueError("unrecognized project mtime read")
            return dict(mtimes)

        def _write_edges_batch(self, *args: Any, **kwargs: Any) -> int:
            capture.edge_batch = True
            try:
                return super()._write_edges_batch(*args, **kwargs)
            finally:
                capture.edge_batch = False

    counts = Planner(capture).write_project(scan, session_id, user_id)
    if not steps or steps[-1][2] != "updated" or any(step[2] == "updated" for step in steps[:-1]):
        raise ValueError("unrecognized project completion contract")
    parameters: dict[str, Any] = {
        "project_id": claim.project_id,
        "project_entities": counts["entities"],
        "project_fixed_edges": counts["edges"],
    }
    parameters["project_now"] = encode_instant(datetime.now(UTC))
    parameters.update({
        "project_id": claim.project_id, "host": claim.host, "root_key": claim.root_key,
        "identity_generation": claim.generation, "scan_generation": scan.scan_generation,
    })
    parts = [_STRUCTURE_PUBLICATION_GUARD_CYPHER, "WITH p AS identity, true AS gate"]
    for i, (query, params, rows) in enumerate(reads):
        name = f"read{i}"
        parameters[name], parameters[f"snapshot{i}"] = params, rows
        body = _bind_project_parameters(query, params, name)
        parts.append(f"""
CALL {{ WITH gate
    CALL {{ WITH gate WITH gate WHERE gate {body} }}
    WITH path, mtime ORDER BY path, mtime
    RETURN collect({{path:path,mtime:mtime}}) AS actual{i}
}}
WITH identity, gate, actual{i}
WHERE coalesce(actual{i}=$snapshot{i},false)
""")
    parts.append("WITH 0 AS stage_initial")
    previous = "stage_initial"
    edge_counts = ["$project_fixed_edges"]
    for i, (query, params, alias, edge_batch) in enumerate(steps):
        name, result = f"step{i}", f"stage{i}"
        parameters[name] = params
        body = _bind_project_parameters(query, params, name)
        if alias == "project_rows":
            body += " RETURN count(*) AS project_rows"
        parts.append(f"""
CALL {{ WITH {previous}
    CALL {{ WITH {previous} WITH {previous} WHERE {previous}>=0 {body} }}
    RETURN sum({alias}) AS {result}
}}
""")
        if edge_batch:
            edge_counts.append(result)
        previous = result
    parts.append(
        # A zero range step is a native execution error: failed postconditions roll back
        # all preceding writes. The step depends on the actual completion count.
        f"RETURN size(range(0,0,CASE WHEN {previous}=1 THEN 1 ELSE 0 END)) AS committed, "
        f"$project_entities AS entities, toInteger({'+'.join(edge_counts)}) AS edges"
    )
    return "\n".join(parts), parameters
