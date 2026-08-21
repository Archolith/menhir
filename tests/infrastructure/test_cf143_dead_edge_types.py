"""CF-143: two resolved-file edge types were written on every create and read by nothing.

Census (2026-08-16) classified every hit of `REFERENCES_FILE` and `RESOLVES_TO` across
`src`/`tests`/`scripts`:

- `REFERENCES_FILE` — one dead write: `MERGE (l)-[:REFERENCES_FILE]->(f)` from `:TodoLocation`
  in `_link_location_files` (todo_repository.py). The only READ of this edge anchors on
  `(n:Todo)` (get_todo), not `(l:TodoLocation)`, so the location-level edge is never traversed.
  The `:Todo`-level `CREATE (todo)-[:REFERENCES_FILE]->(f)` write in `create_todo` IS read and
  is left alone. The `_link_location_files` helper existed only to create the dead edge, so it
  was removed.
- `RESOLVES_TO` — the only non-comment hit was the write `MERGE (l)-[:RESOLVES_TO]->(f)` in
  `_link_location_files` (work_artifact_repository.py); no reader anywhere. Removed with its
  helper.

No graph migration was added: leaving already-written orphan edges in place is safe, and a
destructive cleanup is a separate decision (noted as follow-up).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from menhir.infrastructure.todo_repository import TodoRepository
from menhir.infrastructure.work_artifact_repository import WorkArtifactRepository
from menhir.domain.work_artifact import ArtifactType

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "menhir"


@dataclass
class _StubNeo4j:
    responses: list[list[dict]] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def execute(self, query: str, params: dict | None = None) -> list[dict]:
        self.calls.append({"query": query, "params": params or {}})
        if self.responses:
            return self.responses.pop(0)
        return []


# 1. THE CENSUS, as a structural drift guard: the dead location-anchored writes are gone.
@pytest.mark.unit
def test_dead_location_edge_writes_are_absent_from_src() -> None:
    dead_patterns = ("(l)-[:REFERENCES_FILE]", "(l)-[:RESOLVES_TO]")
    offenders = []
    for py in SRC_ROOT.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for pattern in dead_patterns:
            if pattern in text:
                offenders.append(f"{py.relative_to(SRC_ROOT)}: {pattern!r}")
    assert not offenders, (
        "a dead location-anchored write for REFERENCES_FILE / RESOLVES_TO is back in src/menhir:\n"
        + "\n".join(offenders)
    )


# 2a. POSITIVE CONTROL: creating a todo still writes its :TodoLocation with path/project.
@pytest.mark.unit
def test_create_todo_still_writes_todo_location_with_path_and_project() -> None:
    neo4j = _StubNeo4j()
    repo = TodoRepository(neo4j)

    repo.create_todo(content="Fix handler", code_ref="src/api/routes.py:42", structure_project="menhir")

    location_writes = [
        c for c in neo4j.calls if ":TodoLocation" in c["query"] and "HAS_LOCATION" in c["query"]
    ]
    assert location_writes, "create_todo no longer writes :TodoLocation via :HAS_LOCATION"
    rows = location_writes[0]["params"]["rows"]
    assert rows, ":TodoLocation write carried no location rows"
    assert rows[0]["path"] == "src/api/routes.py"
    assert "project" in rows[0]


# 2b. POSITIVE CONTROL: creating a work artifact still succeeds and writes :ArtifactLocation.
@pytest.mark.unit
def test_create_work_artifact_still_writes_artifact_location_with_path_and_project() -> None:
    neo4j = _StubNeo4j()
    repo = WorkArtifactRepository(neo4j)

    result = repo.create_artifact(
        artifact_type=ArtifactType.PLAN,
        title="A plan",
        code_refs="src/a.py:12, src/b.py",
        structure_project="menhir",
    )

    location_writes = [
        c for c in neo4j.calls if ":ArtifactLocation" in c["query"] and "HAS_LOCATION" in c["query"]
    ]
    assert location_writes, "create_artifact no longer writes :ArtifactLocation via :HAS_LOCATION"
    rows = location_writes[0]["params"]["rows"]
    assert rows, ":ArtifactLocation write carried no location rows"
    assert "path" in rows[0]
    assert "project" in rows[0]
    assert [(loc["path"], loc["line_start"]) for loc in result["locations"]] == [
        ("src/a.py", 12),
        ("src/b.py", None),
    ]


# 3. POSITIVE CONTROL: the live resolver still reaches locations through :HAS_LOCATION and does
#    NOT reference the deleted edge types.
@pytest.mark.unit
def test_structure_resolver_still_reaches_locations_through_has_location() -> None:
    src = (SRC_ROOT / "infrastructure" / "structure_queries.py").read_text(encoding="utf-8")

    assert "-[:HAS_LOCATION]->(l:TodoLocation)" in src
    assert ":HAS_LOCATION" in src
    # The resolver's Cypher must not traverse the deleted edge types. The bare token
    # `REFERENCES_FILE` may still appear in a comment explaining why the old edge was
    # abandoned (see structure_queries.py); only a traversal reference is the bug.
    assert "-[:REFERENCES_FILE]" not in src
    assert "-[:RESOLVES_TO]" not in src


# 4. :HAS_LOCATION is still written where it was (from the owning node to the location node).
@pytest.mark.unit
def test_has_location_still_written_for_todo_and_artifact() -> None:
    todo_neo4j = _StubNeo4j()
    TodoRepository(todo_neo4j).create_todo(content="t", code_ref="src/x.py:1")
    todo_has_location = any(
        "HAS_LOCATION" in c["query"] and ":TodoLocation" in c["query"] for c in todo_neo4j.calls
    )

    art_neo4j = _StubNeo4j()
    WorkArtifactRepository(art_neo4j).create_artifact(
        artifact_type=ArtifactType.PLAN, title="p", code_refs="src/x.py:1"
    )
    art_has_location = any(
        "HAS_LOCATION" in c["query"] and ":ArtifactLocation" in c["query"]
        for c in art_neo4j.calls
    )

    assert todo_has_location
    assert art_has_location
