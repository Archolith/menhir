"""Anchored-time resolver inside the extraction hook, with a fake LLM (no network).

Covers: edges written through the real hook, the receipt report, every failure status of plan
section 8, the gates, the cache, cancellation, and the downstream contradiction branch.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("graphiti_core")

from graphiti_core.edges import EntityEdge  # noqa: E402
from graphiti_core.extraction_routing import SingleEpisodeExtractionResult  # noqa: E402
from graphiti_core.utils.maintenance.edge_operations import resolve_edge_contradictions  # noqa: E402

import menhir.infrastructure.graphiti_extraction_policy as policy  # noqa: E402
from menhir.infrastructure.anchored_time_resolver import AnchoredTimeResolver  # noqa: E402
from menhir.infrastructure.observability import LlmUsageControlSignal  # noqa: E402

pytestmark = pytest.mark.unit

SPEECH = datetime(2024, 2, 14, 18, 30, tzinfo=timezone.utc)
MID_JAN = datetime(2024, 1, 16, tzinfo=timezone.utc)
TURN = "user: I moved to Denver last month and I still love hiking."
LAST_MONTH_OUTPUT = json.dumps({
    "facts": [
        {"i": 0, "expression": "last month", "basis": "speech_relative", "kind": "point_event",
         "offset": None, "calendar": {"which": "last", "unit": "month"}, "date": None, "anchor": None},
        {"i": 1, "expression": None, "basis": "none", "kind": "state",
         "offset": None, "calendar": None, "date": None, "anchor": None},
    ],
    "missing_events": [],
})


def _edge(fact: str, uuid: str, valid_at=SPEECH, invalid_at=None) -> EntityEdge:
    return EntityEdge(
        uuid=uuid, group_id="ns", source_node_uuid="n-user", target_node_uuid=f"n-{uuid}",
        created_at=datetime(2024, 2, 14, 18, 31, tzinfo=timezone.utc), name="RELATES_TO", fact=fact,
        episodes=["ep-1"], valid_at=valid_at, invalid_at=invalid_at,
        reference_time=SPEECH, attributes={"note": "kept"},
    )


def _edges() -> list[EntityEdge]:
    return [_edge("The user moved to Denver.", "e0"), _edge("The user loves hiking.", "e1")]


class _FakeCompletions:
    def __init__(self, outcomes) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return await outcome()
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))])


def _resolver(*outcomes, timeout_s: float = 5.0):
    completions = _FakeCompletions(outcomes or [LAST_MONTH_OUTPUT])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    resolver = AnchoredTimeResolver(
        client, base_url="https://api.openai.com/v1", model="gpt-6-luna", timeout_s=timeout_s
    )
    return resolver, completions


async def _apply(resolver, edges, text=TURN, valid_at=SPEECH):
    receipt = policy.begin_extraction_receipt("ep-1", text)
    try:
        await policy._apply_anchored_time(resolver, receipt, SimpleNamespace(uuid="g-1", valid_at=valid_at), edges)
        return receipt.anchored_time
    finally:
        policy.clear_extraction_receipt()


def _dumps(edges):
    return [e.model_dump() for e in edges]


class _FakeClients:
    def __init__(self) -> None:
        self.llm_client = object()

    def model_copy(self, *, update: dict) -> "_FakeClients":
        copy = _FakeClients()
        copy.llm_client = update["llm_client"]
        return copy


def _context(episode) -> SimpleNamespace:
    return SimpleNamespace(
        clients=_FakeClients(), episode=episode, previous_episodes=[], entity_types=None,
        excluded_entity_types=None, edge_type_map={}, edge_types=None, custom_extraction_instructions=None,
    )


@pytest.mark.asyncio
async def test_hook_overlays_valid_at_and_records_the_report(monkeypatch) -> None:
    edges = _edges()
    before = _dumps(edges)
    node = SimpleNamespace(name="user", uuid="n-user")

    async def fake_extract(clients, episode, previous_episodes, **kwargs):
        return [node], edges, {"n-user": [0]}

    monkeypatch.setattr(policy, "extract_nodes_and_edges", fake_extract)
    resolver, completions = _resolver()
    receipt = policy.begin_extraction_receipt("ep-1", TURN)
    try:
        hook = policy.MenhirExtractionHook(anchored_time=resolver)
        result = await hook.extract_single_episode(_context(SimpleNamespace(uuid="g-1", valid_at=SPEECH)))
    finally:
        policy.clear_extraction_receipt()

    assert isinstance(result, SingleEpisodeExtractionResult)
    assert result.edges is edges
    assert edges[0].valid_at == MID_JAN
    assert edges[1].valid_at == SPEECH
    after = _dumps(edges)
    for old, new in zip(before, after):
        old.pop("valid_at"), new.pop("valid_at")
        assert old == new  # invalid_at, expired_at, attributes, fact, episodes untouched
    report = receipt.anchored_time
    assert (report.status, report.facts, report.overridden, report.cached) == ("ok", 2, 1, False)
    assert [r.reason for r in report.results] == ["written", "undated"]
    assert report.results[0].edge_uuid == "e0" and report.results[0].graphiti_valid_at == SPEECH
    assert report.model == "gpt-6-luna" and report.prompt_version == "a7797891"
    (call,) = completions.calls
    assert "TURN: I moved to Denver last month" in call["messages"][1]["content"]  # prefix stripped
    assert "0. The user moved to Denver.\n1. The user loves hiking." in call["messages"][1]["content"]
    assert "SPEECH_TIME: 2024-02-14 (Wednesday)" in call["messages"][1]["content"]


@pytest.mark.asyncio
async def test_inside_window_and_invalid_at_rules_apply_through_the_hook() -> None:
    edges = [_edge("The user moved to Denver.", "e0", valid_at=datetime(2024, 1, 5, tzinfo=timezone.utc)),
             _edge("The user loves hiking.", "e1")]
    resolver, _ = _resolver()
    report = await _apply(resolver, edges)
    assert edges[0].valid_at == datetime(2024, 1, 5, tzinfo=timezone.utc)
    assert report.kept_inside_window == 1 and report.overridden == 0

    inverted = [_edge("The user moved to Denver.", "e0", invalid_at=datetime(2024, 1, 2, tzinfo=timezone.utc)),
                _edge("The user loves hiking.", "e1")]
    report = await _apply(_resolver()[0], inverted)
    assert inverted[0].valid_at == SPEECH
    assert report.results[0].reason == "would_invert_interval"


async def _sleep_forever():
    await asyncio.sleep(30)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "status", "error_class"),
    [
        (_sleep_forever, "timeout", ""),
        (RuntimeError("connection reset"), "error", "RuntimeError"),
        (LlmUsageControlSignal("budget refused"), "budget", "LlmUsageControlSignal"),
        ("", "empty", ""),
        ("   ", "empty", ""),
        ("{not json", "parse_error", ""),
        ("[1, 2, 3]", "parse_error", ""),
    ],
)
async def test_every_failure_keeps_graphiti_times(outcome, status, error_class) -> None:
    edges = _edges()
    before = _dumps(edges)
    resolver, completions = _resolver(outcome, timeout_s=0.05)
    report = await _apply(resolver, edges)
    assert _dumps(edges) == before
    assert report.status == status and report.reason == error_class
    assert report.results == () and report.overridden == 0
    assert len(completions.calls) == 1


@pytest.mark.asyncio
async def test_failures_are_not_cached() -> None:
    resolver, completions = _resolver("{not json", LAST_MONTH_OUTPUT)
    assert (await _apply(resolver, _edges())).status == "parse_error"
    edges = _edges()
    report = await _apply(resolver, edges)
    assert report.status == "ok" and edges[0].valid_at == MID_JAN
    assert len(completions.calls) == 2


@pytest.mark.asyncio
async def test_unexpected_internal_error_keeps_graphiti_times() -> None:
    class _Broken:
        model = "m"

        async def resolve(self, *args):
            raise KeyError("boom")

    edges = _edges()
    before = _dumps(edges)
    report = await _apply(_Broken(), edges)
    assert report.status == "internal_error"
    assert _dumps(edges) == before


@pytest.mark.asyncio
async def test_cancellation_propagates() -> None:
    resolver, _ = _resolver(asyncio.CancelledError())
    edges = _edges()
    with pytest.raises(asyncio.CancelledError):
        await _apply(resolver, edges)
    assert edges[0].valid_at == SPEECH


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "edges", "valid_at", "reason"),
    [
        (TURN, [], SPEECH, "no_edges"),
        ("assistant: You moved to Denver last month.", None, SPEECH, "not_user_turn"),
        ("I moved to Denver last month.", None, SPEECH, "not_user_turn"),  # unprefixed note
        (TURN, None, None, "no_speech_time"),
        ("user: I moved to Denver and I love hiking.", None, SPEECH, "no_cue"),
        ("user: I moved to Denver last month. " + "x" * 4000, None, SPEECH, "turn_too_long"),
        (TURN, "many", SPEECH, "too_many_edges"),
    ],
)
async def test_gates_skip_without_a_call(text, edges, valid_at, reason) -> None:
    if edges is None:
        edges = _edges()
    elif edges == "many":
        edges = [_edge(f"fact {i}", f"e{i}") for i in range(policy.ANCHORED_TIME_MAX_EDGES + 1)]
    before = _dumps(edges)
    resolver, completions = _resolver()
    report = await _apply(resolver, edges, text=text, valid_at=valid_at)
    assert (report.status, report.reason) == ("skipped", reason)
    assert completions.calls == []
    assert _dumps(edges) == before


@pytest.mark.asyncio
async def test_gate_bounds_are_inclusive() -> None:
    edges = [_edge(f"The user did thing {i}.", f"e{i}") for i in range(policy.ANCHORED_TIME_MAX_EDGES)]
    resolver, completions = _resolver('{"facts": [], "missing_events": []}')
    report = await _apply(resolver, edges)
    assert report.status == "ok" and len(completions.calls) == 1


@pytest.mark.asyncio
async def test_second_identical_episode_hits_the_cache() -> None:
    resolver, completions = _resolver()
    first, second = _edges(), _edges()
    await _apply(resolver, first)
    report = await _apply(resolver, second)
    assert len(completions.calls) == 1
    assert report.cached is True and report.status == "ok"
    assert second[0].valid_at == MID_JAN


@pytest.mark.asyncio
async def test_cache_survives_guard_mutation_and_is_bounded() -> None:
    turn = "user: I read Dune and Emma, and finished Ulysses yesterday."
    facts = ["The user read Dune.", "The user read Emma.", "The user finished reading Ulysses."]
    output = json.dumps({"facts": [
        {"i": i, "expression": "yesterday", "basis": "speech_relative", "kind": "point_event",
         "calendar": {"which": "last", "unit": "day"}} for i in range(3)], "missing_events": []})
    resolver, completions = _resolver(output)
    for _ in range(2):
        edges = [_edge(f, f"e{i}") for i, f in enumerate(facts)]
        report = await _apply(resolver, edges, text=turn)
        assert report.guard_drops == (0, 1)
        assert [e.valid_at for e in edges] == [SPEECH, SPEECH, datetime(2024, 2, 13, tzinfo=timezone.utc)]
    assert len(completions.calls) == 1

    small, small_calls = _resolver()
    small._cache_size = 1
    await _apply(small, _edges())
    await _apply(small, _edges(), text="user: I moved to Denver last month, and I love hiking.")
    await _apply(small, _edges())
    assert len(small_calls.calls) == 3


@pytest.mark.asyncio
async def test_hook_without_resolver_leaves_no_report(monkeypatch) -> None:
    edges = _edges()

    async def fake_extract(clients, episode, previous_episodes, **kwargs):
        return [], edges, {}

    monkeypatch.setattr(policy, "extract_nodes_and_edges", fake_extract)
    receipt = policy.begin_extraction_receipt("ep-1", TURN)
    try:
        await policy.MenhirExtractionHook().extract_single_episode(
            _context(SimpleNamespace(uuid="g-1", valid_at=SPEECH)))
    finally:
        policy.clear_extraction_receipt()
    assert receipt.anchored_time is None
    assert edges[0].valid_at == SPEECH


@pytest.mark.asyncio
async def test_overlay_changes_which_existing_edge_is_invalidated() -> None:
    """Plan section 10: an earlier valid_at changes resolve_edge_contradictions. Both branches."""
    def existing():
        return _edge("The user lives in Austin.", "old", valid_at=datetime(2024, 1, 20, tzinfo=timezone.utc))

    # Graphiti's speech-date default: the new edge is later, so the old one is expired at it.
    graphiti_only = _edges()
    expired = resolve_edge_contradictions(graphiti_only[0], [existing()])
    assert [e.uuid for e in expired] == ["old"] and expired[0].invalid_at == SPEECH

    # With the overlay the move is dated mid-January, before the old edge's start: no expiry.
    overlaid = _edges()
    await _apply(_resolver()[0], overlaid)
    assert overlaid[0].valid_at == MID_JAN
    assert resolve_edge_contradictions(overlaid[0], [existing()]) == []
