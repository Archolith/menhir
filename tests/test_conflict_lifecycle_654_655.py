"""#654 (group status and per-member review) and #655 (enrichment-time conflicts)."""

from __future__ import annotations

import contextlib
import uuid as uuidlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from menhir.config import MemorySettings
from menhir.domain.models import ProcessingState


def _lifecycle(adapter, *, verdict):
    from menhir.services.lifecycle_service import LifecycleService

    llm = MagicMock()
    llm.confirm_contradiction = AsyncMock(side_effect=verdict)
    return LifecycleService(
        graph_adapter=adapter, graphiti_client=MagicMock(), llm=llm, settings=MemorySettings(),
    )


def _group(adapter, group_id: str, members: dict[str, str]) -> None:
    adapter.conflict_groups[group_id] = {
        "group_id": group_id,
        "created_at": "2026-11-01T00:00:00+00:00",
        "status": "mixed",
        "members": [
            {"uuid": u, "name": u, "content": f"content {u}", "status": s, "scope": "PERSISTENT"}
            for u, s in members.items()
        ],
    }


def _statuses(adapter, group_id: str) -> dict[str, str]:
    return {str(m["uuid"]): str(m["status"]) for m in adapter.conflict_groups[group_id]["members"]}


def _pair_verdicts(table: dict[frozenset[str], bool | None]):
    async def _verdict(*, name_a, content_a, name_b, content_b):
        return table[frozenset((name_a, name_b))]
    return _verdict


# ---------------------------------------------------------------------------
# #654: review every member, not members[0] and [1]
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_group_of_three_with_one_false_positive_member(stub_memory_graph_adapter) -> None:
    adapter = stub_memory_graph_adapter
    _group(adapter, "g3", {"a": "pending_llm_review", "b": "pending_llm_review", "c": "pending_llm_review"})
    svc = _lifecycle(adapter, verdict=_pair_verdicts({
        frozenset("ab"): True, frozenset("ac"): False, frozenset("bc"): False,
    }))
    store = MagicMock()
    store.is_pair_resolved.return_value = False

    with patch("menhir.services.lifecycle_conflicts.telemetry_store", store):
        result = await svc.confirm_pending_conflicts(limit=10)

    assert _statuses(adapter, "g3") == {"a": "unresolved", "b": "unresolved"}
    assert adapter.removed_conflict_members == [("g3", "c", "false_positive")]
    suppressed = {
        frozenset((c.kwargs["uuid_a"], c.kwargs["uuid_b"]))
        for c in store.record_conflict_resolution.call_args_list
    }
    # Only the rejected pairs are suppressed; the confirmed a-b pair is not.
    assert suppressed == {frozenset("ac"), frozenset("bc")}
    assert result["confirmed"] == 1 and result["cleared"] == 1 and result["errors"] == 0
    # Each pair is asked once.
    assert svc.llm.confirm_contradiction.await_count == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_new_member_of_unresolved_group_is_reviewed_alone(stub_memory_graph_adapter) -> None:
    """A pending member that joined a confirmed group is reviewed against it; the confirmed
    members are not re-reviewed and stay unresolved whatever the new member's verdict."""
    adapter = stub_memory_graph_adapter
    _group(adapter, "gu", {"a": "unresolved", "b": "unresolved", "n": "pending_llm_review"})
    svc = _lifecycle(adapter, verdict=_pair_verdicts({frozenset("na"): False, frozenset("nb"): False}))

    with patch("menhir.services.lifecycle_conflicts.telemetry_store", MagicMock()):
        await svc.confirm_pending_conflicts(limit=10)

    assert _statuses(adapter, "gu") == {"a": "unresolved", "b": "unresolved"}
    assert svc.llm.confirm_contradiction.await_count == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_llm_failure_leaves_member_pending(stub_memory_graph_adapter) -> None:
    adapter = stub_memory_graph_adapter
    _group(adapter, "gf", {"a": "pending_llm_review", "b": "pending_llm_review", "c": "pending_llm_review"})
    # c's pairs: one rejected, one unanswered -> c is not removed on partial evidence.
    svc = _lifecycle(adapter, verdict=_pair_verdicts({
        frozenset("ab"): None, frozenset("ac"): None, frozenset("bc"): False,
    }))

    with patch("menhir.services.lifecycle_conflicts.telemetry_store", MagicMock()), \
            patch("menhir.services.lifecycle_conflicts.last_provider_quota_exhaustion", return_value=None):
        result = await svc.confirm_pending_conflicts(limit=10)

    assert _statuses(adapter, "gf") == {
        "a": "pending_llm_review", "b": "pending_llm_review", "c": "pending_llm_review",
    }
    assert adapter.removed_conflict_members == []
    assert result["errors"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_limit_caps_llm_calls_and_leaves_the_rest_untouched(stub_memory_graph_adapter) -> None:
    adapter = stub_memory_graph_adapter
    _group(adapter, "g1", {"a": "pending_llm_review", "b": "pending_llm_review"})
    _group(adapter, "g2", {"c": "pending_llm_review", "d": "pending_llm_review"})
    svc = _lifecycle(adapter, verdict=_pair_verdicts({frozenset("ab"): True, frozenset("cd"): True}))

    with patch("menhir.services.lifecycle_conflicts.telemetry_store", MagicMock()):
        result = await svc.confirm_pending_conflicts(limit=1)

    assert svc.llm.confirm_contradiction.await_count == 1
    assert _statuses(adapter, "g1") == {"a": "unresolved", "b": "unresolved"}
    assert _statuses(adapter, "g2") == {"c": "pending_llm_review", "d": "pending_llm_review"}
    assert result == {"confirmed": 1, "cleared": 0, "skipped_no_llm": 0, "errors": 0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_weekly_unresolved_review_drops_only_the_rejected_member(stub_memory_graph_adapter) -> None:
    adapter = stub_memory_graph_adapter
    _group(adapter, "gw", {"a": "unresolved", "b": "unresolved", "c": "unresolved"})
    svc = _lifecycle(adapter, verdict=_pair_verdicts({
        frozenset("ab"): True, frozenset("ac"): False, frozenset("bc"): False,
    }))

    with patch("menhir.services.lifecycle_conflicts.telemetry_store", MagicMock()):
        await svc.confirm_pending_conflicts(limit=10, status="unresolved")

    assert _statuses(adapter, "gw") == {"a": "unresolved", "b": "unresolved"}
    assert adapter.removed_conflict_members == [("gw", "c", "false_positive")]


# ---------------------------------------------------------------------------
# #655: a conflict found at enrichment is queued without consolidation
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.asyncio
async def test_enrichment_conflict_creates_a_pending_group(
    stub_memory_graph_adapter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from menhir.services import enrichment_steps as es
    from menhir.services.correlation_service import CorrelationService
    from menhir.services.correlation_service_types import CorrelationBatchResult, CorrelationResult
    from menhir.services.ingest_gate import IngestGate
    from menhir.services.lifecycle_service import LifecycleService

    adapter = stub_memory_graph_adapter
    episode_uuid = adapter.create_pending_episode(
        episode_uuid="ep-655", name="ep-655", content="the limit is 500 per minute",
        session_id="s", user_id="u", source="unit-test", source_confidence=0.5,
    )
    adapter.pending_episode_rows[episode_uuid]["processing_state"] = ProcessingState.ENRICHING
    adapter.pending_episode_rows[episode_uuid]["processing_owner"] = "w-1"

    async def _check(self, node_uuid, query, *, namespace=None, **_):
        return CorrelationBatchResult(checked=1, conflicts=1, results=[
            CorrelationResult(action="conflict", source_uuid=node_uuid, target_uuid="old-node", similarity=0.9),
        ])

    monkeypatch.setattr(CorrelationService, "check_correlation", _check)
    monkeypatch.setattr(adapter, "_correlation", MagicMock(), raising=False)
    lifecycle = LifecycleService(
        graph_adapter=adapter, graphiti_client=MagicMock(), llm=None, settings=MemorySettings(),
    )
    store = MagicMock()
    store.is_pair_resolved.return_value = False
    ctx = es.EnrichmentContext(
        episode_uuid=episode_uuid,
        claimed={"content": "the limit is 500 per minute", "namespace": "default"},
        started=0.0, processing_attempts=1, worker_id="w-1",
        graph_adapter=adapter, graphiti_client=MagicMock(), lifecycle_service=lifecycle,
        llm=None, ingest_gate=IngestGate(1), processing_steps_total=5,
        settings_record_revisions=False, ready_warning_ms=5000,
        graphiti_add_episode_timeout_s=5.0, graphiti_episode_max_estimated_tokens=8000,
        get_queue_depth=lambda: 0,
    )
    graphiti_result = SimpleNamespace(
        episode=SimpleNamespace(uuid="episode-655"),
        nodes=[SimpleNamespace(uuid="new-node", name="rate limit", content="500 per minute")],
        edges=[], episodic_edges=[],
    )

    with patch("menhir.services.lifecycle_consolidation.telemetry_store", store):
        with contextlib.suppress(Exception):
            # Later steps on the stub are incidental; the conflict write precedes them.
            await es.stamp_and_finalize(ctx, graphiti_result)

    assert [(c["uuid_a"], c["uuid_b"], c["initial_status"]) for c in adapter.conflict_calls] == [
        ("new-node", "old-node", "pending_llm_review"),
    ]
    pending = adapter.list_conflict_groups(status="pending_llm_review")
    assert len(pending) == 1
    assert {m["uuid"] for m in pending[0]["members"]} == {"new-node", "old-node"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enrichment_skips_suppressed_pair_and_takes_the_next(
    stub_memory_graph_adapter, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from menhir.services.lifecycle_service import LifecycleService

    adapter = stub_memory_graph_adapter
    lifecycle = LifecycleService(
        graph_adapter=adapter, graphiti_client=MagicMock(), llm=None, settings=MemorySettings(),
    )
    store = MagicMock()
    store.is_pair_resolved.side_effect = lambda a, b, **_: b == "reviewed"
    with patch("menhir.services.lifecycle_consolidation.telemetry_store", store):
        assert await lifecycle.record_conflict_pair("n", "reviewed") is None
        assert await lifecycle.record_conflict_pair("n", "fresh") is not None
    assert [c["uuid_b"] for c in adapter.conflict_calls] == ["fresh"]


# ---------------------------------------------------------------------------
# Live: set_conflict status preservation and member removal on real Neo4j
# ---------------------------------------------------------------------------

def _seed(repo, gid: str, members: dict[str, str]) -> None:
    for u, status in members.items():
        repo.execute(
            """
            CREATE (:Entity {uuid: $u, name: $u, summary: $u, namespace: 'default',
                             conflict_group_id: $gid, conflict_status: $status,
                             conflict_created_at: datetime()})
            """,
            params={"u": u, "gid": gid, "status": status},
        )


def _fresh(repo, *uuids: str) -> None:
    for u in uuids:
        repo.execute(
            "CREATE (:Entity {uuid: $u, name: $u, summary: $u, namespace: 'default'})",
            params={"u": u},
        )


def _live_statuses(repo, *uuids: str) -> dict[str, tuple[str | None, str | None]]:
    rows = repo.execute(
        "MATCH (n:Entity) WHERE n.uuid IN $u RETURN n.uuid AS u, n.conflict_group_id AS g, "
        "n.conflict_status AS s",
        params={"u": list(uuids)},
    )
    return {r["u"]: (r["g"], r["s"]) for r in rows}


def _ids(prefix: str) -> list[str]:
    tag = uuidlib.uuid4().hex[:8]
    return [f"{prefix}-{tag}-{i}" for i in range(4)]


@pytest.mark.online
def test_live_pending_pair_joining_unresolved_group_keeps_it_visible(test_neo4j_repo) -> None:
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    a, b, n, _ = _ids("ju")
    gid = f"g-{a}"
    _seed(test_neo4j_repo, gid, {a: "unresolved", b: "unresolved"})
    _fresh(test_neo4j_repo, n)
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)

    canonical, _ = adapter.set_conflict(a, n, "unused", initial_status="pending_llm_review")

    assert canonical == gid
    assert _live_statuses(test_neo4j_repo, a, b, n) == {
        a: (gid, "unresolved"), b: (gid, "unresolved"), n: (gid, "pending_llm_review"),
    }


@pytest.mark.online
def test_live_promoted_pair_joining_pending_group_does_not_confirm_the_rest(test_neo4j_repo) -> None:
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    a, b, p, _ = _ids("jp")
    gid = f"g-{a}"
    _seed(test_neo4j_repo, gid, {a: "pending_llm_review", b: "pending_llm_review"})
    _fresh(test_neo4j_repo, p)
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)

    adapter.set_conflict(a, p, "unused", initial_status="unresolved")

    assert _live_statuses(test_neo4j_repo, a, b, p) == {
        a: (gid, "unresolved"), b: (gid, "pending_llm_review"), p: (gid, "unresolved"),
    }


@pytest.mark.online
def test_live_merging_groups_keeps_each_members_status(test_neo4j_repo) -> None:
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    a, b, c, d = _ids("mg")
    g1, g2 = f"g1-{a}", f"g2-{c}"
    _seed(test_neo4j_repo, g1, {a: "unresolved", b: "unresolved"})
    _seed(test_neo4j_repo, g2, {c: "pending_llm_review", d: "pending_llm_review"})
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)

    adapter.set_conflict(a, c, "unused", initial_status="pending_llm_review")

    assert _live_statuses(test_neo4j_repo, a, b, c, d) == {
        a: (g1, "unresolved"), b: (g1, "unresolved"),
        c: (g1, "pending_llm_review"), d: (g1, "pending_llm_review"),
    }


@pytest.mark.online
def test_live_member_ops_and_full_listing(test_neo4j_repo) -> None:
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    a, b, c, _ = _ids("mo")
    gid = f"g-{a}"
    _seed(test_neo4j_repo, gid, {a: "unresolved", b: "unresolved", c: "pending_llm_review"})
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)

    rows = adapter.list_conflict_groups(status="pending_llm_review", include_all_members=True)
    row = next(r for r in rows if r["group_id"] == gid)
    assert {m["uuid"]: m["status"] for m in row["members"]} == {
        a: "unresolved", b: "unresolved", c: "pending_llm_review",
    }

    # Compare-and-set: wrong from_status is a no-op.
    assert adapter.set_conflict_member_status(gid, c, from_status="unresolved", to_status="unresolved") is False
    assert adapter.remove_conflict_member(gid, c, from_status="unresolved")["removed"] is False

    out = adapter.remove_conflict_member(gid, c, from_status="pending_llm_review")
    assert out["removed"] is True and out["dissolved"] is False
    assert _live_statuses(test_neo4j_repo, a, b, c) == {
        a: (gid, "unresolved"), b: (gid, "unresolved"), c: (None, "false_positive"),
    }

    out = adapter.remove_conflict_member(gid, b, from_status="unresolved")
    assert out["removed"] is True and out["dissolved"] is True
    assert _live_statuses(test_neo4j_repo, a, b) == {
        a: (None, "false_positive"), b: (None, "false_positive"),
    }
    # The lock token never persists.
    leftover = test_neo4j_repo.execute(
        "MATCH (n:Entity) WHERE n._conflict_group_lock IS NOT NULL RETURN count(n) AS c"
    )
    assert leftover[0]["c"] == 0


@pytest.mark.online
def test_live_overview_counts_pending_conflict_groups(test_neo4j_repo) -> None:
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    a, b, c, d = _ids("ov")
    ns = f"ns-{a}"
    for u, gid, status in ((a, "g1", "pending_llm_review"), (b, "g1", "pending_llm_review"),
                           (c, "g2", "unresolved"), (d, "g2", "unresolved")):
        test_neo4j_repo.execute(
            "CREATE (:Entity {uuid: $u, name: $u, namespace: $ns, conflict_group_id: $gid, "
            "conflict_status: $s, conflict_created_at: datetime('2026-11-01T00:00:00Z')})",
            params={"u": u, "ns": ns, "gid": f"{gid}-{a}", "s": status},
        )
    overview = MemoryGraphAdapter(neo4j=test_neo4j_repo).fetch_memory_overview(namespace=ns)
    assert overview["pending_conflict_groups"] == 1
    assert str(overview["oldest_pending_conflict_at"]).startswith("2026-11-01")
