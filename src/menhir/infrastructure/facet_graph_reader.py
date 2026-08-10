"""Neo4jFacetGraphReader — the bounded bulk prefetch supplying FacetInputs (R2, Phase 2b-real).

One Cypher over the candidate pool maps each memory to:
- its ANCHORED_TO **file** anchors (``structure_path``) + those files' ``structure_project``,
- the symbols those files ``DEFINES`` (``symbol_kind``),
- node scope/belief/time metadata (namespace, conflict_status, created_at, content).

Implements the domain ``FacetGraphReader`` protocol, so ``FacetCandidateSource`` stays
graph-agnostic. One round-trip for the whole pool (bounded), no per-candidate query.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from menhir.domain.facet_candidate_source import FacetInputs

# conflict_status values that mean the memory is no longer current -> a stale belief bucket.
_STALE_CONFLICT = frozenset({"superseded", "historical", "outdated"})

_FACET_INPUTS_CYPHER = """
MATCH (n:Entity) WHERE n.uuid IN $uuids
OPTIONAL MATCH (n)-[:ANCHORED_TO]->(f) WHERE f.structure_path IS NOT NULL
OPTIONAL MATCH (f)-[:DEFINES]->(s) WHERE s.symbol_kind IS NOT NULL
RETURN n.uuid AS uuid,
       coalesce(n.content, n.summary, n.name, '') AS content,
       coalesce(n.namespace, 'default') AS namespace,
       n.conflict_status AS conflict_status,
       toString(n.created_at) AS created_at,
       collect(DISTINCT f.structure_path) AS files,
       collect(DISTINCT f.structure_project) AS projects,
       collect(DISTINCT s.name) AS symbols
"""


class _Executable(Protocol):
    def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        ...


class Neo4jFacetGraphReader:
    """Bounded bulk facet prefetch over a candidate pool (implements FacetGraphReader)."""

    def __init__(self, neo4j: _Executable) -> None:
        self._neo4j = neo4j

    def fetch_facet_inputs(self, uuids: list[str]) -> dict[str, FacetInputs]:
        pool = [u for u in uuids if u]
        if not pool:
            return {}
        rows = self._neo4j.execute(_FACET_INPUTS_CYPHER, {"uuids": pool})
        return {str(row["uuid"]): _row_to_inputs(row) for row in rows if row.get("uuid")}


def _row_to_inputs(row: dict[str, Any]) -> FacetInputs:
    files = _clean(row.get("files"))
    symbols = _clean(row.get("symbols"))
    projects = list(dict.fromkeys(_clean(row.get("projects"))))
    # Scope 'project' is a scalar: use it only when all anchors agree (unambiguous scope).
    project = projects[0] if len(projects) == 1 else None
    conflict = str(row.get("conflict_status") or "").lower()
    belief_bucket = "historical" if conflict in _STALE_CONFLICT else None
    return FacetInputs(
        content=str(row.get("content") or ""),
        namespace=row.get("namespace") or None,
        project=project,
        belief_bucket=belief_bucket,
        valid_time=row.get("created_at") or None,
        anchored_files=files,
        anchored_symbols=symbols,
    )


def _clean(values: Iterable[Any] | None) -> tuple[str, ...]:
    return tuple(str(v) for v in (values or []) if v)
