"""#1009: one failure event each time an episode is excluded by the repair failure limit."""

from __future__ import annotations

from typing import Any

import pytest

from menhir.services import source_embedding_repair as repair_mod
from tests.test_source_embedding_repair import _Graph, _run

pytestmark = [pytest.mark.unit]


class _CooledGraph(_Graph):
    """Lists the episodes in ``cooled`` whatever their count: their cooldown has passed."""

    def __init__(self, contents: dict[str, str]) -> None:
        super().__init__(contents)
        self.cooled: set[str] = set()

    def list_episodes_missing_content_embedding(self, *args: Any, max_failures=None, **kw: Any):
        rows = super().list_episodes_missing_content_embedding(*args, max_failures=None, **kw)
        return [r for r in rows if max_failures is None or r["uuid"] in self.cooled
                or self.failures.get(r["uuid"], 0) < max_failures]


async def _empty(text: str) -> list[float]:
    return []


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(repair_mod, "record_failure_event", lambda **kw: events.append(kw) or True)
    return events


def test_event_recorded_once_at_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    events = _capture(monkeypatch)
    graph = _Graph({"a": "text"})
    results = [_run(graph, _empty) for _ in range(repair_mod.MAX_EPISODE_FAILURES + 2)]
    assert [r["excluded"] for r in results] == [0, 0, 1, 0, 0]
    assert len(events) == 1
    ev = events[0]
    assert ev["operation"] == "scheduler_source_embedding_repair"
    assert ev["failure_stage"] == "source_embedding_excluded"
    assert ev["classification"] == "exhausted" and ev["retryable"] is False
    assert ev["episode_uuid"] == "a"
    assert ev["details"]["failure_limit"] == repair_mod.MAX_EPISODE_FAILURES
    assert "text" not in str(ev)


def test_reexclusion_after_cooldown_records_a_new_event(monkeypatch: pytest.MonkeyPatch) -> None:
    events = _capture(monkeypatch)
    graph = _CooledGraph({"a": "text"})
    graph.failures = {"a": repair_mod.MAX_EPISODE_FAILURES}
    assert _run(graph, _empty)["excluded"] == 0 and events == []  # still cooling: not listed
    graph.cooled.add("a")
    assert _run(graph, _empty)["excluded"] == 1  # relisted, failed again: excluded again
    assert len(events) == 1 and events[0]["episode_uuid"] == "a"


def test_excluded_count_multiple(monkeypatch: pytest.MonkeyPatch) -> None:
    events = _capture(monkeypatch)
    graph = _Graph({"a": "x", "b": "y"})
    graph.failures = {"a": 2, "b": 2}
    result = _run(graph, _empty)
    assert result["excluded"] == 2 and len(events) == 2


def test_event_failure_does_not_fail_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(**kw: Any) -> bool:
        raise RuntimeError("telemetry down")

    monkeypatch.setattr(repair_mod, "record_failure_event", _raise)
    graph = _Graph({"a": "x"})
    graph.failures = {"a": 2}
    result = _run(graph, _empty)
    assert result["excluded"] == 1 and result["stopped"] is None


def test_reexclusion_event_survives_the_real_event_store_dedup(isolated_telemetry_db) -> None:
    """Review F14-1: the failure store drops a row identical to an earlier one (no time window).
    Each exclusion carries its own failure count as the attempt, so both are stored."""
    import sqlite3

    graph = _CooledGraph({"a": "text"})
    graph.failures = {"a": repair_mod.MAX_EPISODE_FAILURES - 1}
    assert _run(graph, _empty)["excluded"] == 1  # count 3
    graph.cooled.add("a")
    assert _run(graph, _empty)["excluded"] == 1  # count 4, after the cooldown
    with sqlite3.connect(isolated_telemetry_db) as conn:
        attempts = [r[0] for r in conn.execute(
            "SELECT processing_attempt FROM failure_events "
            "WHERE failure_stage = 'source_embedding_excluded' ORDER BY processing_attempt")]
    assert attempts == [3, 4]
