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
    assert "WARNING: 2 groups of contradicting memories are confirmed and undecided" in summary
    assert "list_conflicts" in summary
    lines = asyncio.run(session_start_warning_lines(backend))
    assert len(lines) == 1 and lines[0].startswith("WARNING: 2 groups")


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
    assert "1 possible conflict group has waited over 24h for LLM review" in line
    assert "confirmed" not in line


@pytest.mark.unit
def test_warning_text_has_no_silencing_hint() -> None:
    (w,) = build_operator_warnings([], unresolved_conflict_groups=1, latest_conflict_attention_at=T1)
    line = _operator_warning_line(w) or ""
    assert "1 group of contradicting memories is confirmed and undecided" in line
    assert "acknowledge" not in line.lower()


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
    after = adapter.fetch_memory_overview(namespace=ns)
    assert after["unresolved_conflict_groups"] == 2
    assert str(after["latest_conflict_attention_at"]) > "2026-11"
