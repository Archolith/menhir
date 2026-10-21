"""#267: a timed-out add_episode attempt is cancelled and awaited before any retry starts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from menhir.infrastructure.graphiti_client import GraphitiClient
from menhir.services.enrichment_steps import add_episode_with_timeout

pytestmark = pytest.mark.unit

_WHEN = datetime(2024, 2, 14, tzinfo=timezone.utc)


async def _attempt(client: GraphitiClient, attempt: int) -> None:
    await add_episode_with_timeout(
        client,
        name="n",
        episode_body="b",
        source_description="s",
        reference_time=_WHEN,
        episode_uuid="ep-1",
        attempt=attempt,
        timeout_s=0.1,
    )


async def test_timed_out_attempt_is_cancelled_and_finished_before_the_retry_begins() -> None:
    events: list[str] = []

    async def native_add_episode(**kwargs) -> None:
        number = len(events) and 2 or 1
        events.append(f"start{number}")
        if number == 2:
            return
        try:
            await asyncio.sleep(30)  # blocks past the deadline
        except asyncio.CancelledError:
            events.append("cancelled1")
            await asyncio.sleep(0.05)  # cleanup that still awaits: the timeout must wait for it
            raise
        finally:
            events.append("finished1")

    client = GraphitiClient(client=SimpleNamespace(add_episode=native_add_episode))

    with pytest.raises(TimeoutError, match="timed out"):
        await _attempt(client, 1)
    assert events == ["start1", "cancelled1", "finished1"], "attempt 1 was still running at timeout"

    await _attempt(client, 2)  # the retry
    assert events == ["start1", "cancelled1", "finished1", "start2"]
