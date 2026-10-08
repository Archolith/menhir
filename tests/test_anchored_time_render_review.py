"""Regressions for the Codex review of PR #245: 245-4, 245-5, 245-6, 245-7, 245-8 and X-1."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from menhir.domain.event_time import EVENT_TIME_PROPERTIES, EventTime, rendered_event_time
from menhir.domain.recall import TemporalFact
from menhir.infrastructure.anchored_time import AnchoredTimeReport, EdgeTimeInput, plan_overlay
from menhir.infrastructure.anchored_time_persist import CONTRACT_PROPERTIES, contract_rows

pytestmark = pytest.mark.unit

SPEECH = date(2024, 2, 14)  # a Wednesday
LAST_TUESDAY = {"expression": "last Tuesday", "basis": "speech_relative", "kind": "point_event",
                "calendar": {"which": "last", "unit": "weekday", "name": "tuesday"}}


def _fact(fact: str, event_time: str | None = None) -> TemporalFact:
    return TemporalFact(fact=fact, valid_at="2024-07-01T00:00:00Z", invalid_at=None,
                        created_at=None, expired_at=None, is_current_belief=True,
                        temporal_role="current_belief", event_time=event_time)


def _render_chain(graphiti_valid_at: datetime | None) -> str | None:
    """plan_overlay -> contract_rows -> stored row -> rendered_event_time, as in production."""
    (result,) = plan_overlay(
        [EdgeTimeInput(uuid="e0", fact="f", valid_at=graphiti_valid_at, invalid_at=None)],
        {0: dict(LAST_TUESDAY)}, SPEECH)
    report = AnchoredTimeReport(status="ok", model="m", results=(result,))
    (row,) = contract_rows(report, ["e0"], SPEECH)
    stored = {k: v for k, v in row.items() if k in EVENT_TIME_PROPERTIES}
    if graphiti_valid_at is not None:
        stored["valid_at"] = graphiti_valid_at.isoformat()
    return rendered_event_time(stored)


# --- 245-4: two candidate days are not an interval --------------------------------------------


def test_ambiguity_is_persisted_and_projected() -> None:
    assert "time_ambiguity" in CONTRACT_PROPERTIES and "time_ambiguity" in EVENT_TIME_PROPERTIES


def test_graphiti_candidate_is_shown_not_the_span() -> None:
    out = _render_chain(datetime(2024, 2, 13, tzinfo=timezone.utc))
    assert out == "2024-02-13 (from 'last Tuesday')"
    assert ".." not in out


def test_two_candidates_render_as_alternatives() -> None:
    et = EventTime(basis="speech_relative", expression="last Tuesday", granularity="day",
                   window_start="2024-02-06", window_end="2024-02-13",
                   outcome="graphiti_inside_window", ambiguity="two_options")
    assert et.render("2024-02-10T00:00:00Z") == "2024-02-06 or 2024-02-13 (from 'last Tuesday')"


def test_unwritten_two_options_still_renders_unknown() -> None:
    out = _render_chain(None)
    assert out == "event time unknown (said 2024-02-14; 'last Tuesday')"


# --- 245-6: only whole calendar windows are abbreviated ---------------------------------------


@pytest.mark.parametrize(("granularity", "start", "end", "expected"), [
    ("month", "2024-04-05", "2024-04-25", "~2024-04-05..2024-04-25"),
    ("month", "2024-02-01", "2024-02-28", "~2024-02-01..2024-02-28"),  # leap year: not whole
    ("month", "2024-02-01", "2024-02-29", "~2024-02"),
    ("year", "2023-03-01", "2023-10-31", "~2023-03-01..2023-10-31"),
    ("year", "2023-01-01", "2023-12-31", "~2023"),
])
def test_partial_windows_keep_their_bounds(granularity, start, end, expected) -> None:
    et = EventTime(basis="speech_relative", granularity=granularity, window_start=start,
                   window_end=end, outcome="written")
    assert et.render(None) == expected


# --- 245-7: a plan is not "happened" ----------------------------------------------------------


def test_mcp_plan_is_not_labelled_happened() -> None:
    from menhir.mcp.formatters import _format_when

    plan = asdict(_fact("trip", "planned ~2024-08"))
    assert _format_when(plan).startswith("planned ~2024-08;")
    assert "happened" not in _format_when(plan)


# --- 245-8: one line whatever the stored expression -------------------------------------------


@pytest.mark.parametrize("expression", ["two days\nafter the move", "two days\r\nafter",
                                        "a b", "tab\there", "x\x00y"])
@pytest.mark.parametrize("outcome", ["written", "undated"])
def test_rendered_line_has_no_breaks_or_controls(expression, outcome) -> None:
    out = EventTime(basis="speech_relative", expression=expression, granularity="day",
                    window_start="2023-04-07", window_end="2023-04-07", outcome=outcome,
                    speech_date="2023-05-14").render("2023-05-14T00:00:00Z")
    assert out.splitlines() == [out]
    assert not any(ord(c) < 32 or 0x7f <= ord(c) <= 0x9f for c in out)


# --- 245-5 and X-1: explorer serializer -------------------------------------------------------


def test_explorer_fact_dict_omits_absent_event_time() -> None:
    from menhir.explorer.recall_lab import _temporal_fact_dict

    assert "event_time" not in _temporal_fact_dict(_fact("x"))
    assert _temporal_fact_dict(_fact("x", "~2023-04"))["event_time"] == (
        "~2023-04")


def test_explorer_redacts_event_time_expression() -> None:
    from menhir.explorer.recall_lab import _redact_temporal_facts
    from menhir.privacy import MASK

    text = "event time unknown (said 2024-07-01; 'two days after my diagnosis')"
    facts = [{"fact": "secret", "event_time": text, "valid_at": "2024-07-01"}]
    (hidden,) = _redact_temporal_facts(facts, reveal=False)
    assert hidden["event_time"] == MASK and hidden["valid_at"] == "2024-07-01"
    assert _redact_temporal_facts(facts, reveal=True)[0]["event_time"] == text


def test_explorer_serialized_result_flag_off_has_no_event_time() -> None:
    from menhir.explorer.recall_lab import _serialize_result

    memory = SimpleNamespace(uuid="m1", name="n", content="c", scope="s", memory_type="t",
                             final_score=1.0, temporal_facts=(_fact("x"),))
    out = _serialize_result(SimpleNamespace(trace=None, results=[memory]), reveal=False)
    (fact,) = out["results"][0]["temporal_facts"]
    assert "event_time" not in fact
