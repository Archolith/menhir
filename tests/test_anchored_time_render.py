"""P2 render: persisted anchored-time contracts shown as event times, behind MENHIR_ANCHORED_TIME_RENDER.

Flag off must be identical to today on every surface (recall facts, context lines, MCP items,
timeline query and payload). Flag on must never show a speech date as an occurrence date.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from menhir.domain.event_time import (
    EVENT_TIME_PROPERTIES,
    EventTime,
    event_time_from_row,
    rendered_event_time,
)
from menhir.domain.timeline import TimelineEntry, TimelineFact, TimelineResult

pytestmark = pytest.mark.unit

SPEECH = "2023-05-14"
SPEECH_VALID_AT = "2023-05-14T15:00:00Z"


def contract(**overrides: Any) -> dict[str, Any]:
    row = {
        "time_basis": "speech_relative", "time_expression": "a month ago",
        "time_kind": "point_event", "time_granularity": "month",
        "time_window_start": "2023-04-01", "time_window_end": "2023-04-30",
        "time_outcome": "written", "time_speech_date": SPEECH,
    }
    row.update(overrides)
    return row


def render(valid_at: str | None = SPEECH_VALID_AT, **overrides: Any) -> str:
    et = event_time_from_row(contract(**overrides))
    assert et is not None
    return et.render(valid_at)


# --- renderer ---------------------------------------------------------------------------------


def test_written_month_window() -> None:
    assert render("2023-04-15T00:00:00Z") == "~2023-04 (from 'a month ago')"


def test_graphiti_inside_window_shows_the_window() -> None:
    assert render("2023-04-20T00:00:00Z", time_outcome="graphiti_inside_window") == (
        "~2023-04 (from 'a month ago')")


@pytest.mark.parametrize(("granularity", "start", "end", "expected"), [
    ("day", "2023-04-07", "2023-04-07", "2023-04-07"),
    (None, "2023-04-07", "2023-04-07", "2023-04-07"),
    ("week", "2023-04-03", "2023-04-09", "~2023-04-03..2023-04-09"),
    ("year", "2022-01-01", "2022-12-31", "~2022"),
    ("month", "2023-03-15", "2023-04-14", "~2023-03-15..2023-04-14"),
])
def test_window_text_by_granularity(granularity, start, end, expected) -> None:
    out = render(time_granularity=granularity, time_window_start=start, time_window_end=end,
                 time_expression=None)
    assert out == expected


def test_plan_with_window_is_marked_planned() -> None:
    assert render(time_kind="plan", time_outcome="plan", time_expression="next month",
                  time_window_start="2023-06-01", time_window_end="2023-06-30") == (
        "planned ~2023-06 (from 'next month')")


def test_plan_without_window_never_shows_the_speech_date() -> None:
    out = render(time_kind="plan", time_outcome="open_or_unplaceable", time_expression="someday",
                 time_window_start=None, time_window_end=None)
    assert out == "planned, time unknown (said 2023-05-14; 'someday')"


def test_graphiti_resolved_shows_graphiti_date() -> None:
    assert render("2023-02-10T00:00:00Z", time_outcome="graphiti_resolved") == (
        "2023-02-10 (from 'a month ago')")


@pytest.mark.parametrize("outcome", ["undated", "guard", "no_item", "two_options",
                                     "would_invert_interval", "open_or_unplaceable"])
def test_speech_date_valid_at_renders_unknown(outcome) -> None:
    assert render(SPEECH_VALID_AT, time_outcome=outcome) == (
        "event time unknown (said 2023-05-14; 'a month ago')")


def test_undated_with_missing_valid_at_and_no_expression() -> None:
    out = render(None, time_basis="none", time_expression=None, time_granularity=None,
                 time_window_start=None, time_window_end=None, time_outcome="undated")
    assert out == "event time unknown (said 2023-05-14)"


def test_non_speech_valid_at_kept_when_not_written() -> None:
    assert render("2023-01-02T00:00:00Z", time_outcome="two_options") == (
        "2023-01-02 (from 'a month ago')")


@pytest.mark.parametrize("valid_at", [
    "2023-05-14T23:30:00-05:00",  # 2023-05-15 in UTC: not the speech date
    "2023-05-15T04:30:00Z",
    "2023-05-15T04:30:00Z[UTC]",
])
def test_valid_at_compared_as_utc_date(valid_at) -> None:
    assert render(valid_at, time_outcome="undated").startswith("2023-05-15")


def test_no_contract_means_no_event_time() -> None:
    assert event_time_from_row({"fact": "x", "valid_at": SPEECH_VALID_AT}) is None
    assert rendered_event_time({"time_basis": None}) is None
    assert rendered_event_time({**contract(), "valid_at": "2023-04-15T00:00:00Z"}) == (
        "~2023-04 (from 'a month ago')")


def test_event_time_is_ascii_and_single_line() -> None:
    out = EventTime(basis="explicit_date", expression="x", granularity="day",
                    window_start="2023-04-07", window_end="2023-04-07",
                    outcome="written").render(None)
    assert out.isascii() and "\n" not in out


# --- recall facts -----------------------------------------------------------------------------


def _fact_rows() -> list[dict[str, Any]]:
    return [
        {"node_uuid": "n1", "fact": "moved", "valid_at": SPEECH_VALID_AT, "invalid_at": None,
         "created_at": "2023-05-14T15:00:01Z", "expired_at": None, **contract(time_outcome="undated")},
        {"node_uuid": "n1", "fact": "legacy", "valid_at": "2022-01-01T00:00:00Z",
         "invalid_at": None, "created_at": "2023-05-14T15:00:00Z", "expired_at": None},
    ]


def test_build_temporal_facts_flag_off_is_unchanged() -> None:
    from menhir.services.recall_policies import _build_temporal_facts

    off = _build_temporal_facts(_fact_rows())
    legacy_rows = [{k: v for k, v in r.items() if not k.startswith("time_")} for r in _fact_rows()]
    assert off == _build_temporal_facts(legacy_rows)
    assert all(f.event_time is None for f in off["n1"])


def test_build_temporal_facts_flag_on_renders_contracts_only() -> None:
    from menhir.services.recall_policies import _build_temporal_facts

    facts = {f.fact: f for f in _build_temporal_facts(_fact_rows(), event_time=True)["n1"]}
    assert facts["moved"].event_time == "event time unknown (said 2023-05-14; 'a month ago')"
    assert facts["moved"].valid_at == SPEECH_VALID_AT  # stored value is never rewritten
    assert facts["legacy"].event_time is None


def test_projection_carries_the_contract() -> None:
    from menhir.infrastructure.cypher import FACT_TEMPORAL_FIELDS

    for name in EVENT_TIME_PROPERTIES:
        assert f"r.{name} AS {name}" in FACT_TEMPORAL_FIELDS


def test_pipeline_passes_the_tuning_flag() -> None:
    import inspect

    from menhir.services import recall_pipeline

    src = inspect.getsource(recall_pipeline)
    assert "event_time=tuning.enable_anchored_time_render" in src


# --- context builder and MCP formatter --------------------------------------------------------


def _memory(**fact_overrides: Any) -> SimpleNamespace:
    from menhir.domain.recall import TemporalFact

    fields = dict(fact="moved", valid_at=SPEECH_VALID_AT, invalid_at=None, created_at=None,
                  expired_at=None, is_current_belief=True, temporal_role="current_belief")
    fields.update(fact_overrides)
    return SimpleNamespace(temporal_facts=(TemporalFact(**fields),))


def test_context_lines_unchanged_without_event_time() -> None:
    from menhir.services.context_builder import _source_time_lines

    assert _source_time_lines(_memory()) == [
        "  Source-time evidence:",
        f"  - {SPEECH_VALID_AT} | moved | belief: current belief",
    ]


def test_context_lines_use_event_time() -> None:
    from menhir.services.context_builder import _source_time_lines

    lines = _source_time_lines(_memory(event_time="~2023-04 (from 'a month ago')",
                                       invalid_at="2023-05-01T00:00:00Z"))
    assert lines[1] == ("  - ~2023-04 (from 'a month ago') through 2023-05-01T00:00:00Z | moved"
                        " | belief: current belief")


def test_mcp_fact_item_gains_event_time_only_when_rendered() -> None:
    from menhir.mcp.formatters import _format_when, _with_event_time

    plain = asdict(_memory().temporal_facts[0])
    assert _with_event_time({"fact": "moved"}, plain) == {"fact": "moved"}
    assert _format_when(plain).startswith(f"happened from {SPEECH_VALID_AT};")

    rendered = asdict(_memory(event_time="~2023-04").temporal_facts[0])
    assert _with_event_time({"fact": "moved"}, rendered) == {"fact": "moved",
                                                              "event_time": "~2023-04"}
    assert _format_when(rendered).startswith("happened ~2023-04;")


def test_rest_fact_model_omits_absent_event_time() -> None:
    from menhir.api.routes_support import RecallTemporalFact, RecallTimelineFactResponse

    assert "event_time" not in RecallTemporalFact(fact="x").model_dump(exclude_none=True)
    assert "event_time" not in RecallTimelineFactResponse(fact="x").model_dump(exclude_none=True)


# --- timeline ---------------------------------------------------------------------------------


@dataclass
class _Recorder:
    calls: list[tuple[str, dict[str, object] | None]] = field(default_factory=list)

    def execute(self, query: str, params: dict[str, object] | None = None):
        self.calls.append((query, params))
        return []


def _timeline_query(**kwargs: Any) -> str:
    from menhir.infrastructure.memory_queries import MemoryQueryRepository

    neo4j = _Recorder()
    MemoryQueryRepository(neo4j).timeline_facts(  # type: ignore[arg-type]
        episode_uuids=["ep"], namespace="ns", **kwargs)
    return neo4j.calls[0][0]


def test_timeline_query_flag_off_is_unchanged() -> None:
    assert _timeline_query() == _timeline_query(event_time=False)
    query = _timeline_query()
    assert "ORDER BY r.valid_at\n" in query and "time_" not in query


def test_timeline_query_flag_on_orders_by_window_and_projects_contract() -> None:
    query = _timeline_query(event_time=True)
    assert "ORDER BY coalesce(r.time_window_start, toString(r.valid_at))" in query
    for name in EVENT_TIME_PROPERTIES:
        assert f"{name}: r.{name}" in query
    assert "tenant_namespaces" in query


def _row() -> dict[str, Any]:
    return {"uuid": "ep", "valid_at": "2023-05-14T15:00:00Z", "created_at": None,
            "content": "c", "session_id": None, "source": None}


def _facts_map() -> dict[str, list[dict[str, Any]]]:
    return {"ep": [{"fact": "moved", "valid_at": SPEECH_VALID_AT, "invalid_at": None,
                    "expired_at": None, **contract(time_outcome="undated")}]}


def test_timeline_entry_renders_only_when_on() -> None:
    from menhir.services.timeline_service import _entry_from_row

    off = _entry_from_row(_row(), detail="headline", facts_map=_facts_map())
    assert off.facts[0].event_time is None and off.facts[0].time_basis == "world"
    on = _entry_from_row(_row(), detail="headline", facts_map=_facts_map(), event_time=True)
    assert on.facts[0].event_time == "event time unknown (said 2023-05-14; 'a month ago')"


async def test_timeline_service_passes_kwarg_only_when_on() -> None:
    from menhir.services.timeline_service import run_recall_timeline

    calls: list[dict[str, Any]] = []

    class Adapter:
        def timeline_anchor(self, *, uuid, namespace, subject_uuid=None):
            return _row()

        def timeline_page(self, **kwargs):
            return []

        def timeline_facts(self, **kwargs):
            calls.append(kwargs)
            return _facts_map()

    service = SimpleNamespace(graph_adapter=Adapter())
    off = await run_recall_timeline(service, namespace="ns", around="ep", facts=True)
    on = await run_recall_timeline(service, namespace="ns", around="ep", facts=True,
                                   event_time=True)
    assert "event_time" not in calls[0] and calls[1]["event_time"] is True
    assert off.entries[0].facts[0].event_time is None
    assert on.entries[0].facts[0].event_time is not None


def _ops(settings: Any, fact: TimelineFact):
    from menhir.core.backend_runtime_data_ops import RuntimeProviderDataOpsMixin

    seen: dict[str, Any] = {}

    async def recall_timeline(**kwargs):
        seen.update(kwargs)
        entry = TimelineEntry(uuid="ep", recorded_at="t", created_at=None, session_id=None,
                              source=None, headline="h", content=None, facts=(fact,),
                              is_anchor=True)
        return TimelineResult(thread="namespace", subject_uuid=None, subject_name=None,
                              entries=(entry,), prev_cursor=None, next_cursor=None,
                              histories=(), note=None)

    ops = object.__new__(RuntimeProviderDataOpsMixin)
    ops.built = SimpleNamespace(settings=settings,
                                recall_service=SimpleNamespace(recall_timeline=recall_timeline))
    return ops, seen


async def test_backend_payload_flag_off_has_no_event_time_key() -> None:
    fact = TimelineFact(fact="moved", valid_at=SPEECH_VALID_AT, invalid_at=None, expired_at=None)
    ops, seen = _ops(SimpleNamespace(anchored_time_render_enabled=False), fact)
    payload = await ops.recall_timeline(namespace="ns", around="ep", facts=True)
    assert "event_time" not in seen
    assert payload["entries"][0]["facts"][0] == {
        "fact": "moved", "valid_at": SPEECH_VALID_AT, "invalid_at": None, "expired_at": None,
        "time_basis": "world"}


async def test_backend_payload_flag_on_keeps_rendered_event_time() -> None:
    fact = TimelineFact(fact="moved", valid_at=SPEECH_VALID_AT, invalid_at=None, expired_at=None,
                        event_time="~2023-04")
    ops, seen = _ops(SimpleNamespace(anchored_time_render_enabled=True), fact)
    payload = await ops.recall_timeline(namespace="ns", around="ep", facts=True)
    assert seen["event_time"] is True
    assert payload["entries"][0]["facts"][0]["event_time"] == "~2023-04"


# --- flag plumbing ----------------------------------------------------------------------------


def test_flag_defaults_off_and_reaches_tuning(monkeypatch) -> None:
    from menhir.config.settings_model import MemorySettings

    monkeypatch.delenv("MENHIR_ANCHORED_TIME_RENDER", raising=False)
    off = MemorySettings.from_env()
    assert off.anchored_time_render_enabled is False
    assert off.retrieval_tuning().enable_anchored_time_render is False

    monkeypatch.setenv("MENHIR_ANCHORED_TIME_RENDER", "true")
    on = MemorySettings.from_env()
    assert on.anchored_time_render_enabled is True
    assert on.retrieval_tuning().enable_anchored_time_render is True


def test_flag_is_registered_as_recall_affecting() -> None:
    from menhir.config import feature_flags

    rows = [r for r in feature_flags._SETTINGS if r[1] == "MENHIR_ANCHORED_TIME_RENDER"]
    assert len(rows) == 1
    setting, _env, default, _category, _desc, recall_affecting, _extra = rows[0]
    assert setting == "anchored_time_render_enabled" and default is False and recall_affecting
