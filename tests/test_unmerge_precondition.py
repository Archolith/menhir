"""Unit tests for UnmergeCoordinator._apply's precondition guard (CF-21).

The saga freezes ``expected_before_sha256`` at PREPARE but never read it back, so a replay into a
graph that drifted since the snapshot would overwrite exactly the survivor state that Guard 2
(SURVIVOR_CHANGED_SINCE_MERGE) exists to protect. These tests drive ``_apply`` directly with stub
journal and graph_adapter doubles, so they run without a live graph.
"""

from __future__ import annotations

import pytest

from menhir.services.merge_coordinator import merge_state_fingerprint, MergeDrift
from menhir.services.unmerge_coordinator import UnmergeCoordinator

_MERGED = {"survivor_present": True, "absorbed_present": False, "lineage_recorded": True}
_RESTORED = {"survivor_present": True, "absorbed_present": True, "lineage_recorded": False}
_DRIFTED = {"survivor_present": True, "absorbed_present": True, "lineage_recorded": True}


class _Adapter:
    """Stub graph adapter: the graph's current merge-state is whatever the test sets it to."""

    def __init__(self, state, after_state=_RESTORED):
        self.state = dict(state)
        self.after_state = dict(after_state)
        self.restore_calls = 0

    def fetch_merge_state(self, survivor_uuid, absorbed_uuid):
        return dict(self.state)

    def restore_merge_snapshot(self, **kwargs):
        self.restore_calls += 1
        self.state = dict(self.after_state)
        return {"restored": 1}


class _Journal:
    """Stub journal recording quarantines/commits; never touches a real DB."""

    def __init__(self, expected_after):
        self.expected_after = expected_after
        self.review_calls = []
        self.commits = 0

    def _ensure_ready(self):
        pass

    def get(self, op_id):
        return {"expected_after_sha256": self.expected_after}

    def mark_needs_review(self, op_id, observed_error):
        self.review_calls.append((op_id, observed_error))

    def mark_committed(self, op_id):
        self.commits += 1

    def mark_reversed(self, op_id):
        pass


def _request(op_id="op1", **extra):
    base = {"op_id": op_id, "survivor_uuid": "s", "absorbed_uuid": "a", "merge_op_id": "merge-op-1"}
    base.update(extra)
    return base


def _coordinator(adapter_state, op_id, expected_before):
    expected_after = merge_state_fingerprint(_RESTORED, op_id=op_id)
    adapter = _Adapter(adapter_state)
    journal = _Journal(expected_after)
    coord = UnmergeCoordinator(graph_adapter=adapter, journal=journal)
    request = _request(op_id=op_id, expected_before_sha256=expected_before)
    return coord, adapter, journal, request


@pytest.mark.unit
def test_drift_quarantines_and_never_restores():
    op_id = "op-drift"
    expected_before = merge_state_fingerprint(_MERGED, op_id=op_id)
    coord, adapter, journal, request = _coordinator(_DRIFTED, op_id, expected_before)

    with pytest.raises(MergeDrift):
        coord._apply(request, {})

    assert journal.review_calls, "drift must quarantine to NEEDS_REVIEW"
    assert journal.review_calls[0][0] == op_id
    assert adapter.restore_calls == 0, "drifted graph must NEVER be mutated"


@pytest.mark.unit
def test_missing_precondition_fails_closed_and_never_restores():
    op_id = "op-missing"
    # No expected_before_sha256 in the request at all.
    adapter = _Adapter(_MERGED)
    journal = _Journal(merge_state_fingerprint(_RESTORED, op_id=op_id))
    coord = UnmergeCoordinator(graph_adapter=adapter, journal=journal)
    request = _request(op_id=op_id)

    with pytest.raises(MergeDrift):
        coord._apply(request, {})

    assert journal.review_calls, "missing precondition must quarantine to NEEDS_REVIEW"
    assert journal.review_calls[0][0] == op_id
    assert adapter.restore_calls == 0, "an unverifiable request must never mutate"


@pytest.mark.unit
def test_matching_precondition_still_applies():
    op_id = "op-match"
    expected_before = merge_state_fingerprint(_MERGED, op_id=op_id)
    coord, adapter, journal, request = _coordinator(_MERGED, op_id, expected_before)

    out = coord._apply(request, {})

    assert adapter.restore_calls == 1, "matching precondition must still restore"
    assert not journal.review_calls, "no quarantine on a matching precondition"
    assert out["restored"] == 1


@pytest.mark.unit
def test_already_applied_replay_still_short_circuits():
    op_id = "op-replay"
    # Observed == expected AFTER-state: a previous attempt crashed before COMMITTED.
    coord, adapter, journal, request = _coordinator(_RESTORED, op_id, expected_before=None)
    request = _request(op_id=op_id, expected_before_sha256="irrelevant")

    out = coord._apply(request, {})

    assert out["restored"] == 1 and out.get("replayed") is True
    assert adapter.restore_calls == 0, "already-applied replay must NOT mutate"
    assert not journal.review_calls, "already-applied replay is a success, not a quarantine"
