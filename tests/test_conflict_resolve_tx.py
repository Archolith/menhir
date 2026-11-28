"""#890: a conflict resolve commits all-or-nothing under a lock on the group's members.

Offline: a small in-memory graph answers the statements by shape. ``execute_write`` snapshots the
graph and restores it when the body raises, so a rolled-back transaction is observable; plain
``execute`` applies each statement at once, which is how the pre-transaction code ran.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from menhir.infrastructure.consolidation_queries import summary_sha256
from menhir.infrastructure.graph_text_inventory import ABSORBED_BLOCK_SEPARATOR
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

pytestmark = pytest.mark.unit

GROUP = "g1"


def _node(uuid: str, content: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "uuid": uuid, "name": uuid, "scope": "PERSISTENT", "freshness": "ACTIVE",
        "content": content, "conflict_group_id": GROUP, "conflict_status": "unresolved",
        **extra,
    }


class _Graph:
    """Just enough of the resolve statements to observe their effect."""

    def __init__(self, nodes: list[dict[str, Any]]) -> None:
        self.nodes = {n["uuid"]: n for n in nodes}
        self.epoch = 0
        self.statements: list[str] = []
        self.hooks: list[tuple[Callable[[str], bool], Callable[[_Graph], None]]] = []
        self.fail_when: Callable[[str], bool] | None = None

    def after(self, match: str, fn: Callable[[_Graph], None]) -> None:
        """Run ``fn`` once, right after the first statement containing ``match`` is answered."""
        self.hooks.append((lambda q: match in q, fn))

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self.nodes)

    def _group(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [n for n in self.nodes.values() if n.get("conflict_group_id") == params.get("group_id")]

    def run(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        params = params or {}
        self.statements.append(query)
        if self.fail_when is not None and self.fail_when(query):
            raise RuntimeError("injected failure")
        rows = self._answer(query, params)
        for hook in list(self.hooks):
            if hook[0](query):
                self.hooks.remove(hook)
                hook[1](self)
        return rows

    def _answer(self, q: str, p: dict[str, Any]) -> list[dict[str, Any]]:  # noqa: C901
        if "AS epoch" in q and "ErasureMarker" in q:
            return [{"epoch": self.epoch, "erasing_ops": 0, "marked": []}]
        if "fence_epoch" in q:
            return [
                {"uuid": n["uuid"], "scope": n["scope"], "name": n["name"],
                 "fence_key": "default", "fence_epoch": 0}
                for n in sorted(self._group(p), key=lambda n: n["uuid"])
            ]
        if "EvidenceNamespaceFence" in q:
            return []
        if "AS marker" in q:
            return [
                {"uuid": n["uuid"], "scope": n["scope"], "name": n["name"], "fence_key": "default",
                 "freshness": n["freshness"], "content": n.get("content"),
                 "original_content": n.get("original_content"),
                 "marker": n.get("compressed_summary_sha256"),
                 "absorbed_from": n.get("absorbed_from")}
                for n in sorted(self._group(p), key=lambda n: n["uuid"])
            ]
        if "_conflict_resolve_lock" in q:
            return [{"locked": len(self._group(p))}]
        if "n.content AS content, n.original_content AS original_content" in q:  # old keep read
            n = self.nodes[p["uuid"]]
            return [{"content": n.get("content"), "original_content": n.get("original_content")}]
        if q.strip().endswith("n.content AS content"):  # old target read
            return [{"content": self.nodes[p["uuid"]].get("content")}]
        if q.strip().endswith("RETURN n.uuid AS uuid"):  # old prefetch
            return [{"uuid": n["uuid"]} for n in self._group(p)]
        if "total_edges_bridged" in q:
            return [{"total_edges_bridged": 0, "skipped_hubs": []}]
        if "n.content = $content" in q:
            return self._absorb(q, p)
        if "n.freshness = 'GONE'" in q:
            hit = [
                n for n in self._group(p)
                if n["uuid"] in p["remove_uuids"]
                and ("$allow_promoted" not in q or p["allow_promoted"] or n["scope"] != "PROMOTED")
            ]
            for n in hit:
                n.update(freshness="GONE", conflict_status=p["resolution_status"], conflict_group_id=None)
            return [{"removed_uuids": [n["uuid"] for n in hit]}]
        if "collect(n.uuid) AS member_uuids" in q or "count(n) AS resolved" in q:
            hit = self._group(p)
            for n in hit:
                n.update(conflict_status=p["resolution_status"], conflict_group_id=None)
            row: dict[str, Any] = {"resolved": len(hit)}
            if "member_uuids" in q:
                row["member_uuids"] = [n["uuid"] for n in hit]
            return [row]
        if "n.pending_review = true" in q:
            return []
        raise AssertionError(f"unexpected statement: {q[:120]}")

    def _absorb(self, q: str, p: dict[str, Any]) -> list[dict[str, Any]]:
        keep = self.nodes[p["uuid"]]
        done = p["target"] in (keep.get("absorbed_from") or [])
        if "AS absorbed" in q and done:  # the transaction code guards in the WHERE
            return [{"absorbed": 0}]
        if "n.original_content = $original" in q:  # transaction code: explicit values
            keep["original_content"] = p["original"]
        elif "ELSE coalesce(n.original_content, $original) END" in q:  # old CASE rule
            compressed, orig = keep["freshness"] == "COMPRESSED", keep.get("original_content")
            if compressed and orig is not None:
                trusted = keep.get("compressed_summary_sha256") == p["keep_sha"] and not done
                keep["original_content"] = orig + p["block"] if trusted else orig
            else:
                keep["original_content"] = orig if orig is not None else p["original"]
        if "n.compressed_summary_sha256 = $marker" in q:
            keep["compressed_summary_sha256"] = p["marker"]
        elif "$content_sha" in q and keep["freshness"] == "COMPRESSED":
            trusted = (
                keep.get("original_content") is not None
                and keep.get("compressed_summary_sha256") == p["keep_sha"]
            )
            keep["compressed_summary_sha256"] = p["content_sha"] if trusted else None
        keep["content"] = p["content"]
        keep["absorbed_from"] = list(keep.get("absorbed_from") or []) + (
            [] if done else [p["target"]]
        )
        return [{"absorbed": 1}]


class _Neo4j:
    def __init__(self, graph: _Graph) -> None:
        self.graph = graph

    def execute(self, query: str, params: dict[str, Any] | None = None, timeout_s: Any = None) -> Any:
        return self.graph.run(query, params)

    def execute_write(self, work: Callable[[Any], Any], **_kw: Any) -> Any:
        graph, before = self.graph, self.graph.snapshot()

        class _Tx:
            def execute(self, query: str, params: dict[str, Any] | None = None) -> Any:
                return graph.run(query, params)

        try:
            return work(_Tx())
        except BaseException:
            graph.nodes = before
            raise


def _adapter(graph: _Graph) -> MemoryGraphAdapter:
    return MemoryGraphAdapter(neo4j=_Neo4j(graph))


def _resolve(graph: _Graph, action: str = "replace", **kw: Any) -> dict[str, Any]:
    kw.setdefault("keep_uuid", "keep")
    return _adapter(graph).resolve_conflict_group(GROUP, action, **kw)


def _blocks(content: str, target: str) -> int:
    return content.count(f"{ABSORBED_BLOCK_SEPARATOR}{target})")


def test_gone_failure_leaves_nothing_changed() -> None:
    """The GONE step fails: no absorbed text, no GONE, the group still open. Fails on main, where
    the absorb is already committed when the GONE statement raises."""
    graph = _Graph([_node("keep", "keep text"), _node("t1", "target one")])
    before = graph.snapshot()
    graph.fail_when = lambda q: "n.freshness = 'GONE'" in q

    with pytest.raises(RuntimeError, match="injected"):
        _resolve(graph)

    assert graph.nodes == before


def test_retry_after_a_failed_resolve_absorbs_once() -> None:
    """A failed attempt, then a retry: one block. Fails on main, where the first attempt's block is
    committed and the retry appends a second."""
    graph = _Graph([_node("keep", "keep text"), _node("t1", "target one")])
    graph.fail_when = lambda q: "n.freshness = 'GONE'" in q
    with pytest.raises(RuntimeError):
        _resolve(graph)
    graph.fail_when = None

    result = _resolve(graph)

    assert result["removed_uuids"] == ["t1"]
    assert _blocks(graph.nodes["keep"]["content"], "t1") == 1
    assert graph.nodes["t1"]["freshness"] == "GONE"


def test_retry_of_a_half_done_resolve_does_not_duplicate_the_block() -> None:
    """State left by a resolve that absorbed and then failed before GONE: the retry adds no second
    block. Fails on main, which rebuilds the text from the content that already holds the block."""
    block = f"{ABSORBED_BLOCK_SEPARATOR}t1) on 2026-01-01 ---\ntarget one"
    graph = _Graph([
        _node("keep", f"keep text{block}", absorbed_from=["t1"]),
        _node("t1", "target one"),
    ])

    result = _resolve(graph)

    assert graph.nodes["keep"]["content"] == f"keep text{block}"
    assert graph.nodes["keep"]["absorbed_from"] == ["t1"]
    assert result["removed_uuids"] == ["t1"]
    assert graph.nodes["t1"]["freshness"] == "GONE"


def test_a_fenced_absorb_refuses_the_whole_resolve() -> None:
    """An erase lands after the epoch was read: the resolve is refused with a reason and nothing is
    GONE. Fails on main, which skips the absorb, GONEs the member and reports success."""
    from menhir.infrastructure.consolidation_queries import ConflictResolveRefused

    graph = _Graph([_node("keep", "keep text"), _node("t1", "target one")])
    before = graph.snapshot()
    graph.after("fence_epoch", lambda g: setattr(g, "epoch", g.epoch + 1))

    with pytest.raises(ConflictResolveRefused) as refused:
        _resolve(graph)

    assert refused.value.reason == "erasure_fence"
    assert "epoch_moved" in str(refused.value)
    assert graph.nodes == before


def test_a_member_promoted_after_the_read_is_not_gone() -> None:
    """Promoted between the early read and the lock: the guard re-run under the lock refuses it.
    Fails on main, which checks PROMOTED once, from the early read."""
    graph = _Graph([_node("keep", "keep text"), _node("t1", "target one")])
    graph.after("fence_epoch", lambda g: g.nodes["t1"].update(scope="PROMOTED"))

    with pytest.raises(ValueError, match="Refusing to GONE PROMOTED"):
        _resolve(graph)

    assert graph.nodes["t1"]["freshness"] == "ACTIVE"
    assert graph.nodes["t1"]["conflict_group_id"] == GROUP
    assert graph.nodes["keep"]["content"] == "keep text"


def test_a_change_seen_only_under_the_lock_is_a_refusal_not_a_bad_request() -> None:
    """Promoted after the early read: the in-lock guard raises ConflictResolveRefused, which the
    tool maps to "refused", not a plain ValueError ("invalid_argument")."""
    from menhir.infrastructure.consolidation_queries import ConflictResolveRefused

    graph = _Graph([_node("keep", "keep text"), _node("t1", "target one")])
    graph.after("fence_epoch", lambda g: g.nodes["t1"].update(scope="PROMOTED"))

    with pytest.raises(ConflictResolveRefused) as refused:
        _resolve(graph)

    assert refused.value.reason == "member_changed"


def test_a_member_promoted_between_the_locked_read_and_gone_rolls_back() -> None:
    """The GONE write re-checks PROMOTED itself. Fails on main, whose GONE write has no check."""
    graph = _Graph([_node("keep", "keep text"), _node("t1", "target one")])
    before = graph.snapshot()
    graph.after("AS marker", lambda g: g.nodes["t1"].update(scope="PROMOTED"))

    with pytest.raises(ValueError, match="member_changed"):
        _resolve(graph)

    assert graph.nodes == before


def test_a_member_that_joined_after_the_read_refuses_the_resolve() -> None:
    """A member joins between the early read and the lock: its text was never marker-checked, so
    the resolve is refused with member_changed and nothing is retired. Fails on main, which
    resolves the group without looking at the late member."""
    from menhir.infrastructure.consolidation_queries import ConflictResolveRefused

    graph = _Graph([_node("keep", "keep text"), _node("t1", "target one")])
    late = _node("t2", "late text", conflict_group_id=None)
    graph.nodes["t2"] = late
    graph.after("fence_epoch", lambda g: late.update(conflict_group_id=GROUP))

    with pytest.raises(ConflictResolveRefused) as refused:
        _resolve(graph)

    assert refused.value.reason == "member_changed" and "t2" in str(refused.value)
    assert graph.nodes["keep"]["content"] == "keep text"
    assert graph.nodes["t1"]["freshness"] == "ACTIVE" and graph.nodes["t1"]["conflict_group_id"] == GROUP
    assert graph.nodes["t2"]["freshness"] == "ACTIVE" and graph.nodes["t2"]["conflict_group_id"] == GROUP


def test_a_member_that_left_after_the_read_refuses_the_resolve() -> None:
    from menhir.infrastructure.consolidation_queries import ConflictResolveRefused

    graph = _Graph([_node("keep", "keep text"), _node("t1", "one"), _node("t2", "two")])
    graph.after("fence_epoch", lambda g: g.nodes["t2"].update(conflict_group_id=None))

    with pytest.raises(ConflictResolveRefused) as refused:
        _resolve(graph)

    assert refused.value.reason == "member_changed"
    assert graph.nodes["t1"]["freshness"] == "ACTIVE"
    assert graph.nodes["keep"]["content"] == "keep text"


def test_a_resolve_with_two_targets_keeps_both_blocks() -> None:
    graph = _Graph([_node("keep", "keep text"), _node("t1", "one"), _node("t2", "two")])

    result = _resolve(graph)

    content = graph.nodes["keep"]["content"]
    assert content.startswith("keep text")
    assert _blocks(content, "t1") == 1 and _blocks(content, "t2") == 1
    assert content.index("one") < content.index("two")
    assert graph.nodes["keep"]["absorbed_from"] == ["t1", "t2"]
    assert result["removed_uuids"] == ["t1", "t2"]


def _compressed(marker: str | None) -> dict[str, Any]:
    return _node(
        "keep", "the summary", freshness="COMPRESSED", original_content="the full body",
        compressed_summary_sha256=marker,
    )


def test_compressed_keep_with_a_trusted_marker_gets_the_blocks_in_original_content() -> None:
    graph = _Graph([_compressed(summary_sha256("the summary")), _node("t1", "one"), _node("t2", "two")])

    _resolve(graph)

    keep = graph.nodes["keep"]
    assert keep["original_content"].startswith("the full body")
    assert _blocks(keep["original_content"], "t1") == 1 and _blocks(keep["original_content"], "t2") == 1
    assert keep["compressed_summary_sha256"] == summary_sha256(keep["content"])
    assert keep["original_content"].endswith(keep["content"][len("the summary"):])


def test_compressed_keep_with_an_untrusted_marker_leaves_original_content_and_drops_the_marker() -> None:
    graph = _Graph([_compressed(summary_sha256("something else")), _node("t1", "one"), _node("t2", "two")])

    _resolve(graph)

    keep = graph.nodes["keep"]
    assert keep["original_content"] == "the full body"
    assert keep["compressed_summary_sha256"] is None
    assert _blocks(keep["content"], "t1") == 1 and _blocks(keep["content"], "t2") == 1


def test_a_keep_without_content_never_gets_an_empty_original_content() -> None:
    """Fails on main, which stores original_content = ''."""
    graph = _Graph([_node("keep", None), _node("t1", "one")])

    _resolve(graph)

    assert graph.nodes["keep"].get("original_content") is None
    assert "one" in graph.nodes["keep"]["content"]


def test_keep_both_reports_the_members_the_write_changed() -> None:
    """A member joining between the early read and the write is resolved and reported. Fails on
    main, whose uuids come from an earlier prefetch."""
    graph = _Graph([_node("a", "x"), _node("b", "y")])
    graph.after("", lambda g: g.nodes.update(c=_node("c", "z")))

    result = _adapter(graph).resolve_conflict_group(GROUP, "keep_both")

    assert result["resolved"] == 3
    assert result["member_uuids"] == ["a", "b", "c"]


# --- set_conflict -----------------------------------------------------------------------------
class _RecordingNeo4j:
    def __init__(self, decide_rows: list[dict[str, Any]]) -> None:
        self.statements: list[str] = []
        self.in_write = False
        self.decide_rows = decide_rows
        self.decided_in_write = False

    def execute(self, query: str, params: Any = None, timeout_s: Any = None) -> Any:
        self.statements.append(query)
        return self.decide_rows

    def execute_write(self, work: Callable[[Any], Any], **_kw: Any) -> Any:
        outer = self

        class _Tx:
            def execute(self, query: str, params: Any = None) -> Any:
                outer.statements.append(query)
                if "retired_group_id" in query:
                    outer.decided_in_write = True
                    return outer.decide_rows
                return []

        return work(_Tx())


def test_set_conflict_locks_both_nodes_and_both_groups_before_it_decides() -> None:
    """Fails on main, which reads the groups and writes them in one unlocked statement."""
    neo4j = _RecordingNeo4j([{"canonical_group_id": "g-1", "updated": 3}])

    result = MemoryGraphAdapter(neo4j=neo4j).set_conflict("a", "b", "new-group")

    assert result == ("g-1", 3)
    assert neo4j.decided_in_write
    assert len(neo4j.statements) == 3
    assert neo4j.statements[0].count("_conflict_resolve_lock") == 2
    assert "x.uuid IN [$uuid_a, $uuid_b]" in neo4j.statements[0]
    assert "m.conflict_group_id IN group_ids" in neo4j.statements[1]
    assert "retired_group_id" in neo4j.statements[2]


# --- remove_conflict_member --------------------------------------------------------------------
class _Sequenced:
    def __init__(self, *responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.queries: list[str] = []

    def execute(self, query: str, params: Any = None, timeout_s: Any = None) -> Any:
        self.queries.append(query)
        return self.responses.pop(0) if self.responses else []


def test_remove_conflict_member_names_the_unreviewed_members_it_refused_on() -> None:
    """Fails on main, which returns no reason."""
    neo4j = _Sequenced([], [{"uuid": "c", "visible": True}])

    out = MemoryGraphAdapter(neo4j=neo4j).remove_conflict_member(
        GROUP, "a", from_status="pending_llm_review", reviewed_uuids=["b"]
    )

    assert out["removed"] is False
    assert out["reason"] == "unreviewed_members"
    assert out["unreviewed_uuids"] == ["c"]


def test_remove_conflict_member_refuses_on_an_unreviewed_foreign_member_without_naming_it() -> None:
    neo4j = _Sequenced([], [{"uuid": "foreign", "visible": False}])

    out = MemoryGraphAdapter(neo4j=neo4j).remove_conflict_member(
        GROUP, "a", from_status="pending_llm_review", reviewed_uuids=["b"], namespace="ns1"
    )

    assert out["removed"] is False
    assert out["reason"] == "unreviewed_members"
    assert out["unreviewed_uuids"] == []


def test_remove_conflict_member_reports_a_changed_member_when_nobody_is_unreviewed() -> None:
    neo4j = _Sequenced([], [])

    out = MemoryGraphAdapter(neo4j=neo4j).remove_conflict_member(
        GROUP, "a", from_status="pending_llm_review", reviewed_uuids=["b"]
    )

    assert out["removed"] is False and out["reason"] == "member_changed"


@pytest.mark.asyncio
async def test_the_review_loop_logs_why_a_removal_was_refused(
    stub_memory_graph_adapter, caplog
) -> None:
    from menhir.config import MemorySettings
    from menhir.services.lifecycle_service import LifecycleService

    adapter = stub_memory_graph_adapter
    adapter.conflict_groups["g3"] = {
        "group_id": "g3", "created_at": "2026-11-01T00:00:00+00:00", "status": "mixed",
        "members": [
            {"uuid": u, "name": u, "content": f"content {u}", "status": "pending_llm_review",
             "scope": "PERSISTENT"}
            for u in ("a", "b", "c")
        ],
    }
    adapter.remove_conflict_member = lambda *a, **k: {
        "removed": False, "dissolved": False, "remaining_uuids": [],
        "reason": "unreviewed_members", "unreviewed_uuids": ["zzz"],
    }
    llm = MagicMock()
    llm.confirm_contradiction = AsyncMock(
        side_effect=lambda *, name_a, content_a, name_b, content_b: frozenset((name_a, name_b)) == frozenset("ab")
    )
    svc = LifecycleService(
        graph_adapter=adapter, graphiti_client=MagicMock(), llm=llm, settings=MemorySettings(),
    )
    store = MagicMock()
    store.is_pair_resolved.return_value = False

    with patch("menhir.services.lifecycle_conflicts.telemetry_store", store), caplog.at_level(
        logging.INFO, logger="menhir.services.lifecycle_conflicts"
    ):
        await svc.confirm_pending_conflicts(limit=10)

    assert any("unreviewed_members" in r.getMessage() and "zzz" in r.getMessage() for r in caplog.records)


# --- the operator tool -------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_tool_returns_a_refused_resolve_with_its_reason_not_success() -> None:
    from menhir.mcp.telemetry.tracker import McpToolRefusal
    from menhir.mcp.tools.conflict.resolve_conflict import ResolveConflictTool

    backend = MagicMock()
    backend.list_conflict_groups = AsyncMock(return_value=[{
        "group_id": GROUP, "status": "unresolved",
        "members": [{"uuid": "keep", "name": "k", "status": "unresolved"},
                    {"uuid": "t1", "name": "t", "status": "unresolved"}],
    }])
    backend.resolve_conflict_group = AsyncMock(side_effect=ValueError(
        "Conflict resolve refused (erasure_fence): epoch_moved; retry after the erasure finishes. "
        "Nothing was changed."
    ))
    tool = ResolveConflictTool()
    tool.get_backend = MagicMock(return_value=backend)

    with pytest.raises(McpToolRefusal) as refused:
        await tool.endpoint(group_id=GROUP, action="replace", keep_uuid="keep")

    assert "erasure_fence" in str(refused.value) and "epoch_moved" in str(refused.value)
    assert refused.value.code == "refused"


def _dry_run_tool(estimate: Any) -> tuple[Any, Any]:
    from menhir.mcp.tools.conflict.resolve_conflict import ResolveConflictTool

    backend = MagicMock(spec=["list_conflict_groups", "resolve_conflict_group", *(
        ["estimate_bridges_for_nodes"] if estimate is not None else []
    )])
    backend.list_conflict_groups = AsyncMock(return_value=[{
        "group_id": GROUP, "status": "unresolved",
        "members": [{"uuid": u, "name": u, "status": "unresolved"} for u in ("keep", "t1", "t2")],
    }])
    backend.resolve_conflict_group = AsyncMock()
    if estimate is not None:
        backend.estimate_bridges_for_nodes = AsyncMock(return_value=estimate)
    tool = ResolveConflictTool()
    tool.get_backend = MagicMock(return_value=backend)
    return tool, backend


@pytest.mark.asyncio
async def test_the_dry_run_reports_the_real_bridge_estimate_and_over_cap_hubs() -> None:
    """Fails on main, whose estimate is the number of removed nodes."""
    import json

    tool, backend = _dry_run_tool({
        "neighbour_counts": {"t1": 4, "t2": 30}, "estimated_edges": 6, "over_cap": ["t2"],
    })

    out = json.loads(await tool.endpoint(
        group_id=GROUP, action="replace", keep_uuid="keep", dry_run=True, namespace="ns1"
    ))

    backend.estimate_bridges_for_nodes.assert_awaited_once_with(["t1", "t2"], namespace="ns1")
    backend.resolve_conflict_group.assert_not_called()
    assert out["bridge_count_estimate"] == 6
    assert out["bridge_over_cap_hubs"] == ["t2"]
    assert out["bridge_neighbour_counts"] == {"t1": 4, "t2": 30}
    assert out["bridge_estimate_available"] is True
    assert out["would_remove_uuids"] == ["t1", "t2"]


@pytest.mark.asyncio
async def test_the_dry_run_says_unavailable_when_the_backend_cannot_estimate() -> None:
    import json

    tool, _ = _dry_run_tool(None)

    out = json.loads(await tool.endpoint(
        group_id=GROUP, action="replace", keep_uuid="keep", dry_run=True
    ))

    assert out["bridge_count_estimate"] is None
    assert out["bridge_estimate_available"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    AttributeError("'StubGraphAdapter' object has no attribute 'estimate_bridges_for_nodes'"),
    RuntimeError("Unknown backend operation: estimate_bridges_for_nodes"),
])
async def test_the_dry_run_says_unavailable_when_the_estimate_raises(error: Exception) -> None:
    """An older server or an adapter without the op: the method exists on the client, the call fails."""
    import json

    tool, backend = _dry_run_tool({})
    backend.estimate_bridges_for_nodes = AsyncMock(side_effect=error)

    out = json.loads(await tool.endpoint(
        group_id=GROUP, action="replace", keep_uuid="keep", dry_run=True
    ))

    backend.resolve_conflict_group.assert_not_called()
    assert out["bridge_count_estimate"] is None
    assert out["bridge_estimate_available"] is False
    assert out["would_remove_uuids"] == ["t1", "t2"]


@pytest.mark.asyncio
async def test_the_dry_run_passes_a_tier_refusal_through() -> None:
    """A 403 from the estimate is an answer, not a missing estimate."""
    tool, backend = _dry_run_tool({})
    backend.estimate_bridges_for_nodes = AsyncMock(side_effect=PermissionError("operator tier required"))

    with pytest.raises(Exception, match="operator tier required"):
        await tool.endpoint(group_id=GROUP, action="replace", keep_uuid="keep", dry_run=True)


@pytest.mark.asyncio
async def test_the_resolve_reports_the_hubs_it_did_not_bridge() -> None:
    import json

    from menhir.mcp.tools.conflict.resolve_conflict import ResolveConflictTool

    backend = MagicMock()
    groups = [{
        "group_id": GROUP, "status": "unresolved",
        "members": [{"uuid": "keep", "name": "k", "status": "unresolved"},
                    {"uuid": "t1", "name": "t", "status": "unresolved"}],
    }]
    backend.list_conflict_groups = AsyncMock(side_effect=[groups, []])
    backend.resolve_conflict_group = AsyncMock(return_value={
        "resolved": 1, "removed_uuids": ["t1"], "bridged_edges": 0, "bridge_skipped_hubs": ["t1"],
    })
    tool = ResolveConflictTool()
    tool.get_backend = MagicMock(return_value=backend)

    out = json.loads(await tool.endpoint(
        group_id=GROUP, action="replace", keep_uuid="keep", remove_uuid="t1"
    ))

    assert out["bridge_skipped_hubs"] == ["t1"]
