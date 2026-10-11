"""P3b-3: a concurrent call that replaces the shared receipt's report cannot change this call's
expiry decision or drop its persist.

Call A's extraction hook classifies the Rome trip (expiry on). Call B, sharing the receipt, then
runs its own hook and leaves a skipped report on the receipt. A's edge resolution and persist run
after that, through the real _apply_anchored_time, Graphiti's real resolve_extracted_edge with
MenhirEdgeExpiryHook, and GraphitiClient.add_episode.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("graphiti_core")

from graphiti_core.utils.maintenance.edge_operations import resolve_extracted_edge  # noqa: E402

import menhir.infrastructure.graphiti_extraction_policy as policy  # noqa: E402
from menhir.infrastructure.graphiti_client import GraphitiClient  # noqa: E402
from tests.test_anchored_time_expiry import (  # noqa: E402
    SPEECH,
    TRIP_END,
    _DedupeLLM,
    _edges,
    _episode,
    _other,
    _resolver,
)

pytestmark = pytest.mark.unit


class _Driver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def execute_query(self, query, params=None, **kwargs):
        self.calls.append((query, params))
        return SimpleNamespace(records=[{"written": len(params["rows"])}])


async def _run_two_calls() -> tuple[dict, _Driver, object]:
    a_classified, b_hooked = asyncio.Event(), asyncio.Event()
    native = AsyncMock()
    native.driver = _Driver()
    outcome: dict = {}

    async def add_episode(*, name, **kwargs):
        receipt = policy.get_extraction_receipt()
        if name == "A":
            edges = _edges()
            await policy._apply_anchored_time(
                _resolver(), receipt, SimpleNamespace(uuid="g-a", valid_at=SPEECH), edges,
                expiry=True)
            a_classified.set()
            await b_hooked.wait()  # B replaces the shared receipt's report first
            resolved, invalidated, _ = await resolve_extracted_edge(
                _DedupeLLM([]), edges[0], [], [_other()], _episode(),
                edge_expiry_hook=policy.MenhirEdgeExpiryHook())
            outcome["A"] = resolved
            return SimpleNamespace(episode=SimpleNamespace(uuid="ep-A"), edges=[resolved])
        await a_classified.wait()
        await policy._apply_anchored_time(
            _resolver(), receipt, SimpleNamespace(uuid="g-b", valid_at=SPEECH), [], expiry=True)
        b_hooked.set()
        return SimpleNamespace(episode=SimpleNamespace(uuid="ep-B"), edges=[])

    native.add_episode = add_episode
    client = GraphitiClient(client=native)
    receipt = policy.begin_extraction_receipt("k", "user: I was in Rome from March 27 to April 1")
    try:
        async def call(name: str):
            return await client.add_episode(name=name, episode_body="x", source_description="u",
                                            reference_time=SPEECH, group_id="g")

        await asyncio.wait_for(asyncio.gather(call("A"), call("B")), timeout=10)
    finally:
        policy.clear_extraction_receipt()
    return outcome, native.driver, receipt.anchored_time


@pytest.mark.asyncio
async def test_concurrent_call_cannot_flip_this_calls_expiry_or_drop_its_persist() -> None:
    outcome, driver, left_on_receipt = await _run_two_calls()

    assert left_on_receipt.status == "skipped"  # B did replace the shared report
    rome = outcome["A"]
    assert rome.expired_at is None and rome.invalid_at == TRIP_END
    assert len(driver.calls) == 1
    _query, params = driver.calls[0]
    assert [row["uuid"] for row in params["rows"]] == ["e0"]
    assert params["episode_uuid"] == "ep-A"


@pytest.mark.asyncio
async def test_control_without_the_slot_the_shared_receipt_flips_a_to_expire(monkeypatch) -> None:
    """The counterexample is real: with no per-call slot A reads B's report and expires."""
    no_slot = SimpleNamespace(get=lambda: None, set=lambda value: None, reset=lambda token: None)
    monkeypatch.setattr(policy, "anchored_time_slot", no_slot)

    outcome, driver, _ = await _run_two_calls()

    assert outcome["A"].expired_at is not None
    assert driver.calls == []


@pytest.mark.asyncio
async def test_slot_binding_is_reset_after_the_call() -> None:
    native = AsyncMock()
    native.driver = _Driver()
    seen: list = []

    async def add_episode(**kwargs):
        seen.append(policy.anchored_time_slot.get())
        return SimpleNamespace(episode=SimpleNamespace(uuid="ep"), edges=[])

    native.add_episode = add_episode
    client = GraphitiClient(client=native)
    await client.add_episode(name="t", episode_body="x", source_description="u",
                             reference_time=SPEECH, group_id="g")
    assert seen[0] is not None and seen[0].owner
    assert policy.anchored_time_slot.get() is None
