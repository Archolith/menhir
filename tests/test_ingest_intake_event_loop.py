"""CF-111: `wait_for_episode_processing` must not block the event loop.

The method used to call `graph_adapter.fetch_episode_processing` — a plain synchronous
`def` — directly on the loop. With `timeout_s=60.0` and a default `poll_interval_s=0.1`
that is up to 600 blocking network round trips on the shared event loop for a single
`POST /memory?wait=true` request, starving every other API/MCP/explorer caller.

The load-bearing test proves the loop stays responsive while the wait runs: a heartbeat
task that should tick every ~5ms must advance meaningfully. On the pre-fix source the
blocking calls starve it; with `asyncio.to_thread` it ticks freely. The behavioural
guards pin that the fix changed nothing else about the method's semantics.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from menhir.domain.models import ProcessingState
from menhir.services.ingest_intake import IngestIntakeMixin

_BLOCK_S = 0.05          # how long each stub fetch blocks the calling thread
_POLL_INTERVAL_S = 0.01  # poll_interval passed to wait_for_episode_processing
_TIMEOUT_S = 0.4         # timeout passed to wait_for_episode_processing
_HEARTBEAT_PERIOD_S = 0.005
_HEARTBEAT_MIN_TICKS = 30


class _StubAdapter:
    """A graph adapter whose synchronous fetch genuinely blocks with time.sleep.

    Returning a PENDING row keeps the poll loop going, so the full timeout window is
    exercised. `rows` is popped left-to-right; the final entry repeats forever.
    """

    def __init__(self, rows):
        self._rows = list(rows)
        self.calls = 0

    def fetch_episode_processing(self, episode_uuid: str) -> dict[str, object] | None:
        self.calls += 1
        time.sleep(_BLOCK_S)
        if not self._rows:
            return None
        if len(self._rows) == 1:
            return self._rows[0]
        return self._rows.pop(0)


class _Intake(IngestIntakeMixin):
    def __init__(self, adapter):
        self.graph_adapter = adapter
        self._enrichment_enabled = False


def _pending_row() -> dict[str, object]:
    return {"episode_uuid": "ep-1", "processing_state": ProcessingState.PENDING}


async def _heartbeat() -> int:
    """Ticks ~every 5ms; only runs when the event loop is actually free."""
    count = 0
    while True:
        count += 1
        await asyncio.sleep(_HEARTBEAT_PERIOD_S)
        if count > 10_000:
            raise AssertionError("heartbeat runaway")
        yield count


@contextlib.asynccontextmanager
async def _suppress_cancel():
    try:
        yield
    except asyncio.CancelledError:
        pass


async def test_event_loop_stays_responsive_during_wait():
    """The heartbeat must advance while waiting, proving the loop was never blocked."""
    intake = _Intake(_StubAdapter([_pending_row()]))
    beats = []

    async def tick():
        agen = _heartbeat()
        while True:
            beats.append(await agen.__anext__())

    heartbeat_task = asyncio.create_task(tick())
    async with _suppress_cancel():
        await intake.wait_for_episode_processing(
            "ep-1", timeout_s=_TIMEOUT_S, poll_interval_s=_POLL_INTERVAL_S
        )
        heartbeat_task.cancel()
        await heartbeat_task

    ticks = beats[-1] if beats else 0
    assert ticks >= _HEARTBEAT_MIN_TICKS, (
        f"event loop starved during wait: heartbeat advanced only {ticks} ticks "
        f"(need >= {_HEARTBEAT_MIN_TICKS}); blocking fetch calls are on the loop"
    )


async def test_returns_row_when_ready():
    intake = _Intake(_StubAdapter([{"processing_state": ProcessingState.READY}]))
    row = await intake.wait_for_episode_processing("ep-1", timeout_s=_TIMEOUT_S)
    assert row == {"processing_state": ProcessingState.READY}


async def test_returns_row_when_failed():
    intake = _Intake(_StubAdapter([{"processing_state": ProcessingState.FAILED}]))
    row = await intake.wait_for_episode_processing("ep-1", timeout_s=_TIMEOUT_S)
    assert row == {"processing_state": ProcessingState.FAILED}


async def test_returns_none_when_adapter_returns_none():
    intake = _Intake(_StubAdapter([None]))
    row = await intake.wait_for_episode_processing("ep-1", timeout_s=_TIMEOUT_S)
    assert row is None


async def test_returns_final_fetch_when_deadline_expires():
    """PENDING forever -> keep polling to the deadline, then return the last fetch."""
    pending = _pending_row()
    adapter = _StubAdapter([pending])
    intake = _Intake(adapter)
    row = await intake.wait_for_episode_processing("ep-1", timeout_s=_TIMEOUT_S)
    assert row is pending
    # Polled at least once inside the loop plus one final fetch after the deadline.
    assert adapter.calls >= 2
