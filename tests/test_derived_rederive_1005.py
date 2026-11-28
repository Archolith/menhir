"""#1005: a job can report failure (opt-in), failed repairs are requeued after a cooldown, and a
failed fact embedding queues a re-embed instead of finishing the repair with a null vector."""

from __future__ import annotations

import asyncio
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from menhir.services import derived_rederive as dr
from menhir.services.derived_rederive import (
    MAX_ATTEMPTS,
    MAX_EMBED_ATTEMPTS,
    MAX_REQUEUES,
    REQUEUE_COOLDOWN_DAYS,
    REQUEUE_LIMIT,
    run_derived_rederive,
)
from menhir.services.erasure_coordinator import ErasureCoordinator
from menhir.services.scheduler_protocols import JOB_FAILED_KEY
from tests.test_derived_rederive_unit import FakeAdapter, FakeLLM, _item

pytestmark = pytest.mark.unit

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "menhir"


# --------------------------------------------------------------------------- scheduler hook
def _scheduler(monkeypatch):
    from menhir.services import maintenance_scheduler as ms

    events: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    monkeypatch.setattr(ms, "record_mcp_event", lambda **kw: events.append(kw))
    monkeypatch.setattr(ms, "record_failure_event", lambda **kw: failures.append(kw))

    class _Ingest:
        def get_queue_depth(self):
            return 0

    sched = ms.MaintenanceScheduler(ingest_service=_Ingest(), graph_adapter=object())

    async def passthrough(coro, operation):
        return await coro

    monkeypatch.setattr(sched, "_await_job_under_lease", passthrough)
    return ms, sched, events, failures


def _run_job(monkeypatch, result):
    ms, sched, events, failures = _scheduler(monkeypatch)

    async def job():
        return result

    state = ms._JobState(interval_s=60.0)
    asyncio.run(sched._run_job(state, "scheduler_derived_rederive", job()))
    return state, events, failures


def test_job_failed_key_marks_the_run_unsuccessful(monkeypatch):
    result = {"stopped": "provider_unavailable", JOB_FAILED_KEY: True}
    state, events, failures = _run_job(monkeypatch, result)
    assert state.last_success is False
    assert state.last_result == result and state.runs == 1
    assert state.last_completed_at is not None
    [event] = events
    assert event["success"] is False and event["error"] == "job reported failure"
    # The job's own failure events are its business; the wrapper adds no scheduler_exception.
    assert failures == []


@pytest.mark.parametrize(
    "result",
    [
        {"errors": 25, "failed": 25},  # the shape other jobs return: unchanged
        {JOB_FAILED_KEY: False},
        {JOB_FAILED_KEY: "yes"},  # only a literal True opts in
        {},
    ],
)
def test_other_results_are_recorded_as_success(monkeypatch, result):
    state, events, _ = _run_job(monkeypatch, result)
    assert state.last_success is True
    [event] = events
    assert event["success"] is True and "error" not in event


def test_job_failed_does_not_change_scheduling(monkeypatch):
    """A failed run is due again on its interval like any other: no backoff, no disable."""
    ms, sched, _, _ = _scheduler(monkeypatch)
    state = ms._JobState(interval_s=60.0)

    async def job():
        return {JOB_FAILED_KEY: True}

    asyncio.run(sched._run_job(state, "scheduler_derived_rederive", job()))
    asyncio.run(sched._run_job(state, "scheduler_derived_rederive", job()))
    assert state.runs == 2 and state.interval_s == 60.0


def test_only_derived_rederive_uses_the_reserved_key():
    """No other job returns the key, so every other job is recorded exactly as before."""
    users = sorted(
        str(p.relative_to(SRC)).replace("\\", "/")
        for p in SRC.rglob("*.py")
        if '"job_failed"' in p.read_text(encoding="utf-8")
        or "JOB_FAILED_KEY" in p.read_text(encoding="utf-8")
    )
    assert users == [
        "services/derived_rederive.py",
        "services/maintenance_scheduler.py",
        "services/scheduler_protocols.py",
    ]


# --------------------------------------------------------------------------- fake graph
NOW = datetime(2026, 11, 26, tzinfo=timezone.utc)


class Adapter(FakeAdapter):
    """FakeAdapter plus the #1005 queries, modelled on the real Cypher."""

    def __init__(self, items, repairs, **kw):
        super().__init__(items, repairs, **kw)
        self.requeue_calls: list[tuple[float, int, int]] = []
        self.deferred: list[str] = []

    def record_derived_rederive_failure(self, repair_key, error_class, max_attempts):
        status = super().record_derived_rederive_failure(repair_key, error_class, max_attempts)
        self.repairs[repair_key]["last_attempt_at"] = NOW
        return status

    def requeue_failed_derived_rederive(self, cooldown_days, limit, max_requeues):
        self.requeue_calls.append((cooldown_days, limit, max_requeues))
        cutoff = NOW - timedelta(days=cooldown_days)
        due = sorted(
            (r for r in self.repairs.values()
             if r["status"] == "failed" and r.get("last_attempt_at", NOW) < cutoff
             and r.get("requeue_count", 0) < max_requeues),
            key=lambda r: (r.get("last_attempt_at", NOW), r["repair_key"]),
        )[:limit]
        for r in due:
            r.update(status="pending", attempts=0)
            r["requeue_count"] = r.get("requeue_count", 0) + 1
        return [
            {"repair_key": r["repair_key"], "item_kind": r["item_kind"],
             "last_error": r.get("last_error"), "requeue_count": r["requeue_count"]}
            for r in due
        ]

    def list_embed_pending_derived_rederive(self, limit):
        return [r for r in self.repairs.values() if r["status"] == "embed_pending"][:limit]

    def defer_derived_rederive(self, repair_key):
        self.deferred.append(repair_key)
        return True

    def record_derived_rederive_embed_failure(self, repair_key, error_class, max_attempts):
        r = self.repairs[repair_key]
        if r["status"] != "embed_pending":
            return None
        r["embed_attempts"] = r.get("embed_attempts", 0) + 1
        r["last_error"] = error_class
        self.deferred.append(repair_key)
        if r["embed_attempts"] >= max_attempts:
            r["status"] = "embed_failed"
        return r["status"]

    def apply_derived_rederive(self, **kw):
        self.applied.append(kw)
        repair = self.repairs[kw["repair_key"]]
        item = self.items.get((kw["item_kind"], kw["item_uuid"]))
        if kw.get("reembed"):
            if (repair["status"] != "embed_pending" or item is None or item["pending"]
                    or item["text"] != kw["new_text"] or kw.get("embedding") is None):
                return False
            item["embedding"] = kw["embedding"]
            repair["status"] = "done"
            return True
        if kw.get("delete"):
            repair["status"] = "deleted"
            self.items.pop((kw["item_kind"], kw["item_uuid"]), None)
            return True
        item.update(pending=False, text=kw["new_text"], embedding=kw.get("embedding"))
        repair["status"] = "embed_pending" if kw.get("embedding_missing") else "done"
        return True


def _fact_repairs(n):
    return [
        {"repair_key": f"k{i}", "item_kind": "fact", "item_uuid": f"u{i}", "namespace": "default"}
        for i in range(n)
    ]


def _fact_items(n):
    return {("fact", f"u{i}"): _item() for i in range(n)}


class Embedder:
    """``fail``: every call raises (outage). ``reject``: only these texts raise (item fault).
    ``fail_first``: the first N calls raise, later ones (the probe included) succeed."""

    def __init__(self, fail=False, vector=(0.5, 0.25), reject=(), fail_first=0):
        self.fail = fail
        self.vector = list(vector)
        self.reject = set(reject)
        self.fail_first = fail_first
        self.calls: list[str] = []

    async def embed_query(self, text):
        self.calls.append(text)
        if self.fail or text in self.reject or len(self.calls) <= self.fail_first:
            raise TimeoutError("embedder down")
        return self.vector


def _run(adapter, llm, **kw):
    return asyncio.run(run_derived_rederive(adapter, llm, **kw))


# --------------------------------------------------------------------------- job failure flag
def test_provider_stop_sets_job_failed():
    adapter = Adapter(_fact_items(2), _fact_repairs(2))
    result = _run(adapter, FakeLLM(exc=ConnectionError("refused")))
    assert result["stopped"] == "provider_unavailable" and result[JOB_FAILED_KEY] is True


def test_spent_attempts_set_job_failed():
    adapter = Adapter(_fact_items(1), _fact_repairs(1))
    result = _run(adapter, FakeLLM(verdict=None))
    assert result["errors"] == 1 and result[JOB_FAILED_KEY] is True


def test_clean_run_and_budget_refusal_do_not_set_job_failed():
    from menhir.infrastructure.observability import LlmUsageControlSignal

    clean = _run(Adapter(_fact_items(1), _fact_repairs(1)), FakeLLM(("supported", "x")),
                 embedder=Embedder())
    assert JOB_FAILED_KEY not in clean

    class _Budget:
        async def rederive_from_sources(self, **kw):
            raise LlmUsageControlSignal("budget refused")

    refused = _run(Adapter(_fact_items(1), _fact_repairs(1)), _Budget())
    assert refused["stopped"] == "llm_usage_control" and JOB_FAILED_KEY not in refused


def test_no_llm_skip_is_unchanged():
    assert _run(Adapter(_fact_items(1), _fact_repairs(1)), None) == {"skipped": "no_llm"}


# --------------------------------------------------------------------------- requeue
def _failed(adapter, key, days_ago):
    adapter.repairs[key].update(
        status="failed", attempts=MAX_ATTEMPTS, last_error="no_verdict",
        last_attempt_at=NOW - timedelta(days=days_ago),
    )


def test_failed_repair_past_cooldown_is_requeued_and_processed(monkeypatch):
    events = []
    monkeypatch.setattr(dr, "record_failure_event", lambda **kw: events.append(kw) or True)
    adapter = Adapter(_fact_items(1), _fact_repairs(1))
    _failed(adapter, "k0", REQUEUE_COOLDOWN_DAYS + 1)
    result = _run(adapter, FakeLLM(("supported", "new fact")), embedder=Embedder())
    assert result["requeued"] == 1 and result["rewritten"] == 1
    assert adapter.repairs["k0"]["status"] == "done"
    assert adapter.requeue_calls == [(REQUEUE_COOLDOWN_DAYS, REQUEUE_LIMIT, MAX_REQUEUES)]
    [event] = events
    assert event["operation"] == "scheduler_derived_rederive"
    assert event["failure_stage"] == "rederive_requeued" and event["retryable"] is True
    assert event["error_type"] == "no_verdict"
    assert event["details"] == {"item_kind": "fact", "decision": "requeued", "requeue_count": 1}
    assert "old fact" not in repr(event) and "remaining source" not in repr(event)


def test_failed_repair_inside_cooldown_is_not_requeued():
    adapter = Adapter(_fact_items(1), _fact_repairs(1))
    _failed(adapter, "k0", REQUEUE_COOLDOWN_DAYS - 1)
    llm = FakeLLM(("supported", "new fact"))
    result = _run(adapter, llm)
    assert result["requeued"] == 0 and result["processed"] == 0 and llm.calls == 0
    assert adapter.repairs["k0"]["status"] == "failed"
    assert adapter.repairs["k0"]["attempts"] == MAX_ATTEMPTS


def test_requeue_is_bounded_per_run():
    n = REQUEUE_LIMIT + 3
    adapter = Adapter(_fact_items(n), _fact_repairs(n))
    for i in range(n):
        _failed(adapter, f"k{i}", REQUEUE_COOLDOWN_DAYS + 10 + i)
    first = _run(adapter, FakeLLM(("supported", "x")))
    assert first["requeued"] == REQUEUE_LIMIT
    assert sum(r["status"] == "failed" for r in adapter.repairs.values()) == 3
    second = _run(adapter, FakeLLM(("supported", "x")))
    assert second["requeued"] == 3
    assert all(r["status"] == "done" for r in adapter.repairs.values())


def test_requeued_repair_whose_item_is_gone_becomes_obsolete():
    adapter = Adapter({}, _fact_repairs(1))
    _failed(adapter, "k0", REQUEUE_COOLDOWN_DAYS + 1)
    result = _run(adapter, FakeLLM(("supported", "x")))
    assert result["requeued"] == 1 and result["obsolete"] == 1
    assert adapter.repairs["k0"]["status"] == "obsolete"


def test_requeued_repair_that_keeps_failing_fails_again_and_waits_another_cooldown():
    adapter = Adapter(_fact_items(1), _fact_repairs(1))
    _failed(adapter, "k0", REQUEUE_COOLDOWN_DAYS + 1)
    llm = FakeLLM(verdict=None)
    for _ in range(MAX_ATTEMPTS):
        _run(adapter, llm)
    assert adapter.repairs["k0"]["status"] == "failed"
    # last_attempt_at is now: the next runs do not requeue it.
    assert _run(adapter, llm)["requeued"] == 0
    assert llm.calls == MAX_ATTEMPTS


def test_requeue_store_error_does_not_end_the_run():
    class _Broken(Adapter):
        def requeue_failed_derived_rederive(self, cooldown_days, limit, max_requeues):
            raise RuntimeError("neo4j down")

    adapter = _Broken(_fact_items(1), _fact_repairs(1))
    result = _run(adapter, FakeLLM(("supported", "x")))
    assert result["requeued"] == 0 and result["rewritten"] == 1


def test_adapter_without_requeue_still_runs():
    adapter = FakeAdapter(_fact_items(1), _fact_repairs(1))
    result = _run(adapter, FakeLLM(("supported", "x")))
    assert result["requeued"] == 0 and result["rewritten"] == 1


# --------------------------------------------------------------------------- null embedding
def test_failed_embedding_keeps_the_rewrite_and_queues_a_reembed():
    adapter = Adapter(_fact_items(1), _fact_repairs(1))
    embedder = Embedder(fail=True)
    result = _run(adapter, FakeLLM(("supported", "new fact")), embedder=embedder)
    assert result["rewritten"] == 1 and result["embedding_missing"] == 1
    call = adapter.applied[0]
    assert call["embedding_missing"] is True and "embedding" not in call
    assert adapter.items[("fact", "u0")]["text"] == "new fact"
    assert adapter.items[("fact", "u0")]["pending"] is False  # searchable again
    repair = adapter.repairs["k0"]
    assert repair["status"] == "embed_pending" and repair["attempts"] == 0
    # The rewrite is not a failed attempt, but an embedder outage makes the run a failure (F18-4).
    assert result["errors"] == 0
    assert result["stopped_embedding"] == "embedder_unavailable" and result[JOB_FAILED_KEY] is True


def test_reembed_on_a_later_run_finishes_the_repair():
    adapter = Adapter(_fact_items(1), _fact_repairs(1))
    llm = FakeLLM(("supported", "new fact"))
    _run(adapter, llm, embedder=Embedder(fail=True))
    embedder = Embedder()
    result = _run(adapter, llm, embedder=embedder)
    assert result["reembedded"] == 1 and result["processed"] == 0
    assert llm.calls == 1  # no second LLM call
    assert embedder.calls == ["new fact"]
    assert adapter.applied[-1]["reembed"] is True
    assert adapter.items[("fact", "u0")]["embedding"] == [0.5, 0.25]
    assert adapter.repairs["k0"]["status"] == "done"


def test_reembed_while_embedder_down_spends_nothing_and_stops():
    adapter = Adapter(_fact_items(2), _fact_repairs(2))
    llm = FakeLLM(("supported", "new fact"))
    _run(adapter, llm, embedder=Embedder(fail=True))
    assert {r["status"] for r in adapter.repairs.values()} == {"embed_pending"}
    for _ in range(MAX_ATTEMPTS + 2):
        embedder = Embedder(fail=True)
        result = _run(adapter, llm, embedder=embedder)
        assert len(embedder.calls) == 2  # stopped at the first failure (one embed, one probe)
        assert result["stopped_embedding"] == "embedder_unavailable"
        assert result[JOB_FAILED_KEY] is True
    assert all(
        r["status"] == "embed_pending" and r["attempts"] == 0 for r in adapter.repairs.values()
    )
    assert adapter.deferred  # the head rotates, so one bad item cannot pin the queue


def test_embedder_failure_skips_the_embedder_for_the_rest_of_the_run():
    adapter = Adapter(_fact_items(3), _fact_repairs(3))
    embedder = Embedder(fail=True)
    result = _run(adapter, FakeLLM(("supported", "new fact")), embedder=embedder)
    assert len(embedder.calls) == 2  # one embed, one probe that confirms the outage
    assert result["rewritten"] == 3 and result["embedding_missing"] == 3
    assert {r["status"] for r in adapter.repairs.values()} == {"embed_pending"}


def test_inline_item_fault_with_embedder_up_keeps_inline_embedding_on():
    adapter = Adapter(_fact_items(3), _fact_repairs(3))
    embedder = Embedder(fail_first=1)  # the first fact's embed fails; the probe and the rest work
    result = _run(adapter, FakeLLM(("supported", "new fact")), embedder=embedder)
    assert len(embedder.calls) == 4  # embed, probe, embed, embed
    assert result["rewritten"] == 3 and result["embedding_missing"] == 1
    assert "stopped_embedding" not in result and JOB_FAILED_KEY not in result
    assert [r["status"] for r in adapter.repairs.values()] == ["embed_pending", "done", "done"]


# --------------------------------------------------------------------------- item-specific re-embed
def _embed_pending(n, poison=("u0",)):
    """n 'embed_pending' repairs; the facts in ``poison`` have text the embedder rejects."""
    adapter = Adapter(_fact_items(n), _fact_repairs(n))
    _run(adapter, FakeLLM(("supported", "new fact")), embedder=Embedder(fail=True))
    assert {r["status"] for r in adapter.repairs.values()} == {"embed_pending"}
    for u in poison:
        adapter.items[("fact", u)]["text"] = "poison fact"
    return adapter


def _failure_rows(stage):
    from menhir.infrastructure.telemetry import telemetry_store

    with telemetry_store._connect() as conn:
        return conn.execute(
            "SELECT processing_attempt, details_json FROM failure_events "
            "WHERE operation = 'scheduler_derived_rederive' AND failure_stage = ?",
            (stage,),
        ).fetchall()


def test_reembed_item_fault_with_embedder_up_spends_an_attempt_and_continues():
    adapter = _embed_pending(3)
    embedder = Embedder(reject={"poison fact"})
    result = _run(adapter, FakeLLM(("supported", "new fact")), embedder=embedder)
    assert "stopped_embedding" not in result
    assert result["reembedded"] == 2 and result["errors"] == 1 and result["embed_failed"] == 0
    assert result[JOB_FAILED_KEY] is True  # a spent attempt, like the main pass
    poisoned = adapter.repairs["k0"]
    assert poisoned["status"] == "embed_pending" and poisoned["embed_attempts"] == 1
    assert poisoned["attempts"] == 0  # the LLM attempts are untouched
    assert "k0" in adapter.deferred
    assert [adapter.repairs[k]["status"] for k in ("k1", "k2")] == ["done", "done"]


def test_reembed_item_fault_gives_up_at_max_embed_attempts_with_one_event():
    adapter = _embed_pending(1)
    llm = FakeLLM(("supported", "new fact"))
    for i in range(MAX_EMBED_ATTEMPTS):
        result = _run(adapter, llm, embedder=Embedder(reject={"poison fact"}))
        assert result["embed_failed"] == (1 if i == MAX_EMBED_ATTEMPTS - 1 else 0)
    repair = adapter.repairs["k0"]
    assert repair["status"] == "embed_failed" and repair["embed_attempts"] == MAX_EMBED_ATTEMPTS
    item = adapter.items[("fact", "u0")]
    assert item["text"] == "poison fact" and item["pending"] is False  # rewrite kept
    assert item.get("embedding") is None
    rows = _failure_rows("reembed_attempts_exhausted")
    assert len(rows) == 1 and rows[0][0] is None
    # Terminal: later runs do not list it again.
    embedder = Embedder(reject={"poison fact"})
    later = _run(adapter, llm, embedder=embedder)
    assert embedder.calls == [] and JOB_FAILED_KEY not in later
    assert len(_failure_rows("reembed_attempts_exhausted")) == 1


def test_reembed_item_error_with_embedder_up_spends_an_attempt():
    class _Raising(Adapter):
        def load_derived_rederive_item(self, kind, uuid):
            if uuid == "u0" and self.repairs["k0"]["status"] == "embed_pending":
                raise RuntimeError("graph error on this edge")
            return super().load_derived_rederive_item(kind, uuid)

    adapter = _Raising(_fact_items(2), _fact_repairs(2))
    llm = FakeLLM(("supported", "new fact"))
    _run(adapter, llm, embedder=Embedder(fail=True))
    result = _run(adapter, llm, embedder=Embedder())
    assert result["reembedded"] == 1 and result["errors"] == 1
    assert "stopped_embedding" not in result
    assert adapter.repairs["k0"]["embed_attempts"] == 1
    assert adapter.repairs["k0"]["last_error"] == "RuntimeError"


def test_reembed_item_fault_with_embedder_down_stops_and_spends_nothing():
    adapter = _embed_pending(3)
    embedder = Embedder(fail=True)
    result = _run(adapter, FakeLLM(("supported", "new fact")), embedder=embedder)
    assert result["stopped_embedding"] == "embedder_unavailable" and result["errors"] == 0
    assert len(embedder.calls) == 2
    assert all(r.get("embed_attempts", 0) == 0 for r in adapter.repairs.values())


# --------------------------------------------------------------------------- events not deduped
def test_failure_events_reach_the_real_store_once_per_occurrence():
    """F18-1: the store dedups on (operation, subject, stage, classification, attempt, error); a
    non-episode event shares one subject and the error is redacted, so only processing_attempt
    None keeps distinct repairs' events apart."""
    adapter = Adapter(_fact_items(2), _fact_repairs(2))
    for key in ("k0", "k1"):
        _failed(adapter, key, REQUEUE_COOLDOWN_DAYS + 1)
    llm = FakeLLM(verdict=None)
    for _ in range(MAX_ATTEMPTS):
        _run(adapter, llm)
    requeued = _failure_rows("rederive_requeued")
    exhausted = _failure_rows("rederive_attempts_exhausted")
    assert len(requeued) == 2 and len(exhausted) == 2
    assert all(row[0] is None for row in requeued + exhausted)
    assert all('"requeue_count": 1' in row[1] for row in requeued)
    assert all(f'"attempts": {MAX_ATTEMPTS}' in row[1] for row in exhausted)


# --------------------------------------------------------------------------- requeue cap
def test_requeue_stops_at_max_requeues():
    adapter = Adapter(_fact_items(2), _fact_repairs(2))
    _failed(adapter, "k0", REQUEUE_COOLDOWN_DAYS + 1)
    _failed(adapter, "k1", REQUEUE_COOLDOWN_DAYS + 1)
    adapter.repairs["k0"]["requeue_count"] = MAX_REQUEUES
    adapter.repairs["k1"]["requeue_count"] = MAX_REQUEUES - 1
    result = _run(adapter, FakeLLM(("supported", "x")))
    assert result["requeued"] == 1
    assert adapter.repairs["k0"]["status"] == "failed"
    assert adapter.repairs["k1"]["status"] == "done"


def test_reembed_of_a_reflagged_or_gone_fact_is_obsolete():
    adapter = Adapter(_fact_items(2), _fact_repairs(2))
    llm = FakeLLM(("supported", "new fact"))
    _run(adapter, llm, embedder=Embedder(fail=True))
    adapter.items[("fact", "u0")]["pending"] = True  # a later erasure re-flagged it
    del adapter.items[("fact", "u1")]
    embedder = Embedder()
    result = _run(adapter, llm, embedder=embedder)
    assert result["obsolete"] == 2 and result["reembedded"] == 0
    assert embedder.calls == []  # never embeds a scrubbed or missing fact
    assert {r["status"] for r in adapter.repairs.values()} == {"obsolete"}


def test_without_an_embedder_the_repair_finishes_and_the_gap_is_counted():
    adapter = Adapter(_fact_items(1), _fact_repairs(1))
    result = _run(adapter, FakeLLM(("supported", "new fact")))
    assert result["embedding_missing"] == 1
    assert "embedding_missing" not in adapter.applied[0]
    assert adapter.repairs["k0"]["status"] == "done"


def test_entity_rewrite_never_counts_a_missing_embedding():
    adapter = Adapter({("entity", "u1"): _item()},
                      [{"repair_key": "k1", "item_kind": "entity", "item_uuid": "u1"}])
    result = _run(adapter, FakeLLM(("supported", "summary")), embedder=Embedder(fail=True))
    assert result["rewritten"] == 1 and result["embedding_missing"] == 0


# --------------------------------------------------------------------------- erasure replay
class _ReplayNeo4j:
    """Evaluates the replay query over in-memory repairs; it must not filter by status."""

    def __init__(self, repairs):
        self.repairs = repairs
        self.queries: list[str] = []

    def execute(self, query, params=None):
        self.queries.append(query)
        out = []
        for u in params["uuids"]:
            for r in self.repairs:
                if r["repair_key"].startswith(u + params["sep"]):
                    out.append({"uuid": r["item_uuid"]})
        return out


def test_erasure_replay_still_finds_flagged_items_in_every_repair_status():
    erased = "ep-erased"
    statuses = ["pending", "failed", "embed_pending", "done", "obsolete"]
    repairs = [
        {"repair_key": f"{erased}\x1fitem-{s}", "item_uuid": f"item-{s}", "status": s}
        for s in statuses
    ]
    repairs.append({"repair_key": "other\x1fitem-x", "item_uuid": "item-x", "status": "pending"})
    neo4j = _ReplayNeo4j(repairs)

    class _Self:
        def _fence_neo4j(self):
            return neo4j

    found = ErasureCoordinator._rederive_flagged_uuids(_Self(), {erased})
    assert found == {f"item-{s}" for s in statuses}
    assert "status" not in neo4j.queries[0]
