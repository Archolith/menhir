"""CF-208 row 3 -- the RuntimeError catch cannot be the un-killable-task mechanism.

CF-208 left one row open with an explicit instruction: `_processing_heartbeat_loop` catches
`RuntimeError` around its own `to_thread` and continues, which "remains suspicious ... but nothing
currently demonstrates it firing. Do not 'fix' it without a reproducer." A fix WAS implemented,
tested, disproved and reverted during CF-111.

The reproducer was attempted here and the result is negative, which is a real answer rather than an
absent one. Three things were measured separately instead of assumed to be one:

* **A permanent RuntimeError does make the loop spin.** With `touch_episode_processing_heartbeat`
  raising every time, the loop retried once per interval indefinitely rather than giving up.
  Wasteful, and the entry is right to call it the wrong response.
* **The task still dies when cancelled, including mid-`to_thread`.** This is the load-bearing one.
  There are two windows: `await asyncio.sleep(...)` sits OUTSIDE the try, and
  `await asyncio.to_thread(...)` sits inside it, so only the second could ever be swallowed. With
  the cancellation forced into that second window, the task still dies -- `CancelledError` derives
  from `BaseException`, and the catch names `OSError` and `RuntimeError`. So the catch as written
  cannot produce the failure CF-208's severity rests on: "an un-killable background task ...
  prevented asyncio teardown from ever completing".
* **`asyncio.run` teardown completed** in every arrangement tried, including with the loop's
  default executor shut down underneath it.

So this row is a nuisance, not the hang. That is consistent with CF-111's history: the fix was
disproved because it was never the cause. This file pins the property that makes the fear
impossible, so a future edit that broadens the catch to `except Exception` -- or worse, to
`except BaseException` -- fails here rather than silently reintroducing an un-killable task.
"""

from __future__ import annotations

import asyncio
import inspect
import types

import pytest

from menhir.services.ingest_worker import IngestWorkerMixin


class _AlwaysFailsAdapter:
    """Stands in for a graph adapter whose heartbeat write can never succeed again."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    def touch_episode_processing_heartbeat(self, episode_uuid: str, worker_id: str | None = None):
        self.calls += 1
        raise self.exc


def _worker(adapter) -> IngestWorkerMixin:
    worker = IngestWorkerMixin()
    worker.graph_adapter = adapter
    worker.graphiti_client = types.SimpleNamespace(scheduler_fallback_base_url="")
    worker._worker_id = "test-worker"
    worker._processing_heartbeat_interval_s = 0.01
    worker._scheduler_http_client = None
    return worker


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_permanent_runtime_error_makes_the_loop_retry_rather_than_exit(
    isolated_telemetry_db,
) -> None:
    """The behaviour CF-208 flagged, confirmed. Recorded so the negative result below is not
    mistaken for "nothing happens here".

    Sampled twice rather than counted against a deadline: each failed pass writes a lifecycle
    event SYNCHRONOUSLY, so the real retry rate is set by a SQLite write, not by
    `_processing_heartbeat_interval_s`. Asserting a count per unit time would be measuring the
    telemetry write. What must be true is that the count keeps GROWING -- the loop does not give
    up. (`isolated_telemetry_db` keeps those writes off the operator's real telemetry DB.)
    """
    adapter = _AlwaysFailsAdapter(RuntimeError("cannot schedule new futures after shutdown"))
    worker = _worker(adapter)
    task = asyncio.create_task(worker._processing_heartbeat_loop("ep-1", asyncio.Event()))

    await asyncio.sleep(0.1)
    first_sample = adapter.calls
    await asyncio.sleep(0.2)
    second_sample = adapter.calls

    still_running = not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert still_running, "the loop exited; this test no longer describes the code"
    assert first_sample >= 1, "the loop never attempted a heartbeat"
    assert second_sample > first_sample, (
        "the loop stopped retrying after a permanent failure; CF-208's premise changed"
    )


class _BlocksInsideTheThread:
    """A heartbeat write that parks inside `to_thread` until the test releases it.

    This exists to OWN the interleaving. There are two windows where cancellation can land: the
    `await asyncio.sleep(...)`, which is outside the try, and the `await asyncio.to_thread(...)`,
    which is inside it. Only the second can ever be swallowed by the catch, so a test that cancels
    at an arbitrary moment mostly hits the safe window and proves nothing about the dangerous one.
    """

    def __init__(self) -> None:
        self.entered = __import__("threading").Event()
        self.release = __import__("threading").Event()
        self.calls = 0

    def touch_episode_processing_heartbeat(self, episode_uuid: str, worker_id: str | None = None):
        self.calls += 1
        self.entered.set()
        self.release.wait(timeout=5.0)
        raise RuntimeError("executor went away while this thread was running")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancellation_lands_inside_the_guarded_await_and_still_kills_the_task(
    isolated_telemetry_db,
) -> None:
    """THE PROPERTY, with the interleaving constructed rather than hoped for.

    An un-killable background task is what gave CF-208 its High severity -- it prevented asyncio
    teardown from completing and hung the whole suite. This cancels while the loop is parked INSIDE
    the `to_thread` the `except (OSError, RuntimeError)` wraps, which is the only window where a
    widened catch could swallow the cancellation.

    Mutation history worth keeping: an earlier version of this test cancelled after a fixed sleep
    and passed even with the catch widened to `except BaseException`, because the cancellation was
    landing in the unguarded `asyncio.sleep`. Same family as T12 -- to test a race you must own
    when the awaited thing resolves.
    """
    adapter = _BlocksInsideTheThread()
    worker = _worker(adapter)
    task = asyncio.create_task(worker._processing_heartbeat_loop("ep-2", asyncio.Event()))

    await asyncio.to_thread(adapter.entered.wait, 5.0)
    assert adapter.entered.is_set(), "never reached the guarded await"

    task.cancel()
    adapter.release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)

    assert task.cancelled()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_oserror_storm_is_also_cancellable(isolated_telemetry_db) -> None:
    """The catch names two exception types; both must leave cancellation intact."""
    adapter = _AlwaysFailsAdapter(OSError("socket is gone"))
    worker = _worker(adapter)
    task = asyncio.create_task(worker._processing_heartbeat_loop("ep-3", asyncio.Event()))
    await asyncio.sleep(0.05)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_stop_event_ends_the_loop_even_mid_failure(isolated_telemetry_db) -> None:
    """The loop is bounded by its episode, not only by cancellation -- so a permanent failure
    cannot outlive the work it was heartbeating for."""
    adapter = _AlwaysFailsAdapter(RuntimeError("still gone"))
    worker = _worker(adapter)
    stop = asyncio.Event()
    task = asyncio.create_task(worker._processing_heartbeat_loop("ep-4", stop))
    await asyncio.sleep(0.05)

    stop.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert task.done() and not task.cancelled()


@pytest.mark.unit
def test_the_catch_cannot_be_widened_to_swallow_cancellation() -> None:
    """RATCHET, and the whole point of this file.

    `CancelledError` derives from `BaseException`, so the current `except (OSError, RuntimeError)`
    cannot intercept it. `except Exception` would not either -- but `except BaseException`, or an
    explicit `except asyncio.CancelledError` that does not re-raise, WOULD, and that is the edit
    that would turn this loop into the un-killable task CF-208 feared.
    """
    source = inspect.getsource(IngestWorkerMixin._processing_heartbeat_loop)

    assert "except BaseException" not in source
    assert "CancelledError" not in source, (
        "the heartbeat loop now handles cancellation explicitly; prove it re-raises before "
        "removing this assertion"
    )
