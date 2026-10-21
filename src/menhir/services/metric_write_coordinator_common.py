"""Shared seams and pure helpers for the metric_write_coordinator facade split.

``MetricGraphAdapter`` and ``RunTallyRecorder`` are the two protocols the coordinator is built on,
``MetricDrift`` / ``MetricChainConflict`` are the saga's failure vocabulary, and the digest /
fingerprint helpers are the pure crypto shared by both halves of the saga: the write path freezes
fingerprints into the request at PREPARE, and the replay path re-derives them to classify a row.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol


class MetricGraphAdapter(Protocol):
    """The graph surface the coordinator needs (narrow, so producers can be faked in tests)."""

    def record_metric(self, **kwargs: Any) -> dict[str, Any]: ...
    def fetch_metric(
        self, *, subject: str, counter: str, namespace: str | None = None
    ) -> dict[str, Any] | None: ...
    def fetch_metric_state(self, *, view_key: str) -> dict[str, Any] | None:
        """Full PROTECTED state (uuid/value/type/labels/view_current/receipt_op) for fingerprinting.

        Distinct from fetch_metric (the public counter projection): the saga's precondition and
        after-state checks need labels and the type stamp, or a corrupted node fingerprints clean.
        """
        ...


class RunTallyRecorder(Protocol):
    """The narrow surface handed to perception/correction (plan A6).

    They get ONLY this -- not the telemetry store, not the graph journal -- so an instrumentation
    call site cannot bypass the saga or reach the graph directly.
    """

    def record_run_tally(
        self, *, subject: str, counter: str, value: float, namespace: str | None = ...,
        run_id: str | None = ...,
    ) -> dict[str, Any]: ...


def _canonical(payload: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace drift. The basis of every digest."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chain_digest(previous_digest: str | None, delta_row_ids: list[int], aggregate: float) -> str:
    """Chained accumulator digest (plan C4): H(previous_digest || canonical(delta) || aggregate).

    Chaining the PRIOR digest (not re-reading history) is what lets the raw rows be pruned while
    the lineage stays verifiable: each link commits to everything before it.
    """
    return _sha256(
        _canonical(
            {
                "prev": previous_digest or "",
                "delta_row_ids": sorted(int(x) for x in delta_row_ids),
                "aggregate": float(aggregate),
            }
        )
    )


#: Fingerprint of "no current Metric exists for this key" -- a legitimate before-state.
ABSENT = "absent"


def state_fingerprint(state: dict[str, Any] | None, *, view_key: str) -> str:
    """Fingerprint of a Metric node's PROTECTED state (plan E3).

    Covers everything the operation owns -- identity, key, value, LABEL SET, type stamp,
    currentness, and the receipt pointer -- so a node whose label or type was corrupted does NOT
    fingerprint as correct. Deliberately excludes volatile access timestamps (last_accessed), which
    change without the operation doing anything.

    ``None`` (no current Metric) fingerprints as ABSENT: the expected before-state of a first write.
    """
    if state is None:
        return _sha256(_canonical({"absent": True, "view_key": view_key}))
    return _sha256(
        _canonical(
            {
                "uuid": str(state.get("uuid") or ""),
                "view_key": view_key,
                "value": float(state.get("value") or 0.0),
                "type": str(state.get("type") or ""),
                "labels": sorted(str(x) for x in (state.get("labels") or [])),
                "view_current": bool(state.get("view_current", True)),
                "receipt_op_id": str(state.get("receipt_op_id") or ""),
                # Current-set cardinality (plan E, Phase 2). A changed write must leave EXACTLY one
                # current version; expected-after asserts 1. Folding it into the fingerprint means a
                # graph with two currents cannot match the expected after-state and routes to
                # NEEDS_REVIEW. Defaults to 1 so a caller/state without the field (e.g. a test fake
                # with a single current) fingerprints identically to the one-current graph read.
                "current_count": int(state.get("current_count", 1)),
            }
        )
    )


class MetricDrift(RuntimeError):
    """The graph is in neither the expected before-state nor the expected after-state."""


class MetricChainConflict(RuntimeError):
    """A concurrent fold committed onto the chain head this fold was accumulating from (CF-244).

    Raised BEFORE any journal row, receipt or graph mutation, so nothing was written and the fold
    is safe to retry: it will re-read the new head and accumulate from there. Distinct from
    `MetricDrift`, which means the GRAPH moved under a prepared operation and needs review.
    """
