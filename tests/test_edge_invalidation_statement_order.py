"""2a statement-order veto on the edge-invalidation hook (#168, plan item 8)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from graphiti_core.edge_invalidation import EdgeInvalidationDecision, EdgeInvalidationReason

from menhir.infrastructure.graphiti_edge_invalidation import (
    InvalidationGuardState,
    MenhirEdgeInvalidationHook,
    invalidation_guard_state,
    invalidation_hook_kwargs,
)

INVALIDATE = EdgeInvalidationDecision.INVALIDATE
KEEP = EdgeInvalidationDecision.KEEP
NEWER = EdgeInvalidationReason.NEWER_CANDIDATE
CONTRA = EdgeInvalidationReason.CONTRADICTION
OWN_END = EdgeInvalidationReason.OWN_END

EARLY = datetime(2023, 3, 1, tzinfo=timezone.utc)   # "I've had it for 3 months"
LATE = datetime(2023, 5, 20, tzinfo=timezone.utc)   # "I switched yesterday"
NS = "lme-x"


class Driver:
    """Serves both lookups: ``r.source`` and ``r.reference_time`` by uuid (and group_id)."""

    def __init__(self, sources=None, said=None, *, said_error: Exception | None = None):
        self.sources = sources or {}
        self.said = said or {}
        self.said_error = said_error
        self.said_calls: list[tuple[str, str]] = []
        self.source_calls: list[str] = []

    async def execute_query(self, cypher: str, **kw):
        if "reference_time" in cypher:
            self.said_calls.append((kw["uuid"], kw["group_id"]))
            if self.said_error is not None:
                raise self.said_error
            key = (kw["uuid"], kw["group_id"])
            return SimpleNamespace(records=[{"said": self.said[key]}] if key in self.said else [])
        self.source_calls.append(kw["uuid"])
        if kw["uuid"] not in self.sources:
            return SimpleNamespace(records=[])
        return SimpleNamespace(records=[{"source": self.sources[kw["uuid"]]}])


def edge(uuid: str):
    return SimpleNamespace(uuid=uuid, group_id=NS, fact=f"fact {uuid}")


def ctx(target, cause, reason, *, target_is_new=False, episode_said=LATE, episode=True):
    ep = SimpleNamespace(valid_at=episode_said) if episode else None
    return SimpleNamespace(
        target_edge=target, cause_edge=cause, reason=reason, target_is_new=target_is_new, episode=ep
    )


async def decide(hook, context, state):
    token = invalidation_guard_state.set(state)
    try:
        return await hook.decide_edge_invalidation(context)
    finally:
        invalidation_guard_state.reset(token)


def state(source: str = "user") -> InvalidationGuardState:
    return InvalidationGuardState(owner="o", source=source)


def hook(driver, on: bool = True) -> MenhirEdgeInvalidationHook:
    return MenhirEdgeInvalidationHook(driver, statement_order=on)


# ---- plan §4 ----


@pytest.mark.unit
async def test_newer_candidate_new_mention_said_later_is_kept():
    # "switched yesterday" (said LATE, this call, earlier valid_at) is the target; the stored
    # "for 3 months" (said EARLY, later valid_at) is the newer candidate. Both user-stated.
    driver = Driver(sources={"S": "user"}, said={("S", NS): EARLY})
    result = await decide(
        hook(driver), ctx(edge("N"), edge("S"), NEWER, target_is_new=True, episode_said=LATE), state()
    )
    assert result is KEEP
    assert driver.said_calls == [("S", NS)]


@pytest.mark.unit
async def test_contradiction_mention_said_earlier_keeps_stored_edge():
    # Out-of-order ingest: this call's mention was said EARLY; the stored target was said LATE.
    driver = Driver(sources={"T": "user"}, said={("T", NS): LATE})
    result = await decide(hook(driver), ctx(edge("T"), edge("M"), CONTRA, episode_said=EARLY), state())
    assert result is KEEP
    assert driver.said_calls == [("T", NS)]


@pytest.mark.unit
@pytest.mark.parametrize("reason", [NEWER, CONTRA])
async def test_cause_said_later_keeps_todays_answer(reason):
    if reason is NEWER:
        driver = Driver(said={("S", NS): LATE})
        context = ctx(edge("N"), edge("S"), NEWER, target_is_new=True, episode_said=EARLY)
    else:
        driver = Driver(said={("T", NS): EARLY})
        context = ctx(edge("T"), edge("M"), CONTRA, episode_said=LATE)
    assert await decide(hook(driver), context, state()) is INVALIDATE


@pytest.mark.unit
async def test_same_said_time_keeps_todays_answer():
    driver = Driver(said={("T", NS): EARLY})
    assert await decide(hook(driver), ctx(edge("T"), edge("M"), CONTRA, episode_said=EARLY), state()) is INVALIDATE


@pytest.mark.unit
@pytest.mark.parametrize(
    "case",
    ["no-episode", "episode-without-time", "stored-missing", "stored-null", "stored-unparseable"],
)
async def test_missing_or_unparseable_time_keeps_todays_answer(case):
    said = {("T", NS): LATE}
    kwargs = {"episode_said": EARLY}
    if case == "no-episode":
        kwargs = {"episode": False}
    elif case == "episode-without-time":
        kwargs = {"episode_said": None}
    elif case == "stored-missing":
        said = {}
    elif case == "stored-null":
        said = {("T", NS): None}
    elif case == "stored-unparseable":
        said = {("T", NS): "not a date"}
    driver = Driver(said=said)
    assert await decide(hook(driver), ctx(edge("T"), edge("M"), CONTRA, **kwargs), state()) is INVALIDATE


@pytest.mark.unit
async def test_said_lookup_error_answers_keep():
    driver = Driver(said_error=RuntimeError("neo4j down"))
    assert await decide(hook(driver), ctx(edge("T"), edge("M"), CONTRA, episode_said=EARLY), state()) is KEEP


@pytest.mark.unit
async def test_agent_cause_said_later_vs_stored_human_edge_still_kept_by_authority():
    driver = Driver(sources={"T": "user"}, said={("T", NS): EARLY})
    st = state("claude-code")
    result = await decide(hook(driver), ctx(edge("T"), edge("M"), CONTRA, episode_said=LATE), st)
    assert result is KEEP
    assert [k.kept_edge_uuid for k in st.kept] == ["T"]  # recorded as before
    assert driver.said_calls == []  # authority answered first


# ---- scope of the veto ----


@pytest.mark.unit
async def test_switch_off_keeps_todays_answer_and_reads_no_times():
    driver = Driver(sources={"S": "user"}, said={("S", NS): EARLY})
    result = await decide(
        hook(driver, on=False), ctx(edge("N"), edge("S"), NEWER, target_is_new=True), state()
    )
    assert result is INVALIDATE
    assert driver.said_calls == []


@pytest.mark.unit
async def test_default_hook_has_the_veto_off():
    driver = Driver(said={("T", NS): LATE})
    result = await decide(
        MenhirEdgeInvalidationHook(driver), ctx(edge("T"), edge("M"), CONTRA, episode_said=EARLY), state()
    )
    assert result is INVALIDATE


@pytest.mark.unit
async def test_own_end_keeps_todays_answer():
    driver = Driver(said={("T", NS): LATE})
    assert await decide(hook(driver), ctx(edge("T"), edge("M"), OWN_END, episode_said=EARLY), state()) is INVALIDATE
    assert driver.said_calls == []


@pytest.mark.unit
async def test_stored_newer_candidate_against_stored_target_uses_both_stored_times():
    # target_is_new=False: the resolved edge is a stored duplicate; both times come from Neo4j.
    driver = Driver(said={("T", NS): LATE, ("S", NS): EARLY})
    assert await decide(hook(driver), ctx(edge("T"), edge("S"), NEWER, episode_said=None), state()) is KEEP
    assert sorted(driver.said_calls) == [("S", NS), ("T", NS)]


@pytest.mark.unit
async def test_said_lookups_are_cached_per_call_and_scoped_by_group():
    driver = Driver(said={("T", NS): LATE})
    st = state()
    for cause in ("M1", "M2"):
        assert await decide(hook(driver), ctx(edge("T"), edge(cause), CONTRA, episode_said=EARLY), st) is KEEP
    assert driver.said_calls == [("T", NS)]
    other = SimpleNamespace(uuid="T", group_id="lme-other", fact="")
    assert await decide(hook(driver), ctx(other, edge("M3"), CONTRA, episode_said=EARLY), st) is INVALIDATE
    assert driver.said_calls == [("T", NS), ("T", "lme-other")]


@pytest.mark.unit
@pytest.mark.parametrize(
    "stored",
    [
        "2023-05-20T00:00:00Z",
        "2023-05-20T00:00:00+00:00",
        datetime(2023, 5, 20),  # naive: read as UTC
        SimpleNamespace(to_native=lambda: datetime(2023, 5, 20, tzinfo=timezone.utc)),  # neo4j DateTime
    ],
)
async def test_stored_time_formats(stored):
    driver = Driver(said={("T", NS): stored})
    assert await decide(hook(driver), ctx(edge("T"), edge("M"), CONTRA, episode_said=EARLY), state()) is KEEP


# ---- wiring ----


@pytest.mark.unit
def test_hook_kwargs_pass_the_switch():
    class G:
        def __init__(self, uri=None, edge_invalidation_hook=None):
            pass

    assert invalidation_hook_kwargs(G, object())["edge_invalidation_hook"]._statement_order is False
    assert invalidation_hook_kwargs(G, object(), statement_order=True)["edge_invalidation_hook"]._statement_order


@pytest.mark.unit
@pytest.mark.parametrize(("env", "expected"), [(None, True), ("false", False)])
def test_from_settings_wires_the_setting_default_on(monkeypatch, env, expected):
    import menhir.infrastructure.graphiti_client as graphiti_client
    from menhir.config.settings_model import MemorySettings

    if env is None:
        monkeypatch.delenv("MENHIR_EDGE_INVALIDATION_STATEMENT_ORDER", raising=False)
    else:
        monkeypatch.setenv("MENHIR_EDGE_INVALIDATION_STATEMENT_ORDER", env)
    seen: dict = {}

    class _CapturingGraphiti:
        def __init__(self, uri=None, **kwargs) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(graphiti_client, "Graphiti", _CapturingGraphiti)
    settings = MemorySettings.from_env()
    assert settings.edge_invalidation_statement_order is expected
    graphiti_client.GraphitiClient.from_settings_with_capabilities(settings)
    assert seen["edge_invalidation_hook"]._statement_order is expected
