"""MENHIR_ANCHORED_TIME_ATTACH: the blind attach check inside the extraction hook (no network).

Covers the policy overlay with a fake attacher (agree, reject, every failure status, no candidates,
internal error, blind call arguments, flag-off report), the hook pass-through, the persisted
contract string, the render of the new outcomes, the settings/flag, and the client wiring.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("graphiti_core")

from graphiti_core.edges import EntityEdge  # noqa: E402

from menhir.config.feature_flags import FEATURES  # noqa: E402
from menhir.config.settings_model import MemorySettings  # noqa: E402
from menhir.domain.event_time import EventTime  # noqa: E402
import menhir.infrastructure.anchored_time_resolver as resolver_module  # noqa: E402
import menhir.infrastructure.graphiti_client as graphiti_client  # noqa: E402
import menhir.infrastructure.graphiti_extraction_policy as policy  # noqa: E402
from menhir.infrastructure.anchored_time import (  # noqa: E402
    AnchoredTimeReport,
    AnchoredTimeResult,
    EdgeTimeInput,
    plan_overlay,
)
from menhir.infrastructure.anchored_time_attach_client import (  # noqa: E402
    AnchoredTimeAttacher,
    AttachOutcome,
)
from menhir.infrastructure.anchored_time_persist import CONTRACT_PROPERTIES, contract_rows  # noqa: E402
from menhir.infrastructure.providers import ProviderConfig  # noqa: E402

pytestmark = pytest.mark.unit

SPEECH = datetime(2024, 2, 14, 18, 30, tzinfo=timezone.utc)
MID_JAN = datetime(2024, 1, 16, tzinfo=timezone.utc)
TURN = "I moved to Denver last month and I still love hiking."
STATE_TURN = "My new job starts next month."


def _edge(fact: str, uuid: str, valid_at=SPEECH) -> EntityEdge:
    return EntityEdge(
        uuid=uuid, group_id="ns", source_node_uuid="n-user", target_node_uuid=f"n-{uuid}",
        created_at=datetime(2024, 2, 14, 18, 31, tzinfo=timezone.utc), name="RELATES_TO", fact=fact,
        episodes=["ep-1"], valid_at=valid_at, invalid_at=None,
        reference_time=SPEECH, attributes={"note": "kept"},
    )


def _edges() -> list[EntityEdge]:
    return [_edge("The user moved to Denver.", "e0"), _edge("The user loves hiking.", "e1")]


def _writing_items() -> dict[int, dict]:
    """Fact 0 is a point event "last month" (the writing case of test_anchored_time_hook)."""
    return {
        0: {"i": 0, "expression": "last month", "basis": "speech_relative", "kind": "point_event",
            "offset": None, "calendar": {"which": "last", "unit": "month"}, "date": None, "anchor": None},
        1: {"i": 1, "expression": None, "basis": "none", "kind": "state",
            "offset": None, "calendar": None, "date": None, "anchor": None},
    }


def _nothing_items() -> dict[int, dict]:
    return {
        0: {"i": 0, "expression": None, "basis": "none", "kind": "point_event",
            "offset": None, "calendar": None, "date": None, "anchor": None},
        1: {"i": 1, "expression": None, "basis": "none", "kind": "state",
            "offset": None, "calendar": None, "date": None, "anchor": None},
    }


def _state_future_items() -> dict[int, dict]:
    return {0: {"i": 0, "expression": "next month", "basis": "speech_relative", "kind": "state",
                "offset": None, "calendar": {"which": "next", "unit": "month"}, "date": None,
                "anchor": None}}


class _FakeResolver:
    model = "gpt-6-luna"

    def __init__(self, items: dict[int, dict]) -> None:
        self._items = items

    async def resolve(self, turn, speech_date, facts):
        items = {i: dict(it) for i, it in self._items.items()}  # the guards mutate their input
        return SimpleNamespace(status="ok", items=items, latency_s=0.01, cached=False,
                               missing_events=[], error_class="")


class _FakeAttacher:
    model = "m2"
    prompt_version = "abcd1234"

    def __init__(self, outcome: AttachOutcome | None = None, exc: BaseException | None = None) -> None:
        self.outcome, self.exc = outcome, exc
        self.calls: list[tuple[tuple, dict]] = []

    async def attach(self, *args, **kwargs) -> AttachOutcome:
        self.calls.append((args, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.outcome


def _ok(answers: dict[int, dict]) -> AttachOutcome:
    return AttachOutcome("ok", answers=answers, latency_s=0.02)


def _agree() -> AttachOutcome:
    return _ok({0: {"relation": "occurrence", "expression": "last month"}})


async def _apply(resolver, edges, attacher=None, text=TURN):
    receipt = policy.begin_extraction_receipt("ep-1", text, turn_role="user")
    episode = SimpleNamespace(uuid="g-1", valid_at=SPEECH)
    try:
        await policy._apply_anchored_time(resolver, receipt, episode, edges, attacher=attacher)
        return receipt.anchored_time, receipt, episode
    finally:
        policy.clear_extraction_receipt()


def _valid_ats(edges):
    return [e.valid_at for e in edges]


# --- 1. policy overlay ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agreeing_answer_writes_exactly_like_no_attacher() -> None:
    plain = _edges()
    await _apply(_FakeResolver(_writing_items()), plain)
    assert plain[0].valid_at == MID_JAN  # control: the resolver alone writes

    edges = _edges()
    attacher = _FakeAttacher(_agree())
    report, _, _ = await _apply(_FakeResolver(_writing_items()), edges, attacher)
    assert _valid_ats(edges) == _valid_ats(plain)
    assert edges[0].valid_at == MID_JAN
    assert report.status == "ok" and report.reason == ""
    assert (report.attach_status, report.attach_candidates) == ("ok", 1)
    assert (report.attach_rejected, report.attach_unverified) == (0, 0)
    assert report.attach_model == "m2" and report.attach_prompt_version == "abcd1234"
    assert [r.reason for r in report.results] == ["written", "undated"]
    assert report.overridden == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    {"relation": "occurrence", "expression": "Denver"},   # a different phrase
    {"relation": "start", "expression": "last month"},    # right phrase, wrong relation for a point event
    {"relation": "none", "expression": None},
])
async def test_disagreeing_answer_keeps_graphiti_value(answer) -> None:
    edges = _edges()
    report, _, _ = await _apply(_FakeResolver(_writing_items()), edges, _FakeAttacher(_ok({0: answer})))
    assert edges[0].valid_at == SPEECH
    assert report.results[0].reason == "attach_rejected"
    assert report.results[0].written is False
    assert report.attach_rejected == 1 and report.attach_unverified == 0
    assert report.status == "ok" and report.attach_status == "ok"
    assert report.overridden == 0


@pytest.mark.asyncio
async def test_state_answer_needs_the_start_relation() -> None:
    """V2 by kind: a state needs "start"; the same phrase with "occurrence" is rejected."""
    items = _writing_items()
    # A duration running to now marks the state's start, so the state-start guard lets it through.
    items[0] = {**items[0], "kind": "state", "expression": "for about a month", "calendar": None,
                "offset": {"amount": 1, "unit": "month", "direction": "before", "approx": True}}
    turn = "I have lived in Denver for about a month and I still love hiking."
    ok_edges, bad_edges = _edges(), _edges()
    ok, _, _ = await _apply(_FakeResolver(items), ok_edges,
                            _FakeAttacher(_ok({0: {"relation": "start", "expression": "for about a month"}})), turn)
    bad, _, _ = await _apply(_FakeResolver(items), bad_edges,
                             _FakeAttacher(_ok({0: {"relation": "occurrence", "expression": "a month"}})), turn)
    assert ok.results[0].reason == "written" and ok.results[0].guard is None
    assert ok_edges[0].valid_at == ok.results[0].new_valid_at != SPEECH
    assert bad.results[0].reason == "attach_rejected" and bad_edges[0].valid_at == SPEECH


@pytest.mark.asyncio
async def test_failed_status_ignores_any_answers_it_carries() -> None:
    """V3 is decided by the status alone: a non-ok outcome never writes, even with agreeing answers."""
    edges = _edges()
    agreeing = {0: {"relation": "occurrence", "expression": "last month"}}
    report, _, _ = await _apply(_FakeResolver(_writing_items()), edges,
                                _FakeAttacher(AttachOutcome("timeout", answers=agreeing)))
    assert [e.valid_at for e in edges] == [SPEECH, SPEECH]
    assert report.results[0].reason == "attach_unverified"


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "error_class"), [
    ("error", "RuntimeError"), ("timeout", ""), ("budget", "LlmUsageControlSignal"),
    ("empty", ""), ("parse_error", ""),
])
async def test_failed_attach_call_writes_nothing(status, error_class) -> None:
    edges = _edges()
    attacher = _FakeAttacher(AttachOutcome(status, answers=None, error_class=error_class))
    report, _, _ = await _apply(_FakeResolver(_writing_items()), edges, attacher)
    assert [e.valid_at for e in edges] == [SPEECH, SPEECH]
    assert report.status == "ok"
    assert report.attach_status == status
    assert report.attach_candidates == 1 and report.attach_unverified == 1
    assert report.attach_rejected == 0
    assert report.results[0].reason == "attach_unverified"
    assert all(not r.written for r in report.results)


@pytest.mark.asyncio
@pytest.mark.parametrize("answers", [{}, {1: {"relation": "none", "expression": None}}])
async def test_missing_answer_for_the_fact_is_unverified(answers) -> None:
    edges = _edges()
    report, _, _ = await _apply(_FakeResolver(_writing_items()), edges, _FakeAttacher(_ok(answers)))
    assert edges[0].valid_at == SPEECH
    assert report.results[0].reason == "attach_unverified"
    assert report.attach_unverified == 1 and report.attach_rejected == 0
    assert report.attach_status == "ok" and report.status == "ok"


@pytest.mark.asyncio
async def test_no_candidates_means_no_attach_call() -> None:
    edges = _edges()
    attacher = _FakeAttacher(_agree())
    report, _, _ = await _apply(_FakeResolver(_nothing_items()), edges, attacher)
    assert attacher.calls == []
    assert report.attach_status == "no_candidates" and report.attach_candidates == 0
    assert report.attach_model == "m2" and report.attach_prompt_version == "abcd1234"
    assert report.status == "ok" and _valid_ats(edges) == [SPEECH, SPEECH]

    plan_items = _writing_items()
    plan_items[0] = {**plan_items[0], "kind": "plan"}
    attacher = _FakeAttacher(_agree())
    report, _, _ = await _apply(_FakeResolver(plan_items), _edges(), attacher)
    assert attacher.calls == []
    assert report.attach_status == "no_candidates"
    assert report.results[0].reason == "plan"


@pytest.mark.asyncio
async def test_attacher_exception_is_internal_error_and_writes_nothing() -> None:
    edges = _edges()
    before = _valid_ats(edges)
    attacher = _FakeAttacher(exc=RuntimeError("boom"))
    report, _, _ = await _apply(_FakeResolver(_writing_items()), edges, attacher)
    assert len(attacher.calls) == 1
    assert report.status == "internal_error"
    assert _valid_ats(edges) == before
    assert report.results == ()


@pytest.mark.asyncio
async def test_attach_call_is_blind_and_positional() -> None:
    edges = _edges()
    attacher = _FakeAttacher(_agree())
    _, receipt, episode = await _apply(_FakeResolver(_writing_items()), edges, attacher)
    ((args, kwargs),) = attacher.calls
    assert kwargs == {}
    assert len(args) == 3
    turn, speech_date, facts = args
    assert turn == receipt.episode_text == TURN
    assert speech_date == episode.valid_at.date() == date(2024, 2, 14)
    assert facts == ["The user moved to Denver.", "The user loves hiking."]
    # Nothing the resolver produced is passed: no item dicts, no expression, no kind.
    assert not any(isinstance(a, dict) for a in args)
    assert all(isinstance(f, str) for f in facts)


@pytest.mark.asyncio
async def test_no_attacher_leaves_the_report_and_results_unchanged() -> None:
    edges = _edges()
    report, _, _ = await _apply(_FakeResolver(_writing_items()), edges)
    assert report.attach_status == "" and report.attach_model == "" and report.attach_prompt_version == ""
    assert (report.attach_candidates, report.attach_rejected, report.attach_unverified) == (0, 0, 0)
    assert report.attach_latency_s is None

    inputs = [EdgeTimeInput(uuid=e.uuid, fact=e.fact, valid_at=SPEECH, invalid_at=None) for e in _edges()]
    expected = plan_overlay(inputs, _writing_items(), date(2024, 2, 14), SPEECH.tzinfo)
    assert list(report.results) == expected
    assert [r.reason for r in report.results] == ["written", "undated"]

    again = _edges()
    report_again, _, _ = await _apply(_FakeResolver(_writing_items()), again)
    assert _valid_ats(again) == _valid_ats(edges) and report_again.results == report.results


@pytest.mark.asyncio
async def test_state_window_past_speech_date_has_its_own_reason_only_with_attacher() -> None:
    fact = "The user's new job starts."
    with_attacher = _FakeAttacher(_agree())
    edges = [_edge(fact, "e0")]
    report, _, _ = await _apply(_FakeResolver(_state_future_items()), edges, with_attacher, STATE_TURN)
    assert report.results[0].reason == "state_future"
    assert edges[0].valid_at == SPEECH and with_attacher.calls == []

    plain_edges = [_edge(fact, "e0")]
    plain, _, _ = await _apply(_FakeResolver(_state_future_items()), plain_edges, None, STATE_TURN)
    assert plain.results[0].reason == "state_start"
    assert plain_edges[0].valid_at == SPEECH


# --- 2. hook pass-through ---------------------------------------------------------------------


class _FakeClients:
    llm_client = object()

    def model_copy(self, *, update):
        return self


def _context() -> SimpleNamespace:
    return SimpleNamespace(
        clients=_FakeClients(), episode=SimpleNamespace(uuid="g-1", valid_at=SPEECH), previous_episodes=[],
        entity_types=None, excluded_entity_types=None, edge_type_map={}, edge_types=None,
        custom_extraction_instructions=None,
    )


async def _drive_hook(monkeypatch, hook) -> dict:
    seen: dict = {}

    async def fake_extract(*args, **kwargs):
        seen.update(kwargs)
        return [], [], {}

    monkeypatch.setattr(policy, "_run_graphiti_combined_extraction", fake_extract)
    policy.begin_extraction_receipt("ep-1", TURN, turn_role="user")
    try:
        await hook.extract_single_episode(_context())
    finally:
        policy.clear_extraction_receipt()
    return seen


@pytest.mark.asyncio
async def test_hook_passes_the_attacher_through(monkeypatch) -> None:
    attacher = _FakeAttacher(_agree())
    resolver = _FakeResolver(_writing_items())
    seen = await _drive_hook(monkeypatch, policy.MenhirExtractionHook(
        anchored_time=resolver, anchored_time_attach=attacher))
    assert seen["anchored_time_attach"] is attacher
    assert seen["anchored_time"] is resolver


@pytest.mark.asyncio
async def test_hook_attacher_defaults_to_none(monkeypatch) -> None:
    seen = await _drive_hook(monkeypatch, policy.MenhirExtractionHook(
        anchored_time=_FakeResolver(_writing_items())))
    assert seen["anchored_time_attach"] is None


# --- 3. persisted contract --------------------------------------------------------------------


def _result(uuid: str = "e0") -> AnchoredTimeResult:
    return AnchoredTimeResult(
        edge_uuid=uuid, fact_index=0, expression="a month ago", basis="speech_relative",
        kind="point_event", granularity="month", window_start=date(2023, 4, 1),
        window_end=date(2023, 4, 30), anchor_ref=None, anchor_offset=None, planned_window=None,
        guard=None, written=True, reason="written", graphiti_valid_at=SPEECH,
        new_valid_at=datetime(2023, 4, 15, tzinfo=timezone.utc),
    )


def test_contract_is_at2_with_the_attach_model_and_prompt() -> None:
    rep = AnchoredTimeReport(status="ok", model="gpt-x", results=(_result("e0"), _result("e1")),
                             attach_model="m2", attach_prompt_version="abcd1234")
    rows = contract_rows(rep, ["e0", "e1"], date(2023, 5, 14))
    assert len(rows) == 2
    expected = f"at2|{rep.prompt_version}|gpt-x|abcd1234|m2"
    assert all(r["time_contract"] == expected for r in rows)
    assert all(set(r) == {"uuid", *CONTRACT_PROPERTIES} for r in rows)


def test_contract_stays_at1_without_an_attach_model() -> None:
    rep = AnchoredTimeReport(status="ok", model="gpt-x", results=(_result(),), attach_model="")
    (row,) = contract_rows(rep, ["e0"], date(2023, 5, 14))
    assert row["time_contract"] == f"at1|{rep.prompt_version}|gpt-x"
    assert set(row) == {"uuid", *CONTRACT_PROPERTIES}

    bare = SimpleNamespace(prompt_version="pv", model="gpt-x", results=(_result(),), expiry=False)
    (row,) = contract_rows(bare, ["e0"], date(2023, 5, 14))
    assert row["time_contract"] == "at1|pv|gpt-x"
    assert set(row) == {"uuid", *CONTRACT_PROPERTIES}


# --- 4. render --------------------------------------------------------------------------------

_NEW_OUTCOMES = ("state_future", "attach_rejected", "attach_unverified")


def _render(outcome: str, valid_at: str | None) -> str:
    return EventTime(
        basis="speech_relative", expression="a month ago", kind="point_event", granularity="month",
        window_start="2023-04-01", window_end="2023-04-30", outcome=outcome, speech_date="2023-05-14",
    ).render(valid_at)


@pytest.mark.parametrize("outcome", _NEW_OUTCOMES)
@pytest.mark.parametrize(("valid_at", "literal"), [
    ("2023-04-15T00:00:00Z", "event time unknown (said 2023-05-14)"),  # inside the window: a copy of it
    ("2023-01-02T00:00:00Z", "2023-01-02"),                            # outside it: Graphiti's own date
    ("2023-05-14T15:00:00Z", "event time unknown (said 2023-05-14)"),  # the speech date
])
def test_new_outcomes_render_like_state_start(outcome, valid_at, literal) -> None:
    out = _render(outcome, valid_at)
    assert out == _render("state_start", valid_at)
    assert out == literal
    assert "~2023-04" not in out and "a month ago" not in out


# --- 5. settings and flag ---------------------------------------------------------------------


def _clear_env(monkeypatch) -> None:
    for name in ("MENHIR_ANCHORED_TIME_RESOLVER", "MENHIR_ANCHORED_TIME_MODEL",
                 "MENHIR_ANCHORED_TIME_TIMEOUT_S", "MENHIR_ANCHORED_TIME_ATTACH",
                 "MENHIR_ANCHORED_TIME_ATTACH_MODEL"):
        monkeypatch.delenv(name, raising=False)


def test_attach_settings_default_off(monkeypatch) -> None:
    _clear_env(monkeypatch)
    for settings in (MemorySettings(), MemorySettings.from_env()):
        assert settings.anchored_time_attach_enabled is False
        assert settings.anchored_time_attach_model == ""


def test_attach_settings_read_the_environment(monkeypatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("MENHIR_ANCHORED_TIME_ATTACH", "true")
    monkeypatch.setenv("MENHIR_ANCHORED_TIME_ATTACH_MODEL", " m2 ")
    settings = MemorySettings.from_env()
    assert settings.anchored_time_attach_enabled is True
    assert settings.anchored_time_attach_model == "m2"


def test_attach_feature_flag_registry_entry() -> None:
    flag = FEATURES["anchored_time_attach_enabled"]
    assert flag.env_var == "MENHIR_ANCHORED_TIME_ATTACH"
    assert flag.default is False


# --- 6. client construction -------------------------------------------------------------------


def _captured_hook(monkeypatch, settings: MemorySettings, **kwargs):
    seen: dict = {}

    class _CapturingGraphiti:
        def __init__(self, **graphiti_kwargs) -> None:
            seen.update(graphiti_kwargs)

    monkeypatch.setattr(graphiti_client, "Graphiti", _CapturingGraphiti)
    graphiti_client.GraphitiClient.from_settings_with_capabilities(settings, **kwargs)
    hook = seen["single_episode_extraction_hook"]
    assert isinstance(hook, policy.MenhirExtractionHook)
    return hook


def test_client_builds_the_attacher_only_with_resolver_and_attach_on(monkeypatch) -> None:
    both = MemorySettings(anchored_time_resolver_enabled=True, anchored_time_resolver_model="gpt-6-luna",
                          anchored_time_resolver_timeout_s=7.0, anchored_time_attach_enabled=True,
                          anchored_time_attach_model="m2")
    hook = _captured_hook(monkeypatch, both)
    attacher = hook._anchored_time_attach
    assert isinstance(attacher, AnchoredTimeAttacher)
    assert attacher.model == "m2"
    assert attacher._timeout_s == 7.0
    assert attacher._base_url == ProviderConfig.for_graphiti_llm(both).base_url
    assert isinstance(hook._anchored_time, resolver_module.AnchoredTimeResolver)

    # No attach model: the resolver's model.
    inherit = MemorySettings(anchored_time_resolver_enabled=True, anchored_time_resolver_model="gpt-6-luna",
                             anchored_time_attach_enabled=True)
    assert _captured_hook(monkeypatch, inherit)._anchored_time_attach.model == "gpt-6-luna"
    default_model = MemorySettings(anchored_time_resolver_enabled=True, anchored_time_attach_enabled=True)
    assert (_captured_hook(monkeypatch, default_model)._anchored_time_attach.model
            == ProviderConfig.for_graphiti_llm(default_model).chat_model)

    # Resolver on, attach off.
    resolver_only = MemorySettings(anchored_time_resolver_enabled=True)
    hook = _captured_hook(monkeypatch, resolver_only)
    assert hook._anchored_time is not None and hook._anchored_time_attach is None

    # Resolver off, attach on: the check has nothing to vet.
    attach_only = MemorySettings(anchored_time_attach_enabled=True, anchored_time_attach_model="m2")
    hook = _captured_hook(monkeypatch, attach_only)
    assert hook._anchored_time is None and hook._anchored_time_attach is None
