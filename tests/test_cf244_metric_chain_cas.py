"""CF-244: two concurrent folds chained from the same head, forking the chain and losing a delta.

`record_telemetry_fold` reads the chain head OUTSIDE any transaction:

    prior = self.receipts.latest_for_key(key)      # no transaction
    aggregate = float(prior["aggregate_value"]) + delta_count
    digest = chain_digest(prior["input_digest"], delta_row_ids, aggregate)
    return self._saga_write(..., previous_receipt_op_id=prior["op_id"], ...)

and `receipts.append` was a plain INSERT: `previous_receipt_op_id` was recorded as a chain POINTER,
never checked as a PRECONDITION.

The window is wider than it looks because `latest_for_key` counts only COMMITTED operations. A
concurrent fold that has already written its receipt at PREPARE is invisible to it, so the second
fold reads the same parent, and both commit claiming it. The later aggregate overwrites the
earlier one -- one delta silently gone -- and the digest chain, which exists precisely to make the
fold auditable after the raw rows are pruned, has two receipts claiming one parent.

REACHABILITY, stated honestly. Both production callers are scheduler jobs (`scheduler_tasks.py`),
and `_run_due_jobs` runs jobs sequentially under the scheduler lease, so a single healthy
leaseholder cannot produce this interleaving. It needs two processes believing they hold the lease
-- which is the state CF-240 fixed and CF-241 still permits via `force_takeover`. The store's own
docstring already says the invariant "should hold BY CONSTRUCTION, not by fence discipline", and
that is what these tests pin: the fold is now safe on its own terms rather than because the
scheduler happens to serialize it.

These tests drive the coordinator against a real SQLite file, because the defect and the fix both
live in transaction semantics. A fake receipt store would test the assertion, not the guarantee.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.metric_receipts import MetricReceiptStore
from menhir.services.metric_write_coordinator import (
    MetricChainConflict,
    MetricWriteCoordinator,
)

pytestmark = pytest.mark.unit


class _Graph:
    """Mimics the :Metric surface the coordinator drives.

    Shaped after the proven fake in `tests/test_experience_counters_task.py` -- note the real
    `record_metric` is called with `node_uuid=` / `receipt_op_id=`, not `metric_uuid=` / `op_id=`.
    Getting those names wrong makes every write fingerprint as after-state drift.
    """

    def __init__(self) -> None:
        self.metrics: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _key(namespace: str | None, subject: str, counter: str) -> str:
        return f"{(namespace or '').strip()}::{subject.strip().lower()}::{counter.strip().lower()}"

    def record_metric(
        self, *, subject, counter, value, receipt_op_id, namespace=None, node_uuid=None, **_
    ) -> dict[str, Any]:
        key = self._key(namespace, subject, counter)
        existing = self.metrics.get(key)
        uuid = (
            existing["uuid"]
            if (existing and float(existing["value"]) == float(value))
            else (node_uuid or f"m{len(self.metrics)}")
        )
        self.metrics[key] = {
            "uuid": uuid, "value": float(value), "receipt_op_id": receipt_op_id
        }
        return {"uuid": uuid, "view_key": key, "created": existing is None}

    def fetch_metric(self, *, subject, counter, namespace=None) -> dict[str, Any] | None:
        got = self.metrics.get(self._key(namespace, subject, counter))
        return dict(got) if got else None

    def fetch_metric_state(self, *, view_key: str) -> dict[str, Any] | None:
        got = self.metrics.get(view_key)
        if not got:
            return None
        return {
            "uuid": got["uuid"], "value": float(got["value"]), "type": "METRIC",
            "labels": ["Metric"], "view_current": True,
            "receipt_op_id": got.get("receipt_op_id"),
        }


def _coordinator(tmp_path) -> MetricWriteCoordinator:
    db = tmp_path / "telemetry.db"
    return MetricWriteCoordinator(
        graph_adapter=_Graph(),
        journal=GraphOperationsJournal(db_path=db),
        receipts=MetricReceiptStore(db_path=db),
        telemetry_db_path=db,
    )


def _fold(coord: MetricWriteCoordinator, *, rows: list[int], cutoff: int) -> dict[str, Any]:
    return coord.record_telemetry_fold(
        subject="ops",
        counter="failures",
        source_table="failure_events",
        grouping={"operation": "recall"},
        cutoff_id=cutoff,
        delta_row_ids=rows,
        delta_count=len(rows),
    )


def _receipts(coord: MetricWriteCoordinator, key: str) -> list[dict[str, Any]]:
    with sqlite3.connect(coord.telemetry_db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM metric_receipts WHERE view_key = ? ORDER BY rowid", (key,)
            )
        ]


# ---------------------------------------------------------------------------
# the counterexample
# ---------------------------------------------------------------------------


def test_a_second_fold_chained_from_a_superseded_head_is_refused(tmp_path) -> None:
    """THE COUNTEREXAMPLE. Fold B computed its aggregate from head H, but A committed on top of H
    first. B must not append: its value was derived from a parent that is no longer the head.

    The interleaving is constructed by capturing the head BEFORE A runs and handing it back to
    `_saga_write` afterwards -- which is exactly the stale `prior` the unsynchronised read
    produces, without needing two OS threads to collide on a millisecond."""
    coord = _coordinator(tmp_path)
    key = coord.view_key(coord.namespace, "ops", "failures")

    _fold(coord, rows=[1, 2], cutoff=2)
    head_before = coord.receipts.latest_for_key(key)
    assert head_before is not None

    # A commits on top of `head_before`.
    _fold(coord, rows=[3], cutoff=3)

    # B still believes `head_before` is the head -- the read that produced it was not in a
    # transaction and only counts COMMITTED ops.
    with pytest.raises(MetricChainConflict) as caught:
        coord._saga_write(
            subject="ops",
            counter="failures",
            value=99.0,
            namespace=coord.namespace,
            receipt_kind="TELEMETRY_FOLD",
            source="failure-telemetry",
            run_id=None,
            source_table="failure_events",
            grouping={"operation": "recall"},
            cutoff_id=4,
            receipt_row_ids=[4],
            previous_receipt_op_id=str(head_before["op_id"]),
            input_digest="stale",
            source_row_count=1,
            enforce_chain_head=True,
        )

    assert "chain head moved" in str(caught.value)


def test_the_refused_fold_wrote_nothing_at_all(tmp_path) -> None:
    """A conflict must be detected BEFORE the journal row and the receipt, or the retry that the
    exception invites would double-count. Asserts the stores are byte-identical across the
    refusal."""
    coord = _coordinator(tmp_path)
    key = coord.view_key(coord.namespace, "ops", "failures")

    _fold(coord, rows=[1, 2], cutoff=2)
    head_before = coord.receipts.latest_for_key(key)
    _fold(coord, rows=[3], cutoff=3)

    before_rows = _receipts(coord, key)
    with sqlite3.connect(coord.telemetry_db_path) as conn:
        ops_before = conn.execute("SELECT count(*) FROM graph_operations").fetchone()[0]

    with pytest.raises(MetricChainConflict):
        coord._saga_write(
            subject="ops", counter="failures", value=99.0, namespace=coord.namespace,
            receipt_kind="TELEMETRY_FOLD", source="failure-telemetry", run_id=None,
            source_table="failure_events", grouping=None, cutoff_id=4,
            receipt_row_ids=[4], previous_receipt_op_id=str(head_before["op_id"]),
            input_digest="stale", source_row_count=1, enforce_chain_head=True,
        )

    with sqlite3.connect(coord.telemetry_db_path) as conn:
        ops_after = conn.execute("SELECT count(*) FROM graph_operations").fetchone()[0]
    assert _receipts(coord, key) == before_rows
    assert ops_after == ops_before


def test_the_chain_stays_linear_and_no_delta_is_lost(tmp_path) -> None:
    """The property the digest chain exists for: every receipt names a distinct parent, and the
    aggregate accounts for every delta. This is what a fork breaks."""
    coord = _coordinator(tmp_path)
    key = coord.view_key(coord.namespace, "ops", "failures")

    _fold(coord, rows=[1, 2], cutoff=2)
    _fold(coord, rows=[3], cutoff=3)
    _fold(coord, rows=[4, 5, 6], cutoff=6)

    rows = _receipts(coord, key)
    parents = [r["previous_receipt_op_id"] for r in rows]

    assert len(parents) == len(set(parents)), f"chain forked: two receipts share a parent {parents}"
    assert parents[0] is None
    assert parents[1] == rows[0]["op_id"]
    assert parents[2] == rows[1]["op_id"]
    assert float(rows[-1]["aggregate_value"]) == 6.0  # 2 + 1 + 3, nothing dropped


# ---------------------------------------------------------------------------
# positive controls -- the fix must not refuse valid work
# ---------------------------------------------------------------------------


def test_sequential_folds_still_succeed(tmp_path) -> None:
    """POSITIVE CONTROL, the one that matters most: a guard that refused everything would satisfy
    every assertion above."""
    coord = _coordinator(tmp_path)

    key = coord.view_key(coord.namespace, "ops", "failures")

    _fold(coord, rows=[1, 2], cutoff=2)
    _fold(coord, rows=[3], cutoff=3)

    rows = _receipts(coord, key)
    assert len(rows) == 2
    assert [float(r["aggregate_value"]) for r in rows] == [2.0, 3.0]


def test_the_first_fold_on_an_empty_chain_succeeds(tmp_path) -> None:
    """POSITIVE CONTROL: `previous_receipt_op_id` is None and the head is genuinely absent. The
    check must treat that as a MATCH, not as a mismatch against None."""
    coord = _coordinator(tmp_path)

    key = coord.view_key(coord.namespace, "ops", "failures")

    _fold(coord, rows=[1], cutoff=1)

    rows = _receipts(coord, key)
    assert len(rows) == 1
    assert float(rows[0]["aggregate_value"]) == 1.0
    assert rows[0]["previous_receipt_op_id"] is None


def test_a_first_fold_is_refused_if_a_chain_appeared_meanwhile(tmp_path) -> None:
    """The None case in the other direction: a fold that believed the chain was empty must be
    refused once someone else has established a head. Without this, the very first concurrent
    pair -- the likeliest collision, at startup -- would still fork."""
    coord = _coordinator(tmp_path)
    _fold(coord, rows=[1], cutoff=1)

    with pytest.raises(MetricChainConflict):
        coord._saga_write(
            subject="ops", counter="failures", value=5.0, namespace=coord.namespace,
            receipt_kind="TELEMETRY_FOLD", source="failure-telemetry", run_id=None,
            source_table="failure_events", grouping=None, cutoff_id=2,
            receipt_row_ids=[2], previous_receipt_op_id=None,
            input_digest="stale", source_row_count=1, enforce_chain_head=True,
        )


def test_run_tally_is_not_subject_to_the_chain_check(tmp_path) -> None:
    """POSITIVE CONTROL for the opt-in. RUN_TALLY does not derive its value from the chain, so
    enforcing head equality there would reject writes that are correct. Two tallies in a row must
    both land."""
    coord = _coordinator(tmp_path)

    key = coord.view_key(coord.namespace, "perception", "abstains")

    coord.record_run_tally(subject="perception", counter="abstains", value=1.0)
    coord.record_run_tally(subject="perception", counter="abstains", value=2.0)

    rows = _receipts(coord, key)
    assert len(rows) == 2
    assert [float(r["aggregate_value"]) for r in rows] == [1.0, 2.0]


def test_the_recompute_path_is_not_subject_to_the_chain_check(tmp_path) -> None:
    """POSITIVE CONTROL for the opt-in, second half. The recompute path derives its aggregate from
    the source rows, not from the prior receipt, so it is correct whatever the head is."""
    coord = _coordinator(tmp_path)
    _fold(coord, rows=[1, 2], cutoff=2)

    key = coord.view_key(coord.namespace, "ops", "failures")

    coord.record_telemetry_absolute(
        subject="ops", counter="failures", source_table="failure_events",
        grouping=None, cutoff_id=9, row_ids=[1, 2, 3], absolute_count=3,
    )

    rows = _receipts(coord, key)
    assert len(rows) == 2
    assert float(rows[-1]["aggregate_value"]) == 3.0
