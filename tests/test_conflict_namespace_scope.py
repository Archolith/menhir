"""CF-33 step 3: the conflict tools were tenant-scoped but took no namespace.

Found by the ToolScope census (CF-33 steps 1-2), which reduced "41 tools that are global by
accident" to a reviewable list and made this row inspectable. All four conflict tools sat in
the OBJECT bucket while addressing no object at all -- and their queries carried no tenancy
predicate of any kind, so they were live cross-silo paths independent of any pin:

  * `list_conflicts`   -- readonly tier, returned member `content` from every silo (CF-216's shape)
  * `requeue_conflicts_for_llm_review` -- operator tier, mutated `conflict_status` everywhere (CF-217's shape)
  * `run_llm_conflict_review`          -- operator tier, promoted/cleared groups in every silo
  * `scan_for_conflicts`               -- operator tier, wrote `conflict_group_id` across every silo

The load-bearing fact that makes namespace filtering SOUND rather than merely narrower:
conflict groups are namespace-homogeneous by construction. `set_conflict` is the only writer
of `conflict_group_id`, and its only caller searches for pair candidates with
`group_ids=namespace_to_group_ids(node.namespace)`, so a pair can only form inside one silo.
The group-merge branch preserves that inductively. `test_pairing_is_namespace_scoped_at_the_only_writer`
pins that premise, because every filtering decision here rests on it.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid as uuidlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.mcp.contracts import ToolScope

CONFLICT_TOOLS = (
    "list_conflicts",
    "requeue_conflicts_for_llm_review",
    "run_llm_conflict_review",
    "scan_for_conflicts",
)


def _tool(name: str):
    from menhir.mcp.tools import ALL_TOOLS

    for cls in ALL_TOOLS:
        if cls.name == name:
            return cls
    raise AssertionError(f"tool {name!r} not registered")


# ---------------------------------------------------------------------------
# Declaration and reach
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("name", CONFLICT_TOOLS)
def test_conflict_tools_are_declared_namespaced(name: str) -> None:
    """OBJECT was the wrong declaration and said so out loud: each of these declared itself
    addressed by an object while accepting no object identifier. That mismatch is exactly what
    the ToolScope census exists to surface."""
    cls = _tool(name)
    assert cls.scope == ToolScope.NAMESPACED


@pytest.mark.unit
@pytest.mark.parametrize("name", CONFLICT_TOOLS)
def test_the_pin_can_now_reach_them(name: str) -> None:
    """`_apply_pinned_namespace` is pure signature introspection -- it injects only into
    endpoints that literally declare `namespace`. Declaring the scope does not by itself put a
    tool inside the pin; the parameter does. This asserts the property the pin actually keys on
    rather than the declaration that describes it."""
    cls = _tool(name)
    assert "namespace" in inspect.signature(cls.endpoint).parameters
    assert cls().  _accepts_namespace() is True  # noqa: E211  (the predicate the pin calls)


# ---------------------------------------------------------------------------
# Unpinned behavior is unchanged -- at the CALL, not merely in the result
# ---------------------------------------------------------------------------

def _stub_tool(cls, backend):
    tool = cls()
    tool.get_backend = lambda: backend  # type: ignore[method-assign]
    return tool


@pytest.mark.unit
def test_unpinned_list_conflicts_makes_the_pre_change_backend_call() -> None:
    """Isolation is opt-in (see domain/namespace.py): an absent namespace MUST NOT filter.
    Asserting the exact kwargs, not just the rows, is deliberate -- it proves the parameter is
    absent from the call rather than present-and-null, which is what keeps every pre-existing
    backend stub and protocol implementation valid."""
    from menhir.mcp.tools.conflict.list_conflicts import ListConflictsTool

    backend = MagicMock()
    backend.list_conflict_groups = AsyncMock(return_value=[])
    tool = _stub_tool(ListConflictsTool, backend)

    asyncio.run(tool.endpoint(status="unresolved", limit=25))

    backend.list_conflict_groups.assert_awaited_once_with(
        status="unresolved", limit=25
    )


@pytest.mark.unit
def test_a_namespace_is_forwarded_when_supplied() -> None:
    from menhir.mcp.tools.conflict.list_conflicts import ListConflictsTool

    backend = MagicMock()
    backend.list_conflict_groups = AsyncMock(return_value=[])
    tool = _stub_tool(ListConflictsTool, backend)

    asyncio.run(tool.endpoint(status="unresolved", limit=25, namespace="gamebot"))

    backend.list_conflict_groups.assert_awaited_once_with(
        status="unresolved", limit=25, namespace="gamebot"
    )


@pytest.mark.unit
def test_requeue_forwards_the_namespace_to_the_mutating_call() -> None:
    from menhir.mcp.tools.conflict.requeue_for_review import RequeueForReviewTool

    backend = MagicMock()
    backend.requeue_conflicts_for_llm_review = AsyncMock(return_value=3)
    tool = _stub_tool(RequeueForReviewTool, backend)

    raw = asyncio.run(tool.endpoint(from_status="unresolved", limit=10, namespace="gamebot"))

    assert json.loads(raw)["requeued"] == 3
    backend.requeue_conflicts_for_llm_review.assert_awaited_once_with(
        from_status="unresolved", limit=10, namespace="gamebot"
    )


# ---------------------------------------------------------------------------
# The premise every filtering decision rests on
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_pairing_is_namespace_scoped_at_the_only_writer() -> None:
    """If this ever stops holding, filtering by namespace silently changes from "show the
    caller their groups" to "show the caller PART of a group", and `resolve_conflict` would
    then act on members the caller cannot see. The whole design here depends on it, so it is
    pinned at the source rather than assumed."""
    from menhir.services.lifecycle_consolidation import LifecycleConsolidationMixin

    source = inspect.getsource(LifecycleConsolidationMixin._check_contradictions_batch)
    assert 'namespace_to_group_ids(str(node.get("namespace") or "default"))' in source
    assert "group_ids=group_ids" in source

    # And the group-merge branch that could otherwise join two silos' groups is reached only
    # from that same call site.
    from menhir.infrastructure.consolidation_queries import ConsolidationRepository

    def _body(obj) -> str:
        try:
            return inspect.getsource(obj)
        except (OSError, TypeError):  # slot wrappers, descriptors, C-level attrs
            return ""

    writers = [
        name
        for name, obj in vars(ConsolidationRepository).items()
        if inspect.isfunction(obj) and "conflict_group_id  =" in _body(obj)
    ]
    assert writers == ["set_conflict"], f"a second writer of conflict_group_id appeared: {writers}"


# ---------------------------------------------------------------------------
# Live: the Cypher actually filters, against a real Neo4j
# ---------------------------------------------------------------------------

def _seed_group(repo, *, namespace: str, status: str = "unresolved") -> str:
    group_id = f"grp-{namespace}-{uuidlib.uuid4().hex[:8]}"
    repo.execute(
        """
        CREATE (a:Entity {uuid: $ua, name: 'a', summary: $secret, namespace: $ns,
                          conflict_group_id: $gid, conflict_status: $status,
                          conflict_created_at: datetime()})
        CREATE (b:Entity {uuid: $ub, name: 'b', summary: $secret, namespace: $ns,
                          conflict_group_id: $gid, conflict_status: $status,
                          conflict_created_at: datetime()})
        """,
        params={
            "ua": f"{group_id}-a",
            "ub": f"{group_id}-b",
            "gid": group_id,
            "ns": namespace,
            "status": status,
            "secret": f"{namespace} private content",
        },
    )
    return group_id


@pytest.mark.online
def test_live_list_conflict_groups_does_not_leak_another_silo(test_neo4j_repo) -> None:
    """The finding in its original form: readonly tier, member `content` from every silo."""
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    _seed_group(test_neo4j_repo, namespace="tenant_a")
    _seed_group(test_neo4j_repo, namespace="tenant_b")
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)

    rows = adapter.list_conflict_groups(status="unresolved", namespace="tenant_a")

    assert len(rows) == 1
    blob = json.dumps(rows, default=str)
    assert "tenant_a private content" in blob
    assert "tenant_b private content" not in blob


@pytest.mark.online
def test_live_no_namespace_still_returns_every_silo(test_neo4j_repo) -> None:
    """Isolation is opt-in. A deployment that never passes a namespace must see exactly what it
    saw before this change -- a regression here breaks every existing single-tenant install."""
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    _seed_group(test_neo4j_repo, namespace="tenant_a")
    _seed_group(test_neo4j_repo, namespace="tenant_b")
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)

    assert len(adapter.list_conflict_groups(status="unresolved")) == 2
    assert len(adapter.list_conflict_groups(status="unresolved", namespace=None)) == 2
    assert len(adapter.list_conflict_groups(status="unresolved", namespace="")) == 2


@pytest.mark.online
def test_live_legacy_nodes_without_the_property_read_as_default(test_neo4j_repo) -> None:
    """Nodes predating namespace stamping have no `namespace` property at all. `coalesce(...,
    'default')` is what puts them in the default silo instead of making them invisible to every
    filtered read -- the same convention the rest of the query layer uses."""
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    test_neo4j_repo.execute(
        """
        CREATE (a:Entity {uuid: 'legacy-a', name: 'a', summary: 'legacy',
                          conflict_group_id: 'grp-legacy', conflict_status: 'unresolved',
                          conflict_created_at: datetime()})
        """
    )
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)

    assert len(adapter.list_conflict_groups(status="unresolved", namespace="default")) == 1
    assert adapter.list_conflict_groups(status="unresolved", namespace="tenant_a") == []


@pytest.mark.online
def test_live_requeue_writes_only_inside_the_callers_silo(test_neo4j_repo) -> None:
    """The counterexample that matters for the mutating half. Filtering only the SELECTING match
    would pick groups by the caller's silo and then mutate every member of them; a legacy mixed
    group would turn a same-tenant read into a cross-tenant write. Both halves carry the
    predicate, so the write set is a subset of what the caller may see -- and this seeds exactly
    such a mixed group to prove it."""
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    # One group deliberately straddling two silos -- not producible by `set_conflict` today,
    # but the shape any pre-scoping data could already have on disk.
    test_neo4j_repo.execute(
        """
        CREATE (a:Entity {uuid: 'mixed-a', name: 'a', namespace: 'tenant_a',
                          conflict_group_id: 'grp-mixed', conflict_status: 'unresolved',
                          conflict_created_at: datetime()})
        CREATE (b:Entity {uuid: 'mixed-b', name: 'b', namespace: 'tenant_b',
                          conflict_group_id: 'grp-mixed', conflict_status: 'unresolved',
                          conflict_created_at: datetime()})
        """
    )
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)

    adapter.requeue_conflicts_for_llm_review(from_status="unresolved", namespace="tenant_a")

    rows = test_neo4j_repo.execute(
        "MATCH (n:Entity) WHERE n.conflict_group_id = 'grp-mixed' "
        "RETURN n.uuid AS uuid, n.conflict_status AS status ORDER BY n.uuid"
    )
    by_uuid = {r["uuid"]: r["status"] for r in rows}
    assert by_uuid["mixed-a"] == "pending_llm_review"
    assert by_uuid["mixed-b"] == "unresolved", "a foreign silo's node was mutated"


@pytest.mark.online
def test_live_scan_for_conflicts_only_considers_the_callers_silo(test_neo4j_repo) -> None:
    """The scan WRITES `conflict_group_id` onto whatever it selects, so the candidate query is
    the boundary. Asserted through the query rather than a full scan run, which would need an
    embedding provider."""
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    for ns in ("tenant_a", "tenant_b"):
        test_neo4j_repo.execute(
            """
            CREATE (n:Entity {uuid: $uuid, name: $ns, summary: 'x', namespace: $ns,
                              scope: 'PERSISTENT', freshness: 'ACTIVE'})
            """,
            params={"uuid": f"scan-{ns}", "ns": ns},
        )
    MemoryGraphAdapter(neo4j=test_neo4j_repo)  # schema/ctor parity with the other live tests

    rows = test_neo4j_repo.execute(
        """
        MATCH (n:Entity)
        WHERE n.scope = 'PERSISTENT' AND n.freshness <> 'GONE'
          AND n.conflict_group_id IS NULL
          AND coalesce(n.namespace, 'default') = $namespace
        RETURN n.uuid AS uuid
        """,
        params={"namespace": "tenant_a"},
    )
    assert [r["uuid"] for r in rows] == ["scan-tenant_a"]


# ---------------------------------------------------------------------------
# The bypass found while checking this fix, and closed with it
# ---------------------------------------------------------------------------

@pytest.fixture
def pinned_to_tenant_a(monkeypatch):
    """`get_pinned_namespace` is imported INTO routes_handlers, so the module-local name is what
    the dispatch actually calls -- patching it at its definition site would leave the bound
    reference intact and the test would pass while proving nothing."""
    from menhir.api import routes_handlers

    monkeypatch.setattr(routes_handlers, "get_pinned_namespace", lambda: "tenant_a")
    return "tenant_a"


@pytest.mark.unit
def test_internal_backend_dispatch_applies_the_pin(pinned_to_tenant_a) -> None:
    """`/api/internal/backend/{operation}` re-exposes the same backend operations the named REST
    routes wrap, but it passed the request body through verbatim -- so a pinned client reached
    every silo by naming one in the body, or by naming none.

    Without this, threading `namespace` into the conflict tools would have been decorative: the
    same `list_conflict_groups` is one undocumented POST away with the pin skipped.
    """
    import logging

    from menhir.api import routes_handlers

    async def list_conflict_groups(*, status=None, limit=25, namespace=None):
        return []

    forced = routes_handlers._pin_backend_invoke_namespace(
        "list_conflict_groups",
        list_conflict_groups,
        {"status": "unresolved", "limit": 25, "namespace": "tenant_b"},
        logging.getLogger(__name__),
    )
    assert forced["namespace"] == "tenant_a", "the caller's namespace overrode the pin"


@pytest.mark.unit
def test_internal_backend_dispatch_pins_an_omitted_namespace_too(pinned_to_tenant_a) -> None:
    """Omission is the cheaper evasion and the one a small model performs by accident: no
    argument at all meant "every silo", which is precisely what the pin exists to prevent."""
    import logging

    from menhir.api import routes_handlers

    async def recall(*, query="", limit=10, namespace=None):
        return []

    forced = routes_handlers._pin_backend_invoke_namespace(
        "recall", recall, {"query": "x"}, logging.getLogger(__name__)
    )
    assert forced["namespace"] == "tenant_a"


@pytest.mark.unit
def test_internal_backend_dispatch_leaves_unpinned_clients_untouched() -> None:
    """No pin configured means no behavior change -- the body reaches the backend exactly as
    before, including the absence of a `namespace` key."""
    import logging

    from menhir.api import routes_handlers

    async def recall(*, query="", limit=10, namespace=None):
        return []

    body = {"query": "x"}
    assert routes_handlers._pin_backend_invoke_namespace(
        "recall", recall, dict(body), logging.getLogger(__name__)
    ) == body


@pytest.mark.unit
def test_internal_backend_dispatch_cannot_inject_where_there_is_no_parameter(pinned_to_tenant_a) -> None:
    """Injecting into an operation that does not declare `namespace` would raise TypeError and
    take the endpoint down for a pinned client. Those operations are object-addressed or global;
    their tenancy is ownership-at-load work, not something this boundary can decide."""
    import logging

    from menhir.api import routes_handlers

    async def scheduler_pause():
        return {"paused": True}

    assert routes_handlers._pin_backend_invoke_namespace(
        "scheduler_pause", scheduler_pause, {}, logging.getLogger(__name__)
    ) == {}


# ---------------------------------------------------------------------------
# The startup check gap that let this row exist at all
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_object_without_an_object_key_is_now_a_startup_failure() -> None:
    """The gap that produced this whole row. `assert_tool_scopes_declared` caught
    NAMESPACED-without-`namespace` and GLOBAL-with-`namespace` but not this -- so the one
    declaration that meant "nobody examined this tool" was the one that stayed silent, and nine
    tools sat in the OBJECT bucket addressing nothing. CF-216, CF-217 and the four conflict
    tools all came out of that row.
    """
    from menhir.mcp.contracts import BaseTextTool, assert_tool_scopes_declared

    class _ObjectInNameOnly(BaseTextTool):
        name = "object_in_name_only"
        scope = ToolScope.OBJECT
        description = "declares OBJECT, addresses nothing"

        async def endpoint(self, limit: int = 25) -> str:
            return ""

    with pytest.raises(RuntimeError, match="no object identifier"):
        assert_tool_scopes_declared([_ObjectInNameOnly])


@pytest.mark.unit
def test_a_genuine_object_tool_still_passes() -> None:
    """The check must not become a blanket refusal of the OBJECT declaration -- fourteen tools
    legitimately hold it, and breaking them would push someone toward declaring GLOBAL to
    silence the build, which is the one way this mechanism fails."""
    from menhir.mcp.contracts import BaseTextTool, assert_tool_scopes_declared

    class _RealObjectTool(BaseTextTool):
        name = "real_object_tool"
        scope = ToolScope.OBJECT
        description = "addressed by uuid"

        async def endpoint(self, node_uuid: str) -> str:
            return ""

    assert_tool_scopes_declared([_RealObjectTool])


@pytest.mark.unit
def test_the_object_bucket_is_now_entirely_object_addressed() -> None:
    """The census in prose form. Every remaining OBJECT tool names something; none is a
    tenant-scoped tool hiding behind the declaration. This is what makes CF-33 step 4
    (ownership-at-load) a bounded, enumerable job rather than an open question."""
    from menhir.mcp.tools import ALL_TOOLS
    from menhir.mcp.contracts import _declares_object_key

    keyless = sorted(
        t.name
        for t in ALL_TOOLS
        if getattr(t, "scope", None) == ToolScope.OBJECT
        and not _declares_object_key(inspect.signature(t.endpoint).parameters)
    )
    assert keyless == []


# ---------------------------------------------------------------------------
# Live: the rest of step 3's queries, against a real Neo4j
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_live_episode_processing_rows_are_scoped(test_neo4j_repo) -> None:
    """`list_enrichment_queue` at readonly tier. The rows carry session_id, source and the
    enrichment error text -- tenant-identifying operational metadata, which is why this is filed
    below `list_conflicts` rather than beside it, but it was reaching every silo all the same."""
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    for ns in ("tenant_a", "tenant_b"):
        test_neo4j_repo.execute(
            """
            CREATE (n:Episodic {uuid: $uuid, namespace: $ns, processing_state: 'PENDING',
                                session_id: $sid, source: 'test', created_at: datetime()})
            """,
            params={"uuid": f"ep-{ns}", "ns": ns, "sid": f"session-of-{ns}"},
        )
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)

    scoped = adapter.list_episode_processing(processing_states=["PENDING"], namespace="tenant_a")
    assert [r["uuid"] for r in scoped] == ["ep-tenant_a"]

    # Opt-in: no namespace still sees both.
    unscoped = adapter.list_episode_processing(processing_states=["PENDING"])
    assert len(unscoped) == 2


@pytest.mark.online
def test_live_artifact_snapshots_are_scoped(test_neo4j_repo) -> None:
    """A repository is a locator identity, not a tenancy boundary. Two silos holding artifacts
    for the same repository saw each other's titles, statuses and paths -- and each other's
    artifacts reported as conflicts in their own corpus audit."""
    from menhir.infrastructure.work_artifact_repository import WorkArtifactRepository

    for ns in ("tenant_a", "tenant_b"):
        test_neo4j_repo.execute(
            """
            CREATE (a:WorkArtifact {artifact_uuid: $uuid, namespace: $ns,
                                    artifact_type: 'plan', title: $title, status: 'ACTIVE'})
            CREATE (s:ArtifactSource {source_uuid: $suuid, medium: 'markdown',
                                      locator_repository: 'shared-repo',
                                      locator_path: $path})
            CREATE (a)-[:EMBODIED_IN]->(s)
            """,
            params={
                "uuid": f"art-{ns}", "suuid": f"src-{ns}", "ns": ns,
                "title": f"{ns} private plan", "path": f".agent/plans/{ns}.md",
            },
        )
    repo = WorkArtifactRepository(test_neo4j_repo)

    scoped = repo.list_artifact_source_snapshots(repository="shared-repo", namespace="tenant_a")
    assert [s.artifact_uuid for s in scoped] == ["art-tenant_a"]

    assert len(repo.list_artifact_source_snapshots(repository="shared-repo")) == 2


@pytest.mark.online
def test_live_a_declared_uuid_is_not_proof_of_ownership(test_neo4j_repo) -> None:
    """`list_work_artifact_identities` takes UUIDs read out of files in the caller's own
    worktree. Nothing stops a file from declaring another silo's UUID, and without the filter
    that returned the foreign artifact's title and status."""
    from menhir.infrastructure.work_artifact_repository import WorkArtifactRepository

    test_neo4j_repo.execute(
        """
        CREATE (a:WorkArtifact {artifact_uuid: 'art-foreign', namespace: 'tenant_b',
                                artifact_type: 'plan', title: 'tenant_b secret title',
                                status: 'ACTIVE'})
        """
    )
    repo = WorkArtifactRepository(test_neo4j_repo)

    assert repo.list_work_artifact_identities(
        artifact_uuids=["art-foreign"], namespace="tenant_a"
    ) == []
    assert len(repo.list_work_artifact_identities(artifact_uuids=["art-foreign"])) == 1
