"""Two concurrent add_episode calls sharing one receipt never persist each other's report (245-2).

Call A's hook writes A's report, then call B's hook overwrites the shared receipt slot with B's
report before A reaches its persist step. Without the owner binding A would write B's contract
onto A's edges.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from menhir.infrastructure import graphiti_extraction_policy as extraction
from menhir.infrastructure.anchored_time import AnchoredTimeReport, AnchoredTimeResult
from menhir.infrastructure.graphiti_client import GraphitiClient

pytestmark = pytest.mark.unit

NOW = datetime(2023, 5, 14, 15, 0, tzinfo=timezone.utc)


def _result(uuid: str, expression: str) -> AnchoredTimeResult:
    return AnchoredTimeResult(
        edge_uuid=uuid, fact_index=0, expression=expression, basis="speech_relative",
        kind="point_event", granularity="month", window_start=date(2023, 4, 1),
        window_end=date(2023, 4, 30), anchor_ref=None, anchor_offset=None, planned_window=None,
        guard=None, written=True, reason="written", graphiti_valid_at=NOW,
        new_valid_at=datetime(2023, 4, 15, tzinfo=timezone.utc),
    )


class RecordingDriver:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def execute_query(self, query, params=None, **kwargs):
        self.calls.append(params)
        return SimpleNamespace(records=[{"written": len(params["rows"])}])


async def test_shared_receipt_concurrent_calls_do_not_cross_write() -> None:
    a_hooked, b_hooked = asyncio.Event(), asyncio.Event()
    native = AsyncMock()
    native.driver = RecordingDriver()

    async def add_episode(*, name, **kwargs):
        rec = extraction.get_extraction_receipt()
        edge = "e1"  # same uuid in both reports, so a cross-call report would match A's edges
        # The hook, run inside this invocation's context.
        rec.anchored_time = AnchoredTimeReport(
            status="ok", model="gpt-x", results=(_result(edge, f"said by {name}"),),
            owner=extraction.anchored_time_owner.get())
        if name == "A":
            a_hooked.set()
            await b_hooked.wait()  # B overwrites the shared slot before A persists
        else:
            await a_hooked.wait()
            b_hooked.set()
        return SimpleNamespace(episode=SimpleNamespace(uuid=f"ep-{name}"),
                               edges=[SimpleNamespace(uuid=edge)])

    native.add_episode = add_episode
    client = GraphitiClient(client=native)
    extraction.begin_extraction_receipt("k", "user: a month ago I moved")
    try:
        async def call(name: str):
            return await client.add_episode(name=name, episode_body="x", source_description="u",
                                            reference_time=NOW, group_id="g")

        await asyncio.wait_for(asyncio.gather(call("A"), call("B")), timeout=10)
    finally:
        extraction.clear_extraction_receipt()

    written = [(p["episode_uuid"], r["uuid"], r["time_expression"])
               for p in native.driver.calls for r in p["rows"]]
    # Whichever report is left on the shared slot, only its own call may persist it (which call
    # hooks last depends on scheduling). The overwritten call loses its report; it never borrows.
    assert len(written) == 1
    (episode, _uuid, expression), = written
    assert expression == f"said by {episode.removeprefix('ep-')}"


async def test_owner_binding_is_reset_after_the_call() -> None:
    native = AsyncMock()
    native.driver = RecordingDriver()
    seen: list[str | None] = []

    async def add_episode(**kwargs):
        seen.append(extraction.anchored_time_owner.get())
        return SimpleNamespace(episode=SimpleNamespace(uuid="ep"), edges=[])

    native.add_episode = add_episode
    client = GraphitiClient(client=native)
    for _ in range(2):
        await client.add_episode(name="t", episode_body="x", source_description="u",
                                 reference_time=NOW, group_id="g")
    assert seen[0] and seen[1] and seen[0] != seen[1]
    assert extraction.anchored_time_owner.get() is None
