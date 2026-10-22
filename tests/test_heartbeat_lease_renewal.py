"""Heartbeat-driven lease renewal with a hard enrichment ceiling (offline).

Pins the pieces that make a long enrichment survive the stale-lease sweep without letting a hung
one live forever:

* the repository touch is fenced on owner + claim instant and only renews up to the ceiling;
* the worker heartbeat loop stops itself when the fence is lost;
* the worker abandons the episode at the ceiling and the failure is never graded retryable.

The real-Neo4j counterexamples live in tests/test_heartbeat_lease_renewal_live.py.
"""

from __future__ import annotations

import asyncio
import types
from typing import Any

import pytest

from menhir.infrastructure.episode_stamping import EpisodeStampingRepository
from menhir.services import IngestService
from menhir.services.enrichment_failures import (
    EnrichmentDeadlineExceeded,
    classify_enrichment_failure,
)
from menhir.services.ingest_worker import IngestWorkerMixin


class _RecordingNeo4j:
    """Records every execute(query, params) call; reports one matched row."""

    def __init__(self, updated: int = 1) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.call_kwargs: list[dict[str, Any]] = []  # extra execute() kwargs, parallel to calls
        self.updated = updated

    def execute(
        self, query: str, params: dict[str, Any] | None = None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        self.calls.append((query, dict(params or {})))
        self.call_kwargs.append(dict(kwargs))
        return [{"updated": self.updated}]


def _repo(updated: int = 1) -> tuple[EpisodeStampingRepository, _RecordingNeo4j]:
    repo = EpisodeStampingRepository()
    fake = _RecordingNeo4j(updated)
    repo.neo4j = fake
    return repo, fake


# --------------------------------------------------------------------------- repository query


@pytest.mark.unit
def test_legacy_touch_does_not_renew_the_lease() -> None:
    """Breaks if the legacy call starts writing the lease (or grows extra parameters)."""
    repo, fake = _repo()

    assert repo.touch_episode_processing_heartbeat("ep-1", worker_id="w-1") is True

    (query, params), = fake.calls
    assert "processing_lease_expires_at" not in query
    assert "processing_heartbeat_at" in query
    assert params == {"episode_uuid": "ep-1", "worker_id": "w-1"}


@pytest.mark.unit
def test_renewing_touch_is_fenced_and_ceilinged() -> None:
    """Breaks if the owner fence, either half of the claim-instant fence, or the CASE ceiling
    is dropped from the renewing statement, or lease/ceiling stop being passed as ints."""
    repo, fake = _repo()

    ok = repo.touch_episode_processing_heartbeat(
        "ep-2",
        worker_id="w-2",
        claim_started_at="2026-01-01T00:00:00.123456789Z",
        lease_seconds="30",  # type: ignore[arg-type]  # coerced to int by the repository
        max_enrichment_seconds=3600.0,  # type: ignore[arg-type]
    )

    assert ok is True
    (query, params), = fake.calls
    assert "n.processing_state = 'ENRICHING'" in query
    assert "n.processing_owner = $worker_id" in query
    assert "processing_started_at.epochSeconds = datetime($claim_started_at).epochSeconds" in query
    assert "processing_started_at.nanosecond = datetime($claim_started_at).nanosecond" in query
    assert "CASE WHEN datetime() < n.processing_started_at + duration({seconds: $max_enrichment_seconds})" in query
    assert "THEN datetime() + duration({seconds: $lease_seconds})" in query
    assert "ELSE n.processing_lease_expires_at END" in query
    assert params["episode_uuid"] == "ep-2"
    assert params["worker_id"] == "w-2"
    assert params["claim_started_at"] == "2026-01-01T00:00:00.123456789Z"
    assert params["lease_seconds"] == 30 and type(params["lease_seconds"]) is int
    assert params["max_enrichment_seconds"] == 3600 and type(params["max_enrichment_seconds"]) is int


@pytest.mark.unit
def test_renewing_touch_reports_false_when_the_fence_matches_no_row() -> None:
    """Breaks if a non-matching fence is reported as success (the loop would never see 'lost')."""
    repo, _fake = _repo(updated=0)

    assert (
        repo.touch_episode_processing_heartbeat(
            "ep-3",
            worker_id="w-3",
            claim_started_at="2026-01-01T00:00:00Z",
            lease_seconds=30,
            max_enrichment_seconds=3600,
        )
        is False
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "kwargs",
    [
        {"claim_started_at": "2026-01-01T00:00:00Z"},
        {"lease_seconds": 30},
        {"max_enrichment_seconds": 3600},
        {"claim_started_at": "2026-01-01T00:00:00Z", "lease_seconds": 30},
        {"lease_seconds": 30, "max_enrichment_seconds": 3600},
    ],
)
def test_partial_renewal_arguments_are_rejected(kwargs: dict[str, Any]) -> None:
    """Breaks if a half-specified renewal silently falls back to legacy or renews unfenced."""
    repo, fake = _repo()

    with pytest.raises(ValueError):
        repo.touch_episode_processing_heartbeat("ep-4", worker_id="w-4", **kwargs)
    assert fake.calls == []


@pytest.mark.unit
def test_full_renewal_arguments_without_worker_id_are_rejected() -> None:
    """Breaks if renewal can run without an owner to fence on."""
    repo, fake = _repo()

    for worker_id in (None,):
        with pytest.raises(ValueError):
            repo.touch_episode_processing_heartbeat(
                "ep-5",
                worker_id=worker_id,
                claim_started_at="2026-01-01T00:00:00Z",
                lease_seconds=30,
                max_enrichment_seconds=3600,
            )
    assert fake.calls == []


# --------------------------------------------------------------------------- heartbeat loop


class _KwargRecordingAdapter:
    def __init__(self, results: list[bool] | None = None) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self._results = list(results or [])

    def touch_episode_processing_heartbeat(self, *args: Any, **kwargs: Any) -> bool:
        self.calls.append((args, kwargs))
        return self._results.pop(0) if self._results else True


class _LegacyOnlyAdapter:
    """Accepts ONLY the legacy signature: any renewal kwarg raises TypeError."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def touch_episode_processing_heartbeat(self, episode_uuid: str, worker_id: str | None = None) -> bool:
        self.calls.append((episode_uuid, worker_id))
        return True


def _worker(adapter: Any) -> IngestWorkerMixin:
    worker = IngestWorkerMixin()
    worker.graph_adapter = adapter
    worker.graphiti_client = types.SimpleNamespace(scheduler_fallback_base_url="")
    worker._worker_id = "loop-worker"
    worker._processing_heartbeat_interval_s = 0.01
    worker._scheduler_http_client = None
    worker._enrichment_lease_seconds = 123
    worker._max_enrichment_seconds = 456
    return worker


async def _wait_for(predicate, timeout_s: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_loop_with_claim_instant_passes_lease_and_ceiling(isolated_telemetry_db) -> None:
    """Breaks if the loop drops claim_started_at / lease / ceiling, or sources them from
    anything but the worker's configured values (distinct sentinels 123/456 prove the source)."""
    adapter = _KwargRecordingAdapter()
    worker = _worker(adapter)
    stop = asyncio.Event()
    claim = object()
    task = asyncio.create_task(worker._processing_heartbeat_loop("ep-6", stop, claim_started_at=claim))

    assert await _wait_for(lambda: len(adapter.calls) >= 2), "loop never touched twice"
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)

    args, kwargs = adapter.calls[0]
    assert args == ("ep-6",)
    assert kwargs == {
        "worker_id": "loop-worker",
        "claim_started_at": claim,
        "lease_seconds": 123,
        "max_enrichment_seconds": 456,
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_loop_stops_by_itself_when_the_fence_is_lost(isolated_telemetry_db) -> None:
    """Breaks if a False touch is ignored: the loop would keep heartbeating a row it no longer
    owns (and, with a real adapter, would never learn the claim was reset or re-taken)."""
    adapter = _KwargRecordingAdapter(results=[True, False, True, True, True])
    worker = _worker(adapter)
    stop = asyncio.Event()  # never set: the loop must end on its own

    await asyncio.wait_for(
        worker._processing_heartbeat_loop("ep-7", stop, claim_started_at=object()),
        timeout=2.0,
    )
    await asyncio.sleep(0.1)  # a loop that kept going would add calls here

    assert len(adapter.calls) == 2
    assert not stop.is_set()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_legacy_loop_call_uses_the_two_argument_touch_only(isolated_telemetry_db) -> None:
    """Breaks if the no-claim path starts sending renewal kwargs (the legacy adapter raises
    TypeError on them, which the loop does not swallow, so the task would die)."""
    adapter = _LegacyOnlyAdapter()
    worker = _worker(adapter)
    stop = asyncio.Event()
    task = asyncio.create_task(worker._processing_heartbeat_loop("ep-8", stop))

    assert await _wait_for(lambda: len(adapter.calls) >= 2)
    assert not task.done(), "legacy loop died (renewal kwargs were probably passed)"
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert all(call == ("ep-8", "loop-worker") for call in adapter.calls)


# --------------------------------------------------------------------------- worker deadline


def _service(adapter, graphiti, llm, *, max_enrichment_seconds: float) -> IngestService:
    service = IngestService(graphiti_client=graphiti, graph_adapter=adapter, llm=llm)
    service._max_enrichment_seconds = max_enrichment_seconds
    return service


def _create(adapter, episode_uuid: str) -> str:
    return adapter.create_pending_episode(
        episode_uuid=episode_uuid,
        name=f"episode-session-1-{episode_uuid}",
        content="deadline probe",
        session_id="session-1",
        user_id="user-1",
        source="unit-test",
        source_confidence=0.5,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enrichment_past_the_ceiling_fails_with_deadline_exceeded(
    stub_memory_graph_adapter,
    stub_graphiti_client,
    stub_llm_adapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if asyncio.timeout is removed (the 5 s sleep would complete and the episode would
    not fail), if the expiry is not converted to EnrichmentDeadlineExceeded, or if the
    persisted failure is graded retryable (which would refund the attempt and loop forever)."""
    failures: list[dict[str, object]] = []
    monkeypatch.setattr(
        "menhir.services.enrichment_steps.record_failure_event", lambda **kw: failures.append(kw)
    )
    seen: list[BaseException] = []

    import menhir.services.ingest_worker as worker_module

    real_handle = worker_module.handle_enrichment_failure

    async def spy(ctx, exc):
        seen.append(exc)
        await real_handle(ctx, exc)

    async def slow_reconcile(ctx):
        await asyncio.sleep(5.0)
        return False

    monkeypatch.setattr(worker_module, "handle_enrichment_failure", spy)
    monkeypatch.setattr(worker_module, "try_reconcile_existing", slow_reconcile)

    adapter = stub_memory_graph_adapter
    service = _service(adapter, stub_graphiti_client, stub_llm_adapter, max_enrichment_seconds=0.05)
    episode_uuid = _create(adapter, "deadline-1")

    await asyncio.wait_for(service._process_pending_episode(episode_uuid), timeout=3.0)

    assert len(seen) == 1
    assert isinstance(seen[0], EnrichmentDeadlineExceeded)
    assert "enrichment_deadline_exceeded" in str(seen[0])
    assert isinstance(seen[0].__cause__, TimeoutError)
    row = adapter.pending_episode_rows[episode_uuid]
    assert row["processing_state"] == "FAILED"
    assert "enrichment_deadline_exceeded" in str(row["processing_error"])
    assert failures[-1]["classification"] == "manual_review"
    assert failures[-1]["retryable"] is False
    assert failures[-1]["error_type"] == "EnrichmentDeadlineExceeded"
    assert service._failed_enrichments == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_timeout_raised_by_the_body_before_the_deadline_is_not_converted(
    stub_memory_graph_adapter,
    stub_graphiti_client,
    stub_llm_adapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if the except clause converts EVERY TimeoutError (i.e. drops the
    `deadline.expired()` check): an ordinary inner timeout would then be mis-graded as a hung
    episode and parked for manual review instead of following its own classification."""
    import menhir.services.ingest_worker as worker_module

    seen: list[BaseException] = []
    inner = TimeoutError("inner call timed out")

    async def spy(ctx, exc):
        seen.append(exc)

    async def raising_reconcile(ctx):
        raise inner

    monkeypatch.setattr(worker_module, "handle_enrichment_failure", spy)
    monkeypatch.setattr(worker_module, "try_reconcile_existing", raising_reconcile)

    adapter = stub_memory_graph_adapter
    service = _service(adapter, stub_graphiti_client, stub_llm_adapter, max_enrichment_seconds=3600)
    episode_uuid = _create(adapter, "deadline-2")

    await service._process_pending_episode(episode_uuid)

    assert seen == [inner]
    assert seen[0] is inner
    assert not isinstance(seen[0], EnrichmentDeadlineExceeded)


# --------------------------------------------------------------------------- classification


@pytest.mark.unit
def test_deadline_exceeded_is_never_retryable() -> None:
    """Breaks if the deadline grade falls through to the 'timeout'/'timed out' retryable
    markers (the message deliberately contains none, but the type must win regardless)."""
    exc = EnrichmentDeadlineExceeded("enrichment_deadline_exceeded: episode x ran past 3600s")

    assert classify_enrichment_failure(exc) == "manual_review"
    assert classify_enrichment_failure(exc, error_type="EnrichmentDeadlineExceeded") == "manual_review"
    # Even when wrapped in words that ARE retryable markers, the type decides.
    assert (
        classify_enrichment_failure(EnrichmentDeadlineExceeded("connection timeout while abandoned"))
        == "manual_review"
    )


@pytest.mark.unit
def test_persisted_deadline_text_is_never_retryable() -> None:
    """Retry polling reads the persisted message, not the exception: the reason code alone must
    keep it out of the retryable class, even next to a retryable marker."""
    assert classify_enrichment_failure("enrichment_deadline_exceeded: episode x ran past 3600s") != "retryable"
    assert (
        classify_enrichment_failure("enrichment_deadline_exceeded after connection timeout")
        == "manual_review"
    )


# --------------------------------------------------------------------------- reexecute safety


@pytest.mark.unit
def test_neither_touch_opts_into_reexecution() -> None:
    """CF-158 ratchet: no production caller passes safe_to_reexecute. The renewal relies on the
    heartbeat loop surviving the error and retrying on its next tick instead."""
    repo, fake = _repo()

    repo.touch_episode_processing_heartbeat(
        "ep-r1",
        worker_id="w",
        claim_started_at="2026-01-01T00:00:00Z",
        lease_seconds=30,
        max_enrichment_seconds=3600,
    )
    repo.touch_episode_processing_heartbeat("ep-r2", worker_id="w")

    assert "safe_to_reexecute" not in fake.call_kwargs[0]
    assert "safe_to_reexecute" not in fake.call_kwargs[1]


@pytest.mark.unit
def test_enrichment_ceiling_default_is_three_hours() -> None:
    """Breaks if the default hung-task ceiling changes from 10800 s without this being a
    deliberate decision (the heartbeat renews only up to it)."""
    service = IngestService(
        graphiti_client=types.SimpleNamespace(), graph_adapter=_KwargRecordingAdapter(), llm=None
    )
    assert service._max_enrichment_seconds == 10800


# --------------------------------------------------------------------------- driver errors


class _FlakyAdapter:
    """Raises `first_error` on the first touch, then reports a healthy touch forever."""

    def __init__(self, first_error: BaseException) -> None:
        self.first_error = first_error
        self.calls = 0

    def touch_episode_processing_heartbeat(self, *args: Any, **kwargs: Any) -> bool:
        self.calls += 1
        if self.calls == 1:
            raise self.first_error
        return True


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("renewing", [True, False], ids=["renewing", "legacy"])
async def test_loop_survives_a_neo4j_driver_error(renewing: bool, isolated_telemetry_db) -> None:
    """Breaks if the loop's except clause narrows back to (OSError, RuntimeError):
    neo4j.exceptions.ServiceUnavailable is neither, so one driver blip would kill the task
    and the lease would lapse with a live worker."""
    from neo4j.exceptions import ServiceUnavailable

    adapter = _FlakyAdapter(ServiceUnavailable("connection reset"))
    worker = _worker(adapter)
    stop = asyncio.Event()
    claim = object() if renewing else None
    task = asyncio.create_task(
        worker._processing_heartbeat_loop("ep-9", stop, claim_started_at=claim)
    )

    assert await _wait_for(lambda: adapter.calls >= 3), "loop did not keep touching after the error"
    assert not task.done(), "loop ended after a single driver error"
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)  # re-raises if an exception escaped
    assert task.exception() is None


# --------------------------------------------------------------------------- worker wiring


@pytest.mark.unit
@pytest.mark.asyncio
async def test_worker_passes_the_claimed_start_instant_into_the_heartbeat_loop(
    stub_memory_graph_adapter,
    stub_graphiti_client,
    stub_llm_adapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaks if _process_pending_episode stops forwarding claimed["processing_started_at"] as
    claim_started_at: the loop would fall back to the legacy, non-renewing touch and every
    enrichment longer than one lease would be reset mid-flight."""
    import menhir.services.ingest_worker as worker_module

    seen: list[dict[str, Any]] = []

    async def spy_loop(episode_uuid, stop_event, **kwargs):
        seen.append({"episode_uuid": episode_uuid, **kwargs})
        await stop_event.wait()

    async def done(ctx):
        await asyncio.sleep(0.05)  # let the heartbeat task start before the episode finishes
        return True

    monkeypatch.setattr(worker_module, "try_reconcile_existing", done)
    adapter = stub_memory_graph_adapter
    service = _service(adapter, stub_graphiti_client, stub_llm_adapter, max_enrichment_seconds=3600)
    monkeypatch.setattr(service, "_processing_heartbeat_loop", spy_loop)
    episode_uuid = _create(adapter, "wiring-1")

    await asyncio.wait_for(service._process_pending_episode(episode_uuid), timeout=3.0)

    assert len(seen) == 1
    instant = adapter.pending_episode_rows[episode_uuid]["processing_started_at"]
    assert instant is not None
    assert seen[0]["episode_uuid"] == episode_uuid
    assert seen[0].get("claim_started_at") == instant
