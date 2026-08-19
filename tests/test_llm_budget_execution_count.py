"""CF-79: count the model calls that actually EXECUTE, not the counters that describe them.

**Why counters are the wrong thing to assert on.** CF-79's whole finding was that two controls
were named as limits and implemented as telemetry: they incremented, they logged, and the call
proceeded. A test asserting "the counter reached 6" would have passed against that broken code,
because the counter was never the problem. The only assertion that distinguishes a limit from a
meter is **how many calls ran**.

So these tests instrument the real seam. Every LLM call in this codebase passes through
`observability.start_llm_usage_call`, which is what fires the `phase="started"` event the budget
reserves against. A `_ModelSeam` here does exactly what production does -- announce the call,
then execute -- and counts only the executions that were actually reached. If the reservation
raises, the body never runs and the count does not move.

**One episode fans out into many calls**, because that is the amplification the finding proves:
one attempt costs roughly `1 + N_entities searches + (surviving pairs x 3)` judge calls, and
episode CONTENT decides `N_entities`. The quantity is attacker-influenced, which is why an
in-flight bound is the only kind that helps.

**This file deliberately contains a failing-by-design expectation, marked xfail.** The session
window still meters ATTEMPTS, not calls -- an attempt making twenty calls consumes the same
single slot as one making one. That is recorded as PARTIALLY FIXED in CF-79 and is asserted here
as `xfail(strict=True)` rather than omitted: a gap that no test names is indistinguishable from
one nobody found, and strict mode means the day it starts passing, this file fails and tells
someone to close the finding.
"""

from __future__ import annotations

import asyncio

import pytest

from menhir.infrastructure.observability import (
    set_llm_usage_callback,
    start_llm_usage_call,
)
from menhir.services.ingest_worker import LlmBudgetExceeded

pytestmark = [pytest.mark.unit]


class _ModelSeam:
    """A stand-in model that behaves like production: announce, then execute.

    `executed` counts calls whose BODY was reached. That is the number the finding is about --
    a control that increments a counter and lets the call through leaves this number unbounded,
    which is precisely how CF-79 was able to exist while the counters looked correct.
    """

    def __init__(self) -> None:
        self.announced = 0
        self.executed = 0

    def call(self, kind: str = "extraction") -> str:
        self.announced += 1
        # Raises if the reservation refuses -- exactly as it does in production, before any
        # model work happens.
        start_llm_usage_call(kind=kind, model="test-model", endpoint="test")
        self.executed += 1
        return "result"


class _Worker:
    """The real per-job reservation, on a minimal host object.

    `_record_episode_llm_usage` is bound to `IngestWorkerMixin` and only touches the two
    attributes set here plus `graph_adapter`, so this exercises the production method rather
    than a copy of its logic.
    """

    def __init__(self, *, max_per_job: int) -> None:
        from menhir.services.ingest_worker import IngestWorkerMixin

        self._job_llm_call_counts: dict[str, int] = {}
        self._budget_settings_max_per_job = max_per_job
        self._record_episode_llm_usage = (
            IngestWorkerMixin._record_episode_llm_usage.__get__(self)
        )

        class _Graph:
            def increment_episode_llm_usage(self, *a, **kw):
                return None

        self.graph_adapter = _Graph()


def _under_budget(worker: _Worker, episode_uuid: str):
    """Install the usage callback exactly as `_process_episode` does."""
    return set_llm_usage_callback(
        lambda event: worker._record_episode_llm_usage(episode_uuid, event)
    )


# ---------------------------------------------------------------------------
# Per-job budget: the executions stop, not just the counter
# ---------------------------------------------------------------------------

def test_a_fanning_out_episode_stops_EXECUTING_at_the_per_job_limit() -> None:
    """The finding, measured the only way that distinguishes a limit from a meter.

    The episode asks for twenty calls -- the amplification an attacker-shaped episode produces.
    Six are permitted. The assertion is on `executed`, so a control that warned and let the call
    through would fail here even though every counter it maintained was correct.
    """
    worker = _Worker(max_per_job=6)
    seam = _ModelSeam()
    token = _under_budget(worker, "ep-1")
    try:
        with pytest.raises(LlmBudgetExceeded):
            for _ in range(20):
                seam.call()
    finally:
        set_llm_usage_callback(None) if token is None else None

    assert seam.executed == 6, f"executed {seam.executed} calls against a limit of 6"
    assert seam.announced == 7, "the refusal should land on the 7th announcement"


def test_the_budget_is_a_RESERVATION_so_the_breaching_call_never_runs() -> None:
    """The ordering that makes it a reservation rather than a post-hoc check: the counter is
    incremented and the breach raised BEFORE the call is permitted, so the call that breaches is
    itself refused. A post-hoc check would let call N+1 run and refuse N+2."""
    worker = _Worker(max_per_job=1)
    seam = _ModelSeam()
    _under_budget(worker, "ep-1")

    seam.call()
    assert seam.executed == 1

    with pytest.raises(LlmBudgetExceeded):
        seam.call()
    assert seam.executed == 1, "the breaching call executed anyway"


def test_a_failed_call_still_consumes_its_reservation() -> None:
    """Budget must be consumed at ANNOUNCEMENT, not at success.

    Otherwise the cheapest way past the limit is to make calls that fail: a retry loop around a
    failing model would consume unbounded model capacity while the budget never moved. This is
    the same asymmetry that makes reservation the right shape.
    """
    worker = _Worker(max_per_job=3)
    seam = _ModelSeam()
    _under_budget(worker, "ep-1")

    for _ in range(3):
        start_llm_usage_call(kind="extraction", model="m", endpoint="e")
        # ...and the "call" then fails; no completion event is emitted.

    with pytest.raises(LlmBudgetExceeded):
        seam.call()
    assert seam.executed == 0, "a budget exhausted by failures still permitted a fresh call"


def test_each_episode_gets_its_own_per_job_budget() -> None:
    """Per-JOB means per-episode. One episode exhausting its budget must not starve the next, or
    a single hostile episode becomes a denial of service on every subsequent one."""
    worker = _Worker(max_per_job=2)
    seam = _ModelSeam()

    _under_budget(worker, "ep-1")
    seam.call()
    seam.call()
    with pytest.raises(LlmBudgetExceeded):
        seam.call()

    _under_budget(worker, "ep-2")
    seam.call()
    seam.call()
    assert seam.executed == 4, "the second episode inherited the first episode's exhaustion"


# ---------------------------------------------------------------------------
# Session window: what it does bound, and what it does not
# ---------------------------------------------------------------------------

def _queue(*, max_calls: int, window_s: int):
    from menhir.services.ingest_queue import IngestQueueMixin

    class _Q:
        pass

    q = _Q()
    q._budget_settings_max_calls = max_calls
    q._budget_settings_window_s = window_s
    q._session_llm_call_times = {}
    q._session_llm_budget_lock = asyncio.Lock()
    q._check_session_budget = IngestQueueMixin._check_session_budget.__get__(q)
    return q


@pytest.mark.asyncio
async def test_the_session_window_bounds_ATTEMPTS_across_jobs() -> None:
    """What the session control genuinely does today, asserted honestly.

    It is a rate limit on enrichment ATTEMPTS per session per window, and within that reading it
    works: the fourth attempt in a 3-attempt window is deferred with a retry hint rather than
    admitted.
    """
    q = _queue(max_calls=3, window_s=60)

    for _ in range(3):
        assert await q._check_session_budget("ep", "session-A") is None

    retry_after = await q._check_session_budget("ep", "session-A")
    assert retry_after is not None and retry_after > 0


@pytest.mark.asyncio
async def test_sessions_do_not_share_a_window() -> None:
    q = _queue(max_calls=2, window_s=60)
    for _ in range(2):
        assert await q._check_session_budget("ep", "session-A") is None
    assert await q._check_session_budget("ep", "session-A") is not None
    assert await q._check_session_budget("ep", "session-B") is None, (
        "one session exhausted another session's budget"
    )


@pytest.mark.asyncio
async def test_concurrent_attempts_compete_for_one_session_budget() -> None:
    """The lock is load-bearing: without it, N concurrent attempts each read the deque before any
    appends, and all N are admitted against a budget of one."""
    q = _queue(max_calls=3, window_s=60)

    results = await asyncio.gather(
        *[q._check_session_budget(f"ep-{i}", "session-A") for i in range(10)]
    )
    admitted = [r for r in results if r is None]
    assert len(admitted) == 3, (
        f"{len(admitted)} concurrent attempts were admitted against a budget of 3"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "CF-79 remaining half: the session window meters ATTEMPTS, not CALLS. One attempt "
        "making twenty model calls consumes the same single slot as one making one, so the "
        "session control does not bound call VOLUME at all. Closing it means threading the "
        "session key into the usage callback so the deque is appended per call. Asserted "
        "strict so that this file FAILS the day it is fixed, rather than silently passing and "
        "leaving the finding open in the register."
    ),
)
@pytest.mark.asyncio
async def test_the_session_window_should_bound_CALLS_not_attempts() -> None:
    """The defect this file exists to name, written as the test that will pass once it is fixed.

    One attempt, twenty model calls. The session budget of 3 should be exhausted by call volume;
    today it sees a single attempt and stays almost untouched.
    """
    q = _queue(max_calls=3, window_s=60)
    worker = _Worker(max_per_job=100)  # per-job budget deliberately not the constraint here
    seam = _ModelSeam()
    _under_budget(worker, "ep-1")

    assert await q._check_session_budget("ep-1", "session-A") is None
    for _ in range(20):
        seam.call()

    assert seam.executed == 20
    # If the window metered CALLS, twenty of them would have exhausted a budget of three.
    assert await q._check_session_budget("ep-2", "session-A") is not None, (
        "twenty model calls did not consume the session budget"
    )
