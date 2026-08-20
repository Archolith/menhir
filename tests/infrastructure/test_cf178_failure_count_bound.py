"""CF-178(a): the telemetry failure-count dict must not grow without bound.

`_TELEMETRY_FAILURE_COUNTS` exists to suppress log spam from repeated telemetry write failures.
Five of its ten key formats embed a uuid or call id, so their cardinality is unbounded --
confirmed at source:

    recorders.py:68   record_llm_usage_event:{event.call_id}
    recorders.py:271  record_episode_task_event:{phase}:{episode_uuid or 'unknown'}
    recorders.py:333  record_lifecycle_action:{action}:{node_uuid}
    recorders.py:368  record_merge:{survivor_uuid}:{absorbed_uuid}
    recorders.py:395  record_memory_revision:{node_uuid}:{field}

Growth is gated on telemetry WRITE FAILURE, which is CF-144's `database is locked` contention
mode -- so the structure meant to suppress spam under contention was the one growing without
bound under contention.

`_TELEMETRY_FAILURE_COUNTS` is module-level global state and the suite runs with `-n 8`, so
every test here restores it.
"""

from __future__ import annotations

import pytest

from menhir.infrastructure.telemetry import recorders


@pytest.fixture(autouse=True)
def _isolate_failure_counts():
    """Snapshot and restore the module-level dict so these tests cannot leak into the suite."""
    saved = dict(recorders._TELEMETRY_FAILURE_COUNTS)
    recorders._TELEMETRY_FAILURE_COUNTS.clear()
    yield
    recorders._TELEMETRY_FAILURE_COUNTS.clear()
    recorders._TELEMETRY_FAILURE_COUNTS.update(saved)


def _fail(key: str) -> None:
    """Drive the real shared sink every one of the ten call sites routes through."""
    recorders._log_telemetry_persist_failure(key, RuntimeError("database is locked"))


@pytest.mark.unit
def test_small_key_sets_are_retained_and_counted() -> None:
    """POSITIVE CONTROL: without this, the cap test would pass against a dict that stored nothing."""
    for _ in range(3):
        _fail("record_merge:aaa:bbb")
    _fail("record_merge:ccc:ddd")

    counts = recorders._TELEMETRY_FAILURE_COUNTS
    assert counts["record_merge:aaa:bbb"] == 3, "repeat failures must still accumulate"
    assert counts["record_merge:ccc:ddd"] == 1
    assert len(counts) == 2


@pytest.mark.unit
def test_unbounded_key_cardinality_stays_capped() -> None:
    """The defect: one distinct key per failing uuid, none ever removed."""
    cap = recorders._TELEMETRY_FAILURE_COUNTS_MAX
    for i in range(cap * 2):
        _fail(f"record_memory_revision:node-{i}:field")

    assert len(recorders._TELEMETRY_FAILURE_COUNTS) <= cap


@pytest.mark.unit
def test_the_oldest_key_is_the_one_evicted() -> None:
    cap = recorders._TELEMETRY_FAILURE_COUNTS_MAX
    for i in range(cap):
        _fail(f"record_lifecycle_action:delete:node-{i}")

    assert "record_lifecycle_action:delete:node-0" in recorders._TELEMETRY_FAILURE_COUNTS
    assert len(recorders._TELEMETRY_FAILURE_COUNTS) == cap

    _fail("record_lifecycle_action:delete:node-OVERFLOW")

    counts = recorders._TELEMETRY_FAILURE_COUNTS
    assert len(counts) == cap
    assert "record_lifecycle_action:delete:node-0" not in counts, "oldest key should be evicted"
    assert "record_lifecycle_action:delete:node-OVERFLOW" in counts
    assert "record_lifecycle_action:delete:node-1" in counts, "only ONE key should be evicted"


@pytest.mark.unit
def test_an_existing_key_never_triggers_eviction() -> None:
    """At capacity, re-failing a KNOWN key must not evict anything -- it is not a new entry."""
    cap = recorders._TELEMETRY_FAILURE_COUNTS_MAX
    for i in range(cap):
        _fail(f"record_merge:s-{i}:a")

    _fail("record_merge:s-0:a")  # already present; must not push anything out

    counts = recorders._TELEMETRY_FAILURE_COUNTS
    assert len(counts) == cap
    assert counts["record_merge:s-0:a"] == 2
    assert "record_merge:s-1:a" in counts


@pytest.mark.unit
def test_a_real_recorder_failure_reaches_the_bounded_dict() -> None:
    """CALLER BOUNDARY: prove a production recorder actually routes into this structure.

    Testing `_log_telemetry_persist_failure` alone would still pass if no recorder called it.
    """

    class _ExplodingStore:
        def record_memory_revision(self, **_kwargs) -> None:
            raise RuntimeError("database is locked")

    recorders.record_memory_revision(
        node_uuid="node-xyz",
        field="content",
        old_value="a",
        new_value="b",
        changed_by="test",
        store=_ExplodingStore(),
    )

    assert "record_memory_revision:node-xyz:content" in recorders._TELEMETRY_FAILURE_COUNTS
