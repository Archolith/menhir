"""Unit tests for the human-edge invalidation guard (#168 P1) and its client wiring."""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from graphiti_core.edge_invalidation import EdgeInvalidationDecision, EdgeInvalidationReason

from menhir.infrastructure import graphiti_edge_invalidation as guard
from menhir.infrastructure.graphiti_client import GraphitiClient
from menhir.infrastructure.graphiti_edge_invalidation import (
    KEPT_CONFLICT_CYPHER,
    InvalidationGuardState,
    KeptEdge,
    MenhirEdgeInvalidationHook,
    invalidation_guard_state,
    invalidation_hook_kwargs,
    kept_conflict_rows,
    record_kept_conflicts,
)

INVALIDATE = EdgeInvalidationDecision.INVALIDATE
KEEP = EdgeInvalidationDecision.KEEP


class FakeDriver:
    def __init__(self, sources: dict[str, str | None] | None = None, *, yield_once: bool = False):
        self.sources = sources or {}
        self.calls: list[str] = []
        self.yield_once = yield_once

    async def execute_query(self, cypher: str, **kw):
        self.calls.append(kw["uuid"])
        if self.yield_once:
            await asyncio.sleep(0)
        if kw["uuid"] not in self.sources:
            return SimpleNamespace(records=[])
        return SimpleNamespace(records=[{"source": self.sources[kw["uuid"]]}])


def edge(uuid: str, src: str = "s", dst: str = "o", fact: str = "", group_id: str = ""):
    return SimpleNamespace(
        uuid=uuid, source_node_uuid=src, target_node_uuid=dst, fact=fact, group_id=group_id
    )


def ctx(
    target,
    cause,
    reason=EdgeInvalidationReason.CONTRADICTION,
    target_is_new=False,
):
    return SimpleNamespace(
        target_edge=target, cause_edge=cause, reason=reason, target_is_new=target_is_new
    )


async def decide(hook, context, state):
    token = invalidation_guard_state.set(state)
    try:
        return await hook.decide_edge_invalidation(context)
    finally:
        invalidation_guard_state.reset(token)


def new_state(source: str) -> InvalidationGuardState:
    return InvalidationGuardState(owner="o", source=source)


@pytest.mark.unit
async def test_human_target_agent_cause_contradiction_kept_and_recorded():
    driver = FakeDriver({"T": "user"})
    state = new_state("claude-code")
    result = await decide(
        MenhirEdgeInvalidationHook(driver), ctx(edge("T", "ts", "to"), edge("C", "cs", "co")), state
    )
    assert result is KEEP
    assert len(state.kept) == 1
    k = state.kept[0]
    assert k.kept_edge_uuid == "T"
    assert (k.cause_edge_uuid, k.cause_source) == ("C", "claude-code")
    assert k.reason == "contradiction"


@pytest.mark.unit
async def test_agent_target_human_correction_invalidates():
    driver = FakeDriver({"T": "claude-code"})
    result = await decide(MenhirEdgeInvalidationHook(driver), ctx(edge("T"), edge("C")), new_state("user"))
    assert result is INVALIDATE


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stored", "current"),
    [("user", "user"), ("manual", "manual"), (" User ", "MANUAL"), ("manual", " user ")],
)
async def test_human_over_human_invalidates(stored, current):
    state = new_state(current)
    result = await decide(
        MenhirEdgeInvalidationHook(FakeDriver({"T": stored})), ctx(edge("T"), edge("C")), state
    )
    assert result is INVALIDATE
    assert state.kept == []


@pytest.mark.unit
async def test_stored_human_source_is_normalised_for_protection():
    result = await decide(
        MenhirEdgeInvalidationHook(FakeDriver({"T": " User "})),
        ctx(edge("T"), edge("C")),
        new_state("claude-code"),
    )
    assert result is KEEP


@pytest.mark.unit
async def test_agent_over_agent_invalidates():
    result = await decide(
        MenhirEdgeInvalidationHook(FakeDriver({"T": "claude-code"})),
        ctx(edge("T"), edge("C")),
        new_state("document-ingest"),
    )
    assert result is INVALIDATE


@pytest.mark.unit
async def test_new_target_invalidates_without_driver_calls():
    driver = FakeDriver({"T": "user"})
    result = await decide(
        MenhirEdgeInvalidationHook(driver),
        ctx(edge("T"), edge("C"), target_is_new=True),
        new_state("claude-code"),
    )
    assert result is INVALIDATE
    assert driver.calls == []


@pytest.mark.unit
async def test_driver_error_fails_closed():
    class Boom:
        async def execute_query(self, *a, **k):
            raise ConnectionError("down")

    result = await decide(
        MenhirEdgeInvalidationHook(Boom()), ctx(edge("T"), edge("C")), new_state("claude-code")
    )
    assert result is KEEP


@pytest.mark.unit
async def test_driver_timeout_fails_closed(monkeypatch):
    monkeypatch.setattr(guard, "_SOURCE_LOOKUP_TIMEOUT_S", 0.01)

    class Slow:
        async def execute_query(self, *a, **k):
            await asyncio.sleep(5)

    result = await decide(
        MenhirEdgeInvalidationHook(Slow()), ctx(edge("T"), edge("C")), new_state("claude-code")
    )
    assert result is KEEP


@pytest.mark.unit
@pytest.mark.parametrize("sources", [{}, {"T": None}, {"T": ""}])
async def test_target_without_stored_source_invalidates(sources):
    result = await decide(
        MenhirEdgeInvalidationHook(FakeDriver(sources)),
        ctx(edge("T"), edge("C")),
        new_state("claude-code"),
    )
    assert result is INVALIDATE


@pytest.mark.unit
@pytest.mark.parametrize("reason", [EdgeInvalidationReason.NEWER_CANDIDATE, EdgeInvalidationReason.OWN_END])
async def test_non_contradiction_reasons_keep_but_are_not_recorded(reason):
    state = new_state("claude-code")
    result = await decide(
        MenhirEdgeInvalidationHook(FakeDriver({"T": "user"})),
        ctx(edge("T"), edge("C"), reason=reason),
        state,
    )
    assert result is KEEP
    assert state.kept == []


@pytest.mark.unit
async def test_cause_source_comes_from_graph_for_stored_human_cause():
    driver = FakeDriver({"T": "user", "C": "user"})
    result = await decide(
        MenhirEdgeInvalidationHook(driver),
        ctx(edge("T"), edge("C"), reason=EdgeInvalidationReason.NEWER_CANDIDATE),
        new_state("claude-code"),
    )
    assert result is INVALIDATE
    assert "C" in driver.calls


@pytest.mark.unit
async def test_unset_state_treats_source_as_non_human():
    result = await decide(
        MenhirEdgeInvalidationHook(FakeDriver({"T": "user"})), ctx(edge("T"), edge("C")), None
    )
    assert result is KEEP


@pytest.mark.unit
async def test_concurrent_calls_do_not_share_state():
    driver = FakeDriver({"T": "user"}, yield_once=True)
    hook = MenhirEdgeInvalidationHook(driver)
    s_user, s_agent = new_state("user"), new_state("claude-code")

    async def run(state):
        invalidation_guard_state.set(state)
        await asyncio.sleep(0)
        return await hook.decide_edge_invalidation(ctx(edge("T", "ts", "to"), edge("C", "cs", "co")))

    r_user, r_agent = await asyncio.gather(run(s_user), run(s_agent))
    assert r_user is INVALIDATE
    assert r_agent is KEEP
    assert s_user.kept == []
    assert len(s_agent.kept) == 1 and s_agent.kept[0].cause_source == "claude-code"
    assert invalidation_guard_state.get() is None


@pytest.mark.unit
async def test_source_lookup_is_cached_per_state():
    driver = FakeDriver({"T": "user"})
    hook = MenhirEdgeInvalidationHook(driver)
    state = new_state("claude-code")
    await decide(hook, ctx(edge("T"), edge("C1")), state)
    await decide(hook, ctx(edge("T"), edge("C2")), state)
    assert driver.calls.count("T") == 1


def _kept(kept="K", cause="C", group="g1", source="claude-code") -> KeptEdge:
    return KeptEdge(
        reason="contradiction",
        kept_edge_uuid=kept,
        kept_group_id=group,
        kept_fact=f"fact of {kept}",
        cause_edge_uuid=cause,
        cause_fact=f"fact of {cause}",
        cause_source=source,
    )


@pytest.mark.unit
def test_kept_conflict_rows_dedupes_same_pair():
    rows = kept_conflict_rows([_kept("K", "C"), _kept("K", "C"), _kept("K", "C")])
    assert len(rows) == 1


@pytest.mark.unit
def test_kept_conflict_rows_uuid_is_deterministic_across_calls():
    first = kept_conflict_rows([_kept("K", "C")])[0]["uuid"]
    second = kept_conflict_rows([_kept("X", "Y"), _kept("K", "C")])
    assert first in {r["uuid"] for r in second}
    assert kept_conflict_rows([_kept("K", "C")])[0]["uuid"] == first


@pytest.mark.unit
def test_kept_conflict_rows_distinct_causes_and_kepts_get_distinct_rows():
    rows = kept_conflict_rows([_kept("K", "C1"), _kept("K", "C2"), _kept("K2", "C1")])
    assert len(rows) == 3
    assert len({r["uuid"] for r in rows}) == 3
    # The pair is ordered: (K, C) and (C, K) are different records.
    ab = kept_conflict_rows([_kept("A", "B")])[0]["uuid"]
    ba = kept_conflict_rows([_kept("B", "A")])[0]["uuid"]
    assert ab != ba


@pytest.mark.unit
def test_kept_conflict_rows_maps_fields_including_group_id():
    k = KeptEdge(
        reason="contradiction", kept_edge_uuid="KE", kept_group_id="ns-9", kept_fact="lives in Paris",
        cause_edge_uuid="CE", cause_fact="lives in Rome", cause_source="agent_inference",
    )
    (row,) = kept_conflict_rows([k])
    assert {key: row[key] for key in row if key != "uuid"} == {
        "kept_edge_uuid": "KE",
        "cause_edge_uuid": "CE",
        "group_id": "ns-9",
        "reason": "contradiction",
        "cause_source": "agent_inference",
        "kept_fact": "lives in Paris",
        "cause_fact": "lives in Rome",
    }
    assert row["uuid"]


@pytest.mark.unit
def test_kept_conflict_rows_empty():
    assert kept_conflict_rows([]) == []


class RecordingDriver:
    def __init__(self, *, raises: Exception | None = None, hang: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.raises = raises
        self.hang = hang

    async def execute_query(self, cypher: str, **kw):
        self.calls.append((cypher, kw))
        if self.hang:
            await asyncio.sleep(5)
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(records=[])


@pytest.mark.unit
async def test_record_kept_conflicts_empty_makes_no_call():
    driver = RecordingDriver()
    assert await record_kept_conflicts(driver, []) == 0
    assert driver.calls == []


@pytest.mark.unit
async def test_record_kept_conflicts_sends_one_query_with_deduped_rows():
    driver = RecordingDriver()
    kept = [_kept("K", "C1"), _kept("K", "C1"), _kept("K", "C2")]
    assert await record_kept_conflicts(driver, kept) == 2
    assert len(driver.calls) == 1
    cypher, kw = driver.calls[0]
    assert cypher == KEPT_CONFLICT_CYPHER
    assert kw["rows"] == kept_conflict_rows(kept)
    assert len(kw["rows"]) == 2


@pytest.mark.unit
async def test_record_kept_conflicts_times_out_when_driver_hangs(monkeypatch):
    monkeypatch.setattr(guard, "_RECORD_TIMEOUT_S", 0.01)
    with pytest.raises(asyncio.TimeoutError):
        await record_kept_conflicts(RecordingDriver(hang=True), [_kept()])


@pytest.mark.unit
def test_invalidation_hook_kwargs_requires_seam():
    class Old:
        def __init__(self, uri):
            pass

    class New:
        def __init__(self, uri, edge_invalidation_hook=None):
            pass

    with pytest.raises(RuntimeError, match="edge_invalidation_hook"):
        invalidation_hook_kwargs(Old, object())
    out = invalidation_hook_kwargs(New, object())
    assert isinstance(out["edge_invalidation_hook"], MenhirEdgeInvalidationHook)


# ---- GraphitiClient wiring ----

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_client(driver):
    c = object.__new__(GraphitiClient)
    c.client = SimpleNamespace(driver=driver)
    return c


def conflict_writes(driver: RecordingDriver) -> list[dict]:
    return [kw for cypher, kw in driver.calls if cypher == KEPT_CONFLICT_CYPHER]


async def call_add(c):
    return await c.add_episode(
        name="n", episode_body="b", source_description="claude-code", reference_time=NOW
    )


@pytest.mark.unit
async def test_add_episode_binds_and_resets_state_and_records_kept_conflicts():
    write_driver = RecordingDriver()
    seen: dict = {}
    c = make_client(write_driver)
    hook = MenhirEdgeInvalidationHook(FakeDriver({"T": "user"}))

    async def owned(receipt, owner, **kw):
        seen["state"] = invalidation_guard_state.get()
        assert conflict_writes(write_driver) == []  # recorded only after success

        # Like the real _await_add_episode_request: the hook runs in a child task.
        async def in_task():
            return await hook.decide_edge_invalidation(
                ctx(edge("T", fact="Paris", group_id="ns"), edge("C", fact="Rome"))
            )

        assert await asyncio.create_task(in_task()) is KEEP
        assert conflict_writes(write_driver) == []
        return "RESULT"

    c._add_episode_owned = owned
    assert invalidation_guard_state.get() is None
    assert await call_add(c) == "RESULT"
    assert seen["state"].source == "claude-code"
    assert invalidation_guard_state.get() is None
    writes = conflict_writes(write_driver)
    assert len(writes) == 1
    (row,) = writes[0]["rows"]
    assert (row["kept_edge_uuid"], row["cause_edge_uuid"]) == ("T", "C")
    assert (row["group_id"], row["kept_fact"], row["cause_source"]) == ("ns", "Paris", "claude-code")


@pytest.mark.unit
async def test_no_kept_edges_means_no_conflict_write():
    write_driver = RecordingDriver()
    c = make_client(write_driver)

    async def owned(receipt, owner, **kw):
        return "RESULT"

    c._add_episode_owned = owned
    assert await call_add(c) == "RESULT"
    assert write_driver.calls == []


@pytest.mark.unit
async def test_failed_episode_does_not_record_conflicts_and_resets_state():
    write_driver = RecordingDriver()
    c = make_client(write_driver)
    hook = MenhirEdgeInvalidationHook(FakeDriver({"T": "user"}))

    async def owned(receipt, owner, **kw):
        await hook.decide_edge_invalidation(ctx(edge("T"), edge("C")))
        raise ValueError("extraction failed")

    c._add_episode_owned = owned
    with pytest.raises(ValueError):
        await call_add(c)
    assert write_driver.calls == []
    assert invalidation_guard_state.get() is None


@pytest.mark.unit
async def test_raising_conflict_driver_does_not_change_result():
    write_driver = RecordingDriver(raises=RuntimeError("neo4j down"))
    c = make_client(write_driver)
    hook = MenhirEdgeInvalidationHook(FakeDriver({"T": "user"}))

    async def owned(receipt, owner, **kw):
        await hook.decide_edge_invalidation(ctx(edge("T"), edge("C")))
        return "RESULT"

    c._add_episode_owned = owned
    assert await call_add(c) == "RESULT"
    assert len(conflict_writes(write_driver)) == 1  # attempted, failure swallowed
    assert invalidation_guard_state.get() is None


# ---- T1: constructor wiring ----


@pytest.mark.unit
def test_from_settings_passes_guard_hook_and_preserved_source(monkeypatch):
    import menhir.infrastructure.graphiti_client as graphiti_client
    from menhir.config.settings_model import MemorySettings

    seen: dict = {}

    class _CapturingGraphiti:
        def __init__(self, uri=None, **kwargs) -> None:
            seen.update(kwargs, uri=uri)

    monkeypatch.setattr(graphiti_client, "Graphiti", _CapturingGraphiti)
    graphiti_client.GraphitiClient.from_settings_with_capabilities(MemorySettings())
    assert isinstance(seen["edge_invalidation_hook"], MenhirEdgeInvalidationHook)
    assert "source" in seen["preserved_edge_attributes"]


@pytest.mark.unit
def test_installed_graphiti_has_the_explicit_seam():
    from graphiti_core import Graphiti

    params = inspect.signature(Graphiti.__init__).parameters
    assert "edge_invalidation_hook" in params
    assert not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    out = invalidation_hook_kwargs(Graphiti, object())
    assert isinstance(out["edge_invalidation_hook"], MenhirEdgeInvalidationHook)


@pytest.mark.unit
def test_class_with_neither_param_nor_kwargs_is_refused():
    class Neither:
        def __init__(self, uri, other=None):
            pass

    with pytest.raises(RuntimeError, match="edge_invalidation_hook"):
        invalidation_hook_kwargs(Neither, object())
