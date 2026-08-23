"""CF-106 remainder -- `stale_anchored_memories` returned one tenant's memory names to another.

CF-106's first half (`list_in_window`) closed on 2026-08-20. Its entry then recorded the rest as
*"`tool_event_repository` has no namespace column or property anywhere - that is a schema change
plus a backfill decision, not a predicate"*. **Re-read at source, that is half right and the half
it gets wrong is the reachable one.**

`ToolEventRepository` has six queries. Four match only `(:Entity {structure_role: 'file'})` --
**structure** entities, which per the owner ruling of 2026-08-21 are deliberately a single shared
silo keyed on `(structure_project, structure_path)`, written with `group_id = ''` and no namespace
at all. Those correctly have no tenancy, and adding one would be the defect.

The exception is `stale_anchored_memories`, which joins a structure file to a **memory**:

    MATCH (sem:Entity)-[a:ANCHORED_TO]->(f:Entity {structure_role: 'file'})
    RETURN sem.uuid AS memory_uuid, sem.name AS name, ...

`sem` is tenant-scoped and had no predicate, so the query returned every tenant's memory names.
That is a predicate fix, not a schema change -- no new column, no backfill.

**THE PREDICATE GOES ON `sem`, NOT ON `f`.** Scoping the file side would match nothing (structure
nodes carry no namespace) while looking exactly like a working filter -- which is why
`tenant_scope_cypher` refuses to emit a predicate for the structure scheme rather than returning
one that silently matches zero rows.

**Reachability, traced rather than assumed:** `GET /tool-events/dirty` and `GET /tool-events/stale`
(both `readonly`) return the rows directly, and `recall_pipeline` uses them to label results.

**STILL OPEN, and this is the genuine schema half:** `StaleAnchorVerification` receipt nodes carry
`memory_uuid`, `project`, `path` and operator-written `notes`, and **no namespace**. Scoping
`list_stale_anchor_verifications` needs a property plus a backfill decision for existing receipts,
which is the work CF-106's entry actually describes. Not attempted here.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark_unit = pytest.mark.unit


# ---------------------------------------------------------------------------
# Offline: the predicate is built, and built on the right side of the join
# ---------------------------------------------------------------------------


class _RecordingNeo4j:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def execute(self, query: str, params: dict | None = None) -> list[dict]:
        self.calls.append((query, params or {}))
        return []


def _repo() -> tuple[Any, _RecordingNeo4j]:
    from menhir.infrastructure.tool_event_repository import ToolEventRepository

    neo4j = _RecordingNeo4j()
    return ToolEventRepository(neo4j), neo4j


@pytest.mark.unit
def test_a_namespace_scopes_the_memory_side_of_the_join() -> None:
    """THE FINDING. `sem.name` is a memory's own name; unscoped, it crossed tenants."""
    repo, neo4j = _repo()

    repo.stale_anchored_memories(namespace="tenant-a")

    query, params = neo4j.calls[0]
    assert "coalesce(sem.namespace, sem.group_id, '') IN $tenant_namespaces" in query
    assert params["tenant_namespaces"] == ["tenant-a"]


@pytest.mark.unit
def test_the_structure_side_is_never_scoped() -> None:
    """THE SUBTLETY, asserted so a later edit cannot 'improve' it. `f` is a structure entity --
    single shared silo, no namespace property. A predicate on `f.namespace` would match nothing
    while looking like a working filter, which is the failure `tenant_scope_cypher` refuses to
    emit for exactly this scheme."""
    repo, neo4j = _repo()

    repo.stale_anchored_memories(namespace="tenant-a")

    query = neo4j.calls[0][0]
    assert "f.namespace" not in query
    assert "coalesce(f." not in query


@pytest.mark.unit
def test_the_predicate_comes_from_the_shared_builder_not_by_hand() -> None:
    """WHY, and it is not style. The hand-written form used first here was
    `coalesce(sem.namespace, 'default') = $namespace`, which matches an ABSENT property but not one
    stored as `''` -- and those are the same legacy silo by owner ruling, so a memory stored with an
    empty namespace would have been invisible to the tenant that owns it. That is CF-127's measured
    33,442-row blind spot, reproduced. CF-127's ratchet test caught it on the first suite run."""
    from menhir.domain.namespace import tenant_scope_cypher

    repo, neo4j = _repo()
    repo.stale_anchored_memories(namespace="tenant-a")

    assert tenant_scope_cypher("sem") in neo4j.calls[0][0]


@pytest.mark.unit
def test_omitting_the_namespace_keeps_the_previous_global_read() -> None:
    """Opt-in, matching `list_in_window` and `list_todos` rather than inventing a second
    convention. The repository default staying global is CF-85's policy question, not this one."""
    repo, neo4j = _repo()

    repo.stale_anchored_memories()

    _query, params = neo4j.calls[0]
    # The fragment is always emitted; a NULL parameter is what makes it a no-op. That is the
    # builder's contract, and it is why omitting the namespace cannot accidentally scope.
    assert params["tenant_namespaces"] is None


@pytest.mark.unit
def test_the_project_filter_still_works_alongside_the_namespace() -> None:
    """POSITIVE CONTROL. Two independent filters on two different nodes; a rewrite that dropped
    the project predicate would still pass every tenancy assertion above."""
    repo, neo4j = _repo()

    repo.stale_anchored_memories(project="menhir", namespace="tenant-a")

    query, params = neo4j.calls[0]
    assert "f.structure_project = $project" in query
    assert params["project"] == "menhir"
    assert params["tenant_namespaces"] == ["tenant-a"]


@pytest.mark.unit
def test_both_readonly_routes_and_recall_pass_a_namespace() -> None:
    """TRAP T17. The repository accepting a namespace proves nothing about the three callers
    passing one, and not passing it is indistinguishable from the defect."""
    import inspect

    from menhir.api import routes
    from menhir.services import recall_pipeline

    route_src = inspect.getsource(routes)
    assert route_src.count(
        "adapter.stale_anchored_memories, project=project"
    ) == 2, "both /tool-events routes must still call it"
    assert route_src.count(
        "namespace=_resolve_namespace(request, None),"
    ) >= 2, "both routes must resolve and pass a namespace"

    recall_src = inspect.getsource(recall_pipeline)
    assert "service.graph_adapter.stale_anchored_memories," in recall_src
    idx = recall_src.index("service.graph_adapter.stale_anchored_memories,")
    assert "namespace=namespace," in recall_src[idx : idx + 260], (
        "recall's stale-anchor labelling must scope to the recall namespace"
    )


# ---------------------------------------------------------------------------
# Online: the predicate actually partitions, against a real graph
# ---------------------------------------------------------------------------


def _seed(repo: Any) -> None:
    """One dirty file shared by two tenants' memories, plus a legacy unstamped memory."""
    repo.execute("MATCH (n) DETACH DELETE n")
    repo.execute(
        """
        CREATE (f:Entity {uuid:'f1', structure_role:'file', structure_project:'menhir',
                          structure_path:'src/x.py', structure_dirty:true,
                          dirty_at: datetime('2026-08-20T12:00:00Z'),
                          last_event_op:'write'})
        CREATE (a:Entity {uuid:'mem-a', name:'TENANT A SECRET', namespace:'tenant-a'})
        CREATE (b:Entity {uuid:'mem-b', name:'TENANT B SECRET', namespace:'tenant-b'})
        CREATE (l:Entity {uuid:'mem-legacy', name:'LEGACY UNSTAMPED'})
        CREATE (e:Entity {uuid:'mem-empty', name:'EMPTY NAMESPACE', namespace:''})
        CREATE (a)-[:ANCHORED_TO {created_at: datetime('2026-08-19T12:00:00Z')}]->(f)
        CREATE (b)-[:ANCHORED_TO {created_at: datetime('2026-08-19T12:00:00Z')}]->(f)
        CREATE (l)-[:ANCHORED_TO {created_at: datetime('2026-08-19T12:00:00Z')}]->(f)
        CREATE (e)-[:ANCHORED_TO {created_at: datetime('2026-08-19T12:00:00Z')}]->(f)
        """
    )


def _names(rows: list[dict]) -> set[str]:
    return {str(r["name"]) for r in rows}


@pytest.mark.online
def test_one_tenant_does_not_see_anothers_memory_names(test_neo4j_repo) -> None:
    """THE DEFECT, executed. Both memories are anchored to the SAME dirty file, so the join
    genuinely returns both rows before the predicate."""
    from menhir.infrastructure.tool_event_repository import ToolEventRepository

    _seed(test_neo4j_repo)
    repo = ToolEventRepository(test_neo4j_repo)

    assert _names(repo.stale_anchored_memories(namespace="tenant-a")) == {"TENANT A SECRET"}
    assert _names(repo.stale_anchored_memories(namespace="tenant-b")) == {"TENANT B SECRET"}


@pytest.mark.online
def test_the_unscoped_read_still_returns_everything(test_neo4j_repo) -> None:
    """POSITIVE CONTROL, and the proof the fixture is capable of showing the leak: without a
    namespace the join returns all three, so the assertions above are partitioning real rows
    rather than passing on an empty result."""
    from menhir.infrastructure.tool_event_repository import ToolEventRepository

    _seed(test_neo4j_repo)
    repo = ToolEventRepository(test_neo4j_repo)

    assert _names(repo.stale_anchored_memories()) == {
        "TENANT A SECRET", "TENANT B SECRET", "LEGACY UNSTAMPED", "EMPTY NAMESPACE",
    }


@pytest.mark.online
def test_a_legacy_unstamped_memory_reads_as_default(test_neo4j_repo) -> None:
    """The `coalesce` is the documented legacy-read behaviour, not a shortcut: a node written
    before namespace stamping belongs to the default silo. Without it those memories would become
    invisible to every tenant, which is a silent data-loss-shaped bug rather than a leak."""
    from menhir.infrastructure.tool_event_repository import ToolEventRepository

    _seed(test_neo4j_repo)
    repo = ToolEventRepository(test_neo4j_repo)

    assert _names(repo.stale_anchored_memories(namespace="default")) == {
        "LEGACY UNSTAMPED", "EMPTY NAMESPACE",
    }
    assert "LEGACY UNSTAMPED" not in _names(repo.stale_anchored_memories(namespace="tenant-a"))


@pytest.mark.online
def test_a_memory_stored_with_an_empty_namespace_is_visible_to_default(test_neo4j_repo) -> None:
    """THE BUG THE RATCHET CAUGHT, executed. `''` and `'default'` are one silo by owner ruling, and
    a node can carry either. `coalesce(sem.namespace, 'default')` handles only the ABSENT case: a
    stored `''` coalesces to `''`, which never equals `'default'`, so the row disappears for its own
    tenant. The builder expands both spellings, which is why it exists."""
    from menhir.infrastructure.tool_event_repository import ToolEventRepository

    _seed(test_neo4j_repo)
    repo = ToolEventRepository(test_neo4j_repo)

    assert "EMPTY NAMESPACE" in _names(repo.stale_anchored_memories(namespace="default"))
    assert "EMPTY NAMESPACE" not in _names(repo.stale_anchored_memories(namespace="tenant-a"))


@pytest.mark.online
def test_scoping_the_file_side_would_have_matched_nothing(test_neo4j_repo) -> None:
    """Why the predicate is on `sem`. Structure entities carry no namespace, so the plausible-
    looking alternative returns an empty set for every tenant -- a filter that looks like it works
    and silently disables the whole feature."""
    _seed(test_neo4j_repo)

    rows = test_neo4j_repo.execute(
        """
        MATCH (sem:Entity)-[a:ANCHORED_TO]->(f:Entity {structure_role: 'file'})
        WHERE f.structure_dirty = true
          AND coalesce(f.namespace, 'default') = $namespace
        RETURN sem.name AS name
        """,
        params={"namespace": "tenant-a"},
    )
    assert rows == [], "structure nodes carry no namespace; this is the trap being avoided"
