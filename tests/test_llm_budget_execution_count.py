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
import threading

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

    def __init__(self, *, max_per_job: int, session: object | None = None) -> None:
        from menhir.services.ingest_worker import IngestWorkerMixin

        self._job_llm_call_counts: dict[str, int] = {}
        self._budget_settings_max_per_job = max_per_job
        # IngestService mixes the queue and worker together, so one object owns both budgets.
        # Mirroring that here is what lets the per-call reservation be exercised through the
        # real callback rather than called directly.
        if session is not None:
            self._budget_settings_max_calls = session._budget_settings_max_calls
            self._budget_settings_window_s = session._budget_settings_window_s
            self._session_llm_call_times = session._session_llm_call_times
            self._session_llm_call_lock = session._session_llm_call_lock
            self.reserve_session_llm_call = session.reserve_session_llm_call
        self._record_episode_llm_usage = (
            IngestWorkerMixin._record_episode_llm_usage.__get__(self)
        )

        class _Graph:
            def increment_episode_llm_usage(self, *a, **kw):
                return None

        self.graph_adapter = _Graph()


def _under_budget(worker: _Worker, episode_uuid: str, budget_key: str | None = None):
    """Install the usage callback exactly as `_process_episode` does, session key included."""
    return set_llm_usage_callback(
        lambda event: worker._record_episode_llm_usage(
            episode_uuid, event, budget_key=budget_key
        )
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
    # A THREADING lock, matching production: the per-call reservation fires from worker threads.
    q._session_llm_call_lock = threading.Lock()
    for name in (
        "_check_session_budget",
        "_session_window_retry_after",
        "reserve_session_llm_call",
    ):
        setattr(q, name, getattr(IngestQueueMixin, name).__get__(q))
    return q

@pytest.mark.asyncio
async def test_the_session_window_bounds_CALLS_across_MULTIPLE_EPISODES() -> None:
    """The CF-79 session half, and the acceptance condition: count executed model calls across
    several episodes sharing one session.

    **The contract changed deliberately, so the old tests for it did too.** The window used to
    consume one slot per enrichment ATTEMPT, which is why an attempt making twenty calls cost the
    same as one making one. It now consumes one slot per CALL, so this is the first test that can
    tell those two designs apart -- three episodes making two calls each exhaust a budget of five
    on the sixth CALL, wherever it falls.
    """
    q = _queue(max_calls=5, window_s=60)
    seam = _ModelSeam()
    key = "session-A"

    with pytest.raises(LlmBudgetExceeded):
        for episode in ("ep-1", "ep-2", "ep-3"):
            worker = _Worker(max_per_job=100, session=q)  # per-job is not the constraint here
            _under_budget(worker, episode, key)
            for _ in range(2):
                seam.call()

    assert seam.executed == 5, (
        f"executed {seam.executed} calls across three episodes against a session budget of 5"
    )


@pytest.mark.asyncio
async def test_the_pre_attempt_gate_refuses_a_session_whose_calls_are_spent() -> None:
    """The gate survives the change and now reads the CALL ledger.

    Refusing to start work whose budget is already gone is cheaper than refusing it
    mid-extraction, and it keeps the retry-after behaviour that requeues the episode rather than
    failing it.
    """
    q = _queue(max_calls=3, window_s=60)
    seam = _ModelSeam()
    worker = _Worker(max_per_job=100, session=q)
    _under_budget(worker, "ep-1", "session-A")

    assert await q._check_session_budget("ep-1", "session-A") is None
    for _ in range(3):
        seam.call()
    assert seam.executed == 3

    retry_after = await q._check_session_budget("ep-2", "session-A")
    assert retry_after is not None and retry_after > 0, (
        "a session whose call budget is spent still admitted a new episode"
    )


@pytest.mark.asyncio
async def test_sessions_do_not_share_a_call_window() -> None:
    """One tenant's session exhausting the budget must not stop another's."""
    q = _queue(max_calls=2, window_s=60)
    seam = _ModelSeam()

    worker_a = _Worker(max_per_job=100, session=q)
    _under_budget(worker_a, "ep-a", "session-A")
    seam.call()
    seam.call()
    with pytest.raises(LlmBudgetExceeded):
        seam.call()
    assert seam.executed == 2

    worker_b = _Worker(max_per_job=100, session=q)
    _under_budget(worker_b, "ep-b", "session-B")
    seam.call()
    assert seam.executed == 3, "one session exhausted another session's call budget"


@pytest.mark.asyncio
async def test_concurrent_episodes_compete_for_one_session_call_budget() -> None:
    """Concurrency on real worker THREADS, dispatched the way production dispatches.

    `asyncio.to_thread` is not interchangeable with `threading.Thread` here, and getting it wrong
    is silent. The usage callback is held in a CONTEXTVAR: `asyncio.to_thread` copies the context
    into the worker, so the budget applies; a bare `threading.Thread` starts with a fresh context,
    `_emit_llm_usage_event` finds no callback, and every call runs UNMETERED. A first version of
    this test used `threading.Thread` and saw 40 of 40 calls execute -- which looked like a broken
    budget and was actually a test that had disconnected the budget it meant to measure.

    Production is clean: every LLM dispatch goes through `asyncio.to_thread` (`graphiti_client`),
    and the only raw thread in the codebase is the saga gate heartbeat, which makes no model
    calls. But it is a live hazard for anything added later -- an LLM call dispatched on a raw
    thread or a plain executor escapes both budgets entirely and nothing reports it.

    **What this does NOT prove, stated because the inverse test was run and came back negative:**
    removing the lock from `reserve_session_llm_call` leaves this test PASSING. Under CPython the
    critical section is a deque trim and a length compare, and the GIL makes the interleaving that
    would double-admit vanishingly unlikely at this scale. So the lock is correct practice for a
    structure genuinely reached from multiple threads -- but it is defensive here rather than
    demonstrated load-bearing, and claiming otherwise on the strength of a green test would be
    exactly the counter-vs-execution mistake this file exists to avoid.
    """
    q = _queue(max_calls=5, window_s=60)
    seam = _ModelSeam()
    worker = _Worker(max_per_job=1000, session=q)
    _under_budget(worker, "ep-shared", "session-A")

    refused: list[int] = []

    def attempt() -> None:
        try:
            seam.call()
        except LlmBudgetExceeded:
            refused.append(1)

    await asyncio.gather(*[asyncio.to_thread(attempt) for _ in range(40)])

    assert seam.executed == 5, (
        f"{seam.executed} of 40 concurrent calls executed against a session budget of 5"
    )
    assert len(refused) == 35


def test_the_per_job_budget_still_binds_inside_a_generous_session() -> None:
    """Both controls apply, and the per-job one is checked FIRST.

    Ordering matters for diagnosis rather than safety: attributing a single runaway episode's
    overrun to "session exhausted" would point an operator at the wrong cause.
    """
    q = _queue(max_calls=1000, window_s=60)
    seam = _ModelSeam()
    worker = _Worker(max_per_job=2, session=q)
    _under_budget(worker, "ep-1", "session-A")

    seam.call()
    seam.call()
    with pytest.raises(LlmBudgetExceeded, match="per-job"):
        seam.call()
    assert seam.executed == 2
