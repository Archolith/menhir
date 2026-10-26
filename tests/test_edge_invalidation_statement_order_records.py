"""2a statement-order KEEPs are recorded as EdgeInvalidationConflict rows (kept_by='statement_order')."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from graphiti_core.edge_invalidation import EdgeInvalidationDecision, EdgeInvalidationReason

from menhir.infrastructure.graphiti_edge_invalidation import (
    InvalidationGuardState,
    KeptEdge,
    MenhirEdgeInvalidationHook,
    invalidation_guard_state,
    kept_conflict_rows,
)

KEEP = EdgeInvalidationDecision.KEEP
INVALIDATE = EdgeInvalidationDecision.INVALIDATE
NEWER = EdgeInvalidationReason.NEWER_CANDIDATE
CONTRA = EdgeInvalidationReason.CONTRADICTION
EARLY = datetime(2023, 3, 1, tzinfo=timezone.utc)
LATE = datetime(2023, 5, 20, tzinfo=timezone.utc)
NS = "lme-x"


class Driver:
    def __init__(self, sources=None, said=None):
        self.sources = sources or {}
        self.said = said or {}

    async def execute_query(self, cypher: str, **kw):
        if "reference_time" in cypher:
            key = (kw["uuid"], kw["group_id"])
            return SimpleNamespace(records=[{"said": self.said[key]}] if key in self.said else [])
        if kw["uuid"] not in self.sources:
            return SimpleNamespace(records=[])
        return SimpleNamespace(records=[{"source": self.sources[kw["uuid"]]}])


def edge(uuid: str):
    return SimpleNamespace(uuid=uuid, group_id=NS, fact=f"fact {uuid}")


def ctx(target, cause, reason, *, target_is_new=False, episode_said=LATE):
    return SimpleNamespace(
        target_edge=target, cause_edge=cause, reason=reason, target_is_new=target_is_new,
        episode=SimpleNamespace(valid_at=episode_said),
    )


async def decide(hook, context, state):
    token = invalidation_guard_state.set(state)
    try:
        return await hook.decide_edge_invalidation(context)
    finally:
        invalidation_guard_state.reset(token)


@pytest.mark.unit
async def test_newer_candidate_keep_is_recorded_with_the_stored_cause_source():
    driver = Driver(sources={"S": "user"}, said={("S", NS): EARLY})
    state = InvalidationGuardState(owner="o", source="user")
    hook = MenhirEdgeInvalidationHook(driver, statement_order=True)
    result = await decide(hook, ctx(edge("N"), edge("S"), NEWER, target_is_new=True), state)
    assert result is KEEP
    assert state.kept == [
        KeptEdge(
            reason="newer_candidate", kept_edge_uuid="N", kept_group_id=NS, kept_fact="fact N",
            cause_edge_uuid="S", cause_fact="fact S", cause_source="user", kept_by="statement_order",
        )
    ]


@pytest.mark.unit
async def test_contradiction_keep_is_recorded_with_this_calls_source():
    driver = Driver(sources={"T": "user"}, said={("T", NS): LATE})
    state = InvalidationGuardState(owner="o", source="user")
    hook = MenhirEdgeInvalidationHook(driver, statement_order=True)
    assert await decide(hook, ctx(edge("T"), edge("M"), CONTRA, episode_said=EARLY), state) is KEEP
    (kept,) = state.kept
    assert (kept.reason, kept.kept_edge_uuid, kept.cause_edge_uuid) == ("contradiction", "T", "M")
    assert (kept.cause_source, kept.kept_by) == ("user", "statement_order")


@pytest.mark.unit
@pytest.mark.parametrize("on", [True, False])
async def test_no_record_when_the_veto_does_not_keep(on):
    # Cause said later (on) or the switch off: today's answer, nothing recorded.
    driver = Driver(sources={"T": "user"}, said={("T", NS): EARLY})
    state = InvalidationGuardState(owner="o", source="user")
    hook = MenhirEdgeInvalidationHook(driver, statement_order=on)
    assert await decide(hook, ctx(edge("T"), edge("M"), CONTRA, episode_said=LATE if on else EARLY), state) is INVALIDATE
    assert state.kept == []


@pytest.mark.unit
async def test_authority_keep_is_recorded_once_as_authority():
    driver = Driver(sources={"T": "user"}, said={("T", NS): LATE})
    state = InvalidationGuardState(owner="o", source="claude-code")
    hook = MenhirEdgeInvalidationHook(driver, statement_order=True)
    assert await decide(hook, ctx(edge("T"), edge("M"), CONTRA, episode_said=EARLY), state) is KEEP
    assert [k.kept_by for k in state.kept] == ["authority"]


@pytest.mark.unit
async def test_keep_without_bound_state_still_answers_keep():
    driver = Driver(said={("T", NS): LATE})
    hook = MenhirEdgeInvalidationHook(driver, statement_order=True)
    assert await decide(hook, ctx(edge("T"), edge("M"), CONTRA, episode_said=EARLY), None) is KEEP


@pytest.mark.unit
def test_rows_carry_kept_by_only_for_statement_order():
    base = dict(
        reason="newer_candidate", kept_edge_uuid="K", kept_group_id=NS, kept_fact="k",
        cause_edge_uuid="C", cause_fact="c", cause_source="user",
    )
    (so_row,) = kept_conflict_rows([KeptEdge(**base, kept_by="statement_order")])
    (auth_row,) = kept_conflict_rows([KeptEdge(**base)])
    assert so_row["kept_by"] == "statement_order"
    assert "kept_by" not in auth_row
    assert so_row["uuid"] == auth_row["uuid"]  # one record per ordered pair
