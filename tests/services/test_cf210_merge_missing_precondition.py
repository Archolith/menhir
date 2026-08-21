"""CF-210: `MergeCoordinator._apply` must fail closed on a missing precondition explicitly.

A request with no `expected_before_sha256` used to fall into `observed_fp != expected_before`,
which is always True because `observed_fp` is a hex digest and `expected_before` is `None`. The
behaviour was safe (quarantined, not mutated) but misreported real drift and left the safety
resting on an incidental type mismatch. The sibling coordinators (CF-21) separate the cases; this
pins that `MergeCoordinator` now does too.
"""

from __future__ import annotations

import pytest

from menhir.services.merge_coordinator import (
    WOULD_NEEDS_REVIEW,
    WOULD_REPLAY,
    MergeCoordinator,
    MergeDrift,
    merge_state_fingerprint,
)

pytestmark = pytest.mark.unit


class _FakeJournal:
    def _ensure_ready(self):
        return None

    def get(self, op_id):
        return {}

    def mark_needs_review(self, op_id, observed_error):
        self.reviewed = observed_error


class _FakeAdapter:
    def __init__(self, state):
        self.state = state

    def fetch_merge_state(self, survivor, absorbed):
        return self.state


def _coordinator(state=None):
    journal = _FakeJournal()
    adapter = _FakeAdapter(state if state is not None else {})
    return MergeCoordinator(graph_adapter=adapter, journal=journal), journal


def _base_request():
    return {
        "op_id": "op-1",
        "survivor_uuid": "s-1",
        "absorbed_uuid": "a-1",
        "similarity": 0.5,
    }


def test_missing_precondition_is_quarantined_without_drift():
    """The finding: a request with no `expected_before_sha256` is quarantined with a message naming
    the missing precondition, and NOT with a message containing the word 'drift'."""
    coordinator, journal = _coordinator()
    outcome, diag = coordinator._classify_replay(_base_request())
    assert outcome == WOULD_NEEDS_REVIEW
    assert "expected_before_sha256" in diag["observed_error"]
    assert "cannot verify precondition" in diag["observed_error"]
    assert "drift" not in diag["observed_error"]


def test_missing_precondition_raises_merge_drift_like_drift():
    """The exception type is the same one `reconcile` counts as quarantine: a missing precondition
    must raise `MergeDrift` (DRIFTED), not a generic failure (FAILED)."""
    coordinator, journal = _coordinator()
    with pytest.raises(MergeDrift):
        coordinator._apply_owned(_base_request())
    assert journal.reviewed is not None


def test_genuine_precondition_mismatch_still_reports_drift():
    """POSITIVE CONTROL: a precondition that genuinely does not match still quarantines with the
    drift message -- the fix must not turn real drift into a missing-precondition report."""
    coordinator, _ = _coordinator(state={"survivor_present": True})
    request = _base_request()
    request["expected_before_sha256"] = "e" * 64
    outcome, diag = coordinator._classify_replay(request)
    assert outcome == WOULD_NEEDS_REVIEW
    assert "drift" in diag["observed_error"]


def test_matching_precondition_gets_past_the_precondition_check():
    """POSITIVE CONTROL: a request whose precondition matches is not quarantined at the precondition
    check -- it proceeds (here to WOULD_REPLAY, the first non-quarantine outcome)."""
    state = {"survivor_present": True, "absorbed_present": True, "lineage_recorded": False}
    coordinator, _ = _coordinator(state=state)
    request = _base_request()
    request["expected_before_sha256"] = merge_state_fingerprint(state, op_id="op-1")
    outcome, diag = coordinator._classify_replay(request)
    assert outcome == WOULD_REPLAY
