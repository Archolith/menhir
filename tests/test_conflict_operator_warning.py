"""Conflicts surface like FAILED saves: a warning line on writes and at session start,
acknowledgeable, raised again by a newly confirmed or newly stuck conflict."""

from __future__ import annotations

import asyncio
import uuid as uuidlib
from typing import Any

import pytest

from menhir.infrastructure.telemetry.operator_warnings_store import (
    build_operator_warnings,
    redact_operator_warnings_for_caller,
)
from menhir.mcp.formatters import _operator_warning_line, _queue_summary, session_start_warning_lines
from tests.test_operator_warnings import _Backend, _reset, store  # noqa: F401 - fixtures
from tests.test_operator_warnings_followups import _Caller

T1 = "2026-11-03T10:00:00.000000+00:00"
T2 = "2026-11-03T11:00:00.000000+00:00"


def _conflicts(confirmed: int = 0, stale: int = 0, at: str | None = T1) -> dict[str, Any]:
    return {
        "failed_count": 0,
        "latest_failed_at": None,
        "unresolved_conflict_groups": confirmed,
        "stale_pending_conflict_groups": stale,
        "latest_conflict_attention_at": at,
    }


def _warnings(backend: _Backend) -> list[dict[str, Any]]:
    return asyncio.run(backend.get_operator_warnings(_fresh=True))["warnings"]


@pytest.mark.unit
def test_no_conflicts_needing_attention_no_warning(store) -> None:  # noqa: F811
    backend = _Backend(store)
    backend.overview = _conflicts()
    assert _warnings(backend) == []
    assert asyncio.run(_queue_summary(backend)) == "queue_depth=0, active_enriching=0, scheduler=running"


@pytest.mark.unit
def test_confirmed_conflict_warns_on_writes_and_session_start(store) -> None:  # noqa: F811
    backend = _Backend(store)
    backend.overview = _conflicts(confirmed=2)
    (w,) = _warnings(backend)
    assert w["kind"] == "conflicts" and w["acked"] is False and w["unresolved_groups"] == 2

    summary = asyncio.run(_queue_summary(backend))
    assert "WARNING: 2 conflict groups have a confirmed contradiction nobody has decided" in summary
    assert "review_inbox" in summary
    lines = asyncio.run(session_start_warning_lines(backend))
    assert len(lines) == 1 and lines[0].startswith("WARNING: 2 conflict groups")


@pytest.mark.unit
def test_ack_holds_until_a_new_conflict_needs_attention(store) -> None:  # noqa: F811
    backend = _Backend(store)
    backend.overview = _conflicts(confirmed=1, at=T1)
    ack = asyncio.run(backend.acknowledge_operator_warning("conflicts", "", "alice"))
    assert ack["acknowledged"] == ["global"]
    assert _warnings(backend)[0]["acked"] is True
    assert "WARNING" not in asyncio.run(_queue_summary(backend))

    # Another group confirmed later: the ack does not cover it.
    backend.overview = _conflicts(confirmed=2, at=T2)
    assert _warnings(backend)[0]["acked"] is False
    # Everything decided: no warning.
    backend.overview = _conflicts()
    assert _warnings(backend) == []


@pytest.mark.unit
def test_stale_pending_review_warns_alone() -> None:
    (w,) = build_operator_warnings(
        [], unresolved_conflict_groups=0, stale_pending_conflict_groups=1,
        latest_conflict_attention_at=T1,
    )
    line = _operator_warning_line(w)
    assert line is not None
    assert "1 conflict group has a possible contradiction waiting over 24h for LLM review" in line
    assert "run_llm_conflict_review" in line
    assert "confirmed" not in line


@pytest.mark.unit
def test_warning_text_has_no_silencing_hint() -> None:
    (w,) = build_operator_warnings([], unresolved_conflict_groups=1, latest_conflict_attention_at=T1)
    line = _operator_warning_line(w) or ""
    assert "1 conflict group has a confirmed contradiction nobody has decided" in line
    assert "acknowledge" not in line.lower()


@pytest.mark.unit
@pytest.mark.parametrize("bad", [float("inf"), "x", None, -1, {"a": 1}])
def test_malformed_counts_render_nothing_and_never_raise(bad) -> None:
    entry = {"kind": "conflicts", "summary": "s", "unresolved_groups": bad, "stale_pending_groups": bad}
    assert _operator_warning_line(entry) is None


@pytest.mark.unit
def test_write_response_redacts_for_a_pinned_caller_even_if_the_serve_did_not(store, monkeypatch) -> None:  # noqa: F811
    """F6: the add_memory surface filters on the client side like the session-start surface."""

    class _UnredactingServe(_Backend):
        async def get_operator_warnings(self, _fresh: bool = False):
            return {"warnings": build_operator_warnings(
                [], unresolved_conflict_groups=2, latest_conflict_attention_at=T1,
            )}

    backend = _UnredactingServe(store)
    with _Caller(monkeypatch, client_name="bot", tier="agent", pin="game"):
        assert "WARNING" not in asyncio.run(_queue_summary(backend))
    with _Caller(monkeypatch, client_name="ops", tier="operator"):
        assert "WARNING" in asyncio.run(_queue_summary(backend))


@pytest.mark.unit
def test_pinned_caller_does_not_see_the_deployment_wide_count() -> None:
    warnings = build_operator_warnings([], unresolved_conflict_groups=3, latest_conflict_attention_at=T1)
    assert redact_operator_warnings_for_caller(warnings, pinned=True, tier="agent") == []
    assert len(redact_operator_warnings_for_caller(warnings, pinned=False, tier="agent")) == 1


# ---------------------------------------------------------------------------
# Live: the overview counts and the confirmation stamp that drives re-raising
# ---------------------------------------------------------------------------

@pytest.mark.online
def test_live_overview_counts_and_confirmation_moves_the_watermark(test_neo4j_repo) -> None:
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    tag = uuidlib.uuid4().hex[:8]
    ns = f"ns-{tag}"
    a, b, c, d, e, f = (f"{x}-{tag}" for x in "abcdef")
    seed = [
        # Confirmed long ago.
        (a, "g1", "unresolved", "2026-01-01T00:00:00Z"),
        (b, "g1", "unresolved", "2026-01-01T00:00:00Z"),
        # Pending for days: stuck.
        (c, "g2", "pending_llm_review", "2026-01-02T00:00:00Z"),
        (d, "g2", "pending_llm_review", "2026-01-02T00:00:00Z"),
        # Detected long ago, still pending; confirmed below.
        (e, "g3", "pending_llm_review", "2026-01-03T00:00:00Z"),
        (f, "g3", "pending_llm_review", "2026-01-03T00:00:00Z"),
    ]
    for u, g, s, at in seed:
        test_neo4j_repo.execute(
            "CREATE (:Entity {uuid: $u, name: $u, namespace: $ns, conflict_group_id: $g, "
            "conflict_status: $s, conflict_created_at: datetime($at)})",
            params={"u": u, "ns": ns, "g": f"{g}-{tag}", "s": s, "at": at},
        )
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)

    before = adapter.fetch_memory_overview(namespace=ns)
    assert before["unresolved_conflict_groups"] == 1
    assert before["stale_pending_conflict_groups"] == 2
    # Newest "became stuck" time: g3 created 01-03 + 24h.
    assert str(before["latest_conflict_attention_at"]).startswith("2026-01-04")

    # Review confirms an old detection today: the watermark moves to now.
    assert adapter.set_conflict_member_status(
        f"g3-{tag}", e, from_status="pending_llm_review", to_status="unresolved",
    ) is True
    now = test_neo4j_repo.execute("RETURN toString(datetime()) AS t")[0]["t"]
    after = adapter.fetch_memory_overview(namespace=ns)
    assert after["unresolved_conflict_groups"] == 2
    assert str(after["latest_conflict_attention_at"])[:10] == now[:10]


def _overview(repo, ns):
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    return MemoryGraphAdapter(neo4j=repo).fetch_memory_overview(namespace=ns)


@pytest.mark.online
def test_live_requeue_is_not_instantly_stuck(test_neo4j_repo) -> None:
    """F1: a requeued group starts a new review window; it is not "pending over 24h"
    because it was first detected days ago."""
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    tag = uuidlib.uuid4().hex[:8]
    ns = f"ns-{tag}"
    for u in ("a", "b"):
        test_neo4j_repo.execute(
            "CREATE (:Entity {uuid: $u, name: $u, namespace: $ns, conflict_group_id: $g, "
            "conflict_status: 'unresolved', conflict_created_at: datetime() - duration('P3D'), "
            "conflict_confirmed_at: datetime() - duration('P2D')})",
            params={"u": f"{u}-{tag}", "ns": ns, "g": f"g-{tag}"},
        )
    before = _overview(test_neo4j_repo, ns)
    assert before["unresolved_conflict_groups"] == 1

    MemoryGraphAdapter(neo4j=test_neo4j_repo).requeue_conflicts_for_llm_review(
        from_status="unresolved", namespace=ns,
    )
    after = _overview(test_neo4j_repo, ns)
    assert after["unresolved_conflict_groups"] == 0
    assert after["pending_conflict_groups"] == 1
    assert after["stale_pending_conflict_groups"] == 0
    assert after["latest_conflict_attention_at"] is None


@pytest.mark.online
def test_live_protected_members_are_not_counted(test_neo4j_repo) -> None:
    """F3: members no conflict tool can list or settle must not raise a warning."""
    tag = uuidlib.uuid4().hex[:8]
    ns = f"ns-{tag}"
    test_neo4j_repo.execute(
        """
        CREATE (v:Entity {uuid: $v, name: 'view', namespace: $ns, is_view: true, view_current: true})
        CREATE (a:Entity {uuid: $a, name: 'a', namespace: $ns, conflict_group_id: $g,
                          conflict_status: 'unresolved', conflict_created_at: datetime()})
        CREATE (b:Entity {uuid: $b, name: 'b', namespace: $ns, conflict_group_id: $g,
                          conflict_status: 'unresolved', conflict_created_at: datetime()})
        CREATE (a)-[:MENTIONS]->(v), (b)-[:MENTIONS]->(v)
        """,
        params={"v": f"v-{tag}", "a": f"a-{tag}", "b": f"b-{tag}", "ns": ns, "g": f"g-{tag}"},
    )
    overview = _overview(test_neo4j_repo, ns)
    assert overview["unresolved_conflict_groups"] == 0
    assert overview["latest_conflict_attention_at"] is None


@pytest.mark.online
def test_live_promoted_pair_is_stamped_confirmed_and_pending_pair_is_not(test_neo4j_repo) -> None:
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    tag = uuidlib.uuid4().hex[:8]
    a, b, c, d = (f"{x}-{tag}" for x in "abcd")
    for u in (a, b, c, d):
        test_neo4j_repo.execute(
            "CREATE (:Entity {uuid: $u, name: $u, namespace: 'default'})", params={"u": u},
        )
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    adapter.set_conflict(a, b, f"gp-{tag}", initial_status="unresolved")
    adapter.set_conflict(c, d, f"gq-{tag}", initial_status="pending_llm_review")
    rows = test_neo4j_repo.execute(
        "MATCH (n:Entity) WHERE n.uuid IN $u RETURN n.uuid AS u, "
        "n.conflict_confirmed_at IS NOT NULL AS confirmed, "
        "n.conflict_pending_since IS NOT NULL AS pending",
        params={"u": [a, b, c, d]},
    )
    got = {r["u"]: (r["confirmed"], r["pending"]) for r in rows}
    assert got == {a: (True, False), b: (True, False), c: (False, True), d: (False, True)}
