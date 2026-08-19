"""CF-33 step 4: ownership-at-load for the object-addressed tools.

A NAMESPACED tool is bounded by its argument. An OBJECT-addressed tool has no argument to
inject -- the caller names a uuid -- so the boundary has to be checked where the object is
loaded. CF-64 established the shape on `delete_memory` and `flag_memory`; this is that shape
applied to the remaining fourteen and factored into `menhir.mcp.ownership`.

**The category turned out to be empty.** `ToolScope.OBJECT` was defined as "the pin cannot be
injected as an argument, so tenancy must be checked at load". In practice every one of the
fourteen could take the argument -- what they lacked was the parameter, not the ability to have
one. Two were not object-addressed at all (`add_candidate`'s `cluster_id` is a grouping label on
a new write; `rate_recall`'s `recall_id` is a telemetry receipt token), and the other twelve
now declare `namespace` and check it. `test_no_tool_is_object_addressed_any_more` records that,
because it is a claim about the design that a future reader should be able to re-check rather
than take on trust.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from menhir.mcp.contracts import ToolScope
from menhir.mcp.ownership import foreign_object_refusal

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------

def _lookup_from(table: dict[str, str]):
    """A scoped lookup over {uuid: owning_namespace}."""

    async def lookup(uuid: str, *, namespace: str | None = None):
        owner = table.get(uuid)
        if owner is None:
            return None
        if namespace is not None and owner != namespace:
            return None
        return {"uuid": uuid, "namespace": owner}

    return lookup


def test_an_object_in_another_silo_is_refused() -> None:
    refusal = asyncio.run(
        foreign_object_refusal(
            uuid="obj-1",
            namespace="tenant_a",
            lookup=_lookup_from({"obj-1": "tenant_b"}),
            label="episode",
        )
    )
    assert refusal is not None
    assert "outside namespace tenant_a" in refusal


def test_the_callers_own_object_proceeds() -> None:
    assert asyncio.run(
        foreign_object_refusal(
            uuid="obj-1",
            namespace="tenant_a",
            lookup=_lookup_from({"obj-1": "tenant_a"}),
            label="episode",
        )
    ) is None


def test_an_absent_object_proceeds_rather_than_being_refused() -> None:
    """The second lookup is the whole point of the two-lookup shape, and this is why it exists.
    Refusing whenever the object is not found IN the caller's namespace would also refuse when
    it is not in the graph at all -- and absent is not an ownership violation. `delete_memory`
    is the case that proves it: `graph_already_absent` is how a merge leaves the node it
    absorbed, whose stored content must still be erasable. A tool must keep reporting
    "not found" for a uuid nobody owns, not "refused"."""
    assert asyncio.run(
        foreign_object_refusal(
            uuid="never-existed",
            namespace="tenant_a",
            lookup=_lookup_from({}),
            label="episode",
        )
    ) is None


def test_an_unscoped_caller_never_reaches_the_lookup() -> None:
    """Isolation is opt-in, and this asserts the stronger form of that: an unpinned caller does
    not merely get the same answer, it does not perform the lookup at all. The tools rely on
    this -- each passes `lambda uuid, **kw: backend.<method>(uuid, **kw)` precisely so an
    unpinned call does not even resolve the backend attribute."""
    calls: list[str] = []

    async def lookup(uuid: str, *, namespace: str | None = None):
        calls.append(uuid)
        return None

    assert asyncio.run(
        foreign_object_refusal(uuid="obj-1", namespace="", lookup=lookup, label="x")
    ) is None
    assert asyncio.run(
        foreign_object_refusal(uuid="obj-1", namespace="   ", lookup=lookup, label="x")
    ) is None
    assert calls == [], "an unscoped call performed a lookup it never used to perform"


# ---------------------------------------------------------------------------
# Every tool that names an object now declares the parameter the pin keys on
# ---------------------------------------------------------------------------

GUARDED_TOOLS = (
    "get_enrichment_status",
    "get_episode_trace",
    "watch_enrichment",
    "force_reenrich",
    "force_release_enrichment_lease",
    "get_artifact_relationships",
    "link_artifacts",
    "supersede_artifact",
    "relocate_artifact_source",
    "get_provenance",
    "close_todo",
    "resolve_conflict",
)


@pytest.mark.parametrize("name", GUARDED_TOOLS)
def test_guarded_tools_declare_namespace_and_are_reachable_by_the_pin(name: str) -> None:
    """`_apply_pinned_namespace` is pure signature introspection: it injects only into endpoints
    that literally name `namespace`. Declaring the scope documents the intent; the parameter is
    what actually puts the tool inside the pin, so that is what this asserts."""
    from menhir.mcp.tools import ALL_TOOLS

    tool = next(t for t in ALL_TOOLS if t.name == name)
    assert tool.scope == ToolScope.NAMESPACED
    assert "namespace" in inspect.signature(tool.endpoint).parameters


def test_no_tool_is_object_addressed_any_more() -> None:
    """Recorded rather than assumed. If a future tool declares OBJECT this fails, which is the
    prompt to decide whether it genuinely cannot take a `namespace` argument -- the question
    fourteen tools were never asked."""
    from menhir.mcp.tools import ALL_TOOLS

    object_tools = sorted(
        t.name for t in ALL_TOOLS if getattr(t, "scope", None) == ToolScope.OBJECT
    )
    assert object_tools == []


def test_the_guard_runs_before_the_work_in_every_tool_that_has_one() -> None:
    """Ordering is the property, not presence. A guard that runs after the mutation refuses
    nothing. This checks the refusal is reached before the backend call in each source body,
    which is cheap and catches the one mistake that would make all of the above vacuous."""
    from menhir.mcp.tools import ALL_TOOLS

    checked = 0
    for tool in ALL_TOOLS:
        source = inspect.getsource(tool.endpoint)
        if "foreign_object_refusal" not in source:
            continue
        checked += 1
        guard_at = source.index("foreign_object_refusal")
        # The first backend call that is not the lazily-bound lookup inside the guard itself.
        work = [
            source.index(marker)
            for marker in ("result = await backend.", "await backend.close_todo(",
                           "await backend.fetch_node_receipts(",
                           "released = await backend.force_release_episode_lease(",
                           "row, history, timed_out = await ",
                           "row = await backend.fetch_episode_processing(",
                           "data = await backend.get_artifact_relationships(")
            if marker in source
        ]
        assert work, f"{tool.name}: no recognised work call found to order against"
        assert guard_at < min(work), f"{tool.name}: the guard runs after the work"
    assert checked >= 10, f"only {checked} guards found; the sweep is not covering the tools"


# ---------------------------------------------------------------------------
# End to end through a tool, not just the helper
# ---------------------------------------------------------------------------

def _stub(tool_cls, backend):
    tool = tool_cls()
    tool.get_backend = lambda: backend  # type: ignore[method-assign]
    return tool


def test_close_todo_refuses_a_todo_in_another_silo() -> None:
    """`close_stale_todos` was namespace-scoped by CF-217; closing ONE todo by uuid was not, so
    the bulk path was bounded and the single path was not. That asymmetry is the tell this
    cluster keeps producing."""
    from unittest.mock import AsyncMock, MagicMock

    from menhir.mcp.tools.ops.close_todo import CloseTodoTool

    backend = MagicMock()
    backend.get_todo = AsyncMock(side_effect=lambda uuid, **kw: None if kw.get("namespace") else {"uuid": uuid})
    backend.close_todo = AsyncMock(return_value=True)

    result = asyncio.run(_stub(CloseTodoTool, backend).endpoint(uuid="t-1", namespace="tenant_a"))

    assert "outside namespace tenant_a" in result
    backend.close_todo.assert_not_awaited()


def test_close_todo_still_closes_the_callers_own_todo() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from menhir.mcp.tools.ops.close_todo import CloseTodoTool

    backend = MagicMock()
    backend.get_todo = AsyncMock(return_value={"uuid": "t-1"})
    backend.close_todo = AsyncMock(return_value=True)

    result = asyncio.run(_stub(CloseTodoTool, backend).endpoint(uuid="t-1", namespace="tenant_a"))

    assert "Closed TODO t-1" in result
    backend.close_todo.assert_awaited_once()


def test_close_todo_unpinned_never_looks_the_todo_up() -> None:
    """The byte-identical property, at the tool rather than the helper: an unpinned caller makes
    exactly the backend call it made before the guard existed."""
    from unittest.mock import AsyncMock, MagicMock

    from menhir.mcp.tools.ops.close_todo import CloseTodoTool

    backend = MagicMock()
    backend.get_todo = AsyncMock(return_value=None)
    backend.close_todo = AsyncMock(return_value=True)

    asyncio.run(_stub(CloseTodoTool, backend).endpoint(uuid="t-1"))

    backend.get_todo.assert_not_awaited()
    backend.close_todo.assert_awaited_once_with("t-1")


def test_link_artifacts_checks_both_uuids_not_just_the_source() -> None:
    """The existing "must share a namespace" rule is RELATIVE -- it stops a cross-silo link but
    is equally satisfied by two artifacts that both live in someone else's silo. Checking only
    the source would leave that intact, so the target is checked too."""
    from unittest.mock import AsyncMock, MagicMock

    from menhir.mcp.tools.ops.link_artifacts import LinkArtifactsTool

    owners = {"a-mine": "tenant_a", "a-theirs": "tenant_b"}

    async def get_artifact(uuid, **kw):
        owner = owners.get(uuid)
        if owner is None:
            return None
        if kw.get("namespace") and kw["namespace"] != owner:
            return None
        return {"artifact_uuid": uuid}

    backend = MagicMock()
    backend.get_artifact = AsyncMock(side_effect=get_artifact)
    backend.link_artifacts = AsyncMock(return_value={"linked": True})

    result = asyncio.run(
        _stub(LinkArtifactsTool, backend).endpoint(
            source_uuid="a-mine", target_uuid="a-theirs", relation="informs",
            namespace="tenant_a",
        )
    )

    assert "a-theirs exists but is outside namespace tenant_a" in result
    backend.link_artifacts.assert_not_awaited()


# ---------------------------------------------------------------------------
# Live: the candidate MERGE key, which is a data-model change and the riskiest
# thing in this batch
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_live_two_silos_no_longer_collide_on_one_candidate(test_neo4j_repo) -> None:
    """`add_candidate` MERGEd on (source, cluster_id) alone. Two silos emitting the same cluster
    id from the same source therefore landed on ONE node, and `ON MATCH SET` let each rewrite
    the other's `candidate_evidence_strength` and `candidate_distinct_sessions` -- the fields a
    promotion decision reads. One silo's activity could push another silo's candidate up the
    evidence ladder toward approval. That is the consequence worth testing, not the untidiness.
    """
    from menhir.infrastructure.candidate_repository import CandidateRepository

    repo = CandidateRepository(test_neo4j_repo)
    a = repo.create_candidate(
        content="tenant A content", source="painscan", cluster_id="c-1", label="A",
        evidence_strength="ANECDOTAL", distinct_sessions=1, namespace="tenant_a",
    )
    b = repo.create_candidate(
        content="tenant B content", source="painscan", cluster_id="c-1", label="B",
        evidence_strength="COMMON", distinct_sessions=99, namespace="tenant_b",
    )

    assert a["uuid"] != b["uuid"], "two silos collided on one candidate node"

    rows = test_neo4j_repo.execute(
        "MATCH (n:Entity) WHERE n.candidate_cluster_id = 'c-1' "
        "RETURN n.namespace AS ns, n.candidate_evidence_strength AS strength, "
        "       n.candidate_distinct_sessions AS sessions, n.group_id AS group_id "
        "ORDER BY n.namespace"
    )
    by_ns = {r["ns"]: r for r in rows}
    assert by_ns["tenant_a"]["strength"] == "ANECDOTAL", "tenant B inflated tenant A's evidence"
    assert by_ns["tenant_a"]["sessions"] == 1
    assert by_ns["tenant_a"]["group_id"] == "tenant_a"
    assert by_ns["tenant_b"]["group_id"] == "tenant_b"


@pytest.mark.online
def test_live_an_unscoped_candidate_keeps_its_old_key_and_group(test_neo4j_repo) -> None:
    """No migration is included, so the unscoped write must still MERGE onto its historical key
    and land in graphiti's default partition -- otherwise every existing candidate silently
    duplicates on the next emit."""
    from menhir.infrastructure.candidate_repository import CandidateRepository

    repo = CandidateRepository(test_neo4j_repo)
    first = repo.create_candidate(
        content="x", source="painscan", cluster_id="c-2", label="L",
        evidence_strength="ANECDOTAL", distinct_sessions=1,
    )
    second = repo.create_candidate(
        content="x", source="painscan", cluster_id="c-2", label="L",
        evidence_strength="COMMON", distinct_sessions=5,
    )

    assert first["uuid"] == second["uuid"], "an unscoped re-emit created a duplicate node"
    rows = test_neo4j_repo.execute(
        "MATCH (n:Entity) WHERE n.candidate_cluster_id = 'c-2' RETURN n.group_id AS g"
    )
    assert len(rows) == 1
    assert rows[0]["g"] == "", "the unscoped write left graphiti's default partition"
