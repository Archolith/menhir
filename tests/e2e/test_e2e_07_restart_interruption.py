"""E2E-7 — restart and interrupted work.

"No false READY" is the criterion this lane exists for. A process killed mid-enrichment
must not leave a record that claims completion, and the recovered state must be one of:
eventual READY, explicit FAILED, or a documented recoverable state. "Probably finished"
is not one of them.

Kill the backend with SIGKILL, not terminate(): a graceful stop exercises the shutdown
path, which is E2E-1's job. This lane needs the ungraceful one.

Acceptance criteria are the ``CRITERIA`` list below, taken from the approved release
plan's Phase C section.
"""

from __future__ import annotations

import pytest

from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.features import FeatureCombo
from tests.e2e._harness.pending import declare_pending

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(2400)]

CRITERIA = [
    "kill_during_queued_enrichment",
    "no_false_ready_after_restart",
    "eventual_ready_or_explicit_failed_or_documented_recoverable",
    "restart_recovers_structure_artifacts_todos",
    "idempotent_rerun_of_same_fixture",
]


async def test_e2e_07_restart_interruption(
    running_stack,
    feature_combo: FeatureCombo,
    lane_evidence: LaneEvidence,
) -> None:
    declare_pending(lane_evidence, CRITERIA, note="Requires a kill-and-restart helper on BackendProcess; terminate() is graceful by design.")
