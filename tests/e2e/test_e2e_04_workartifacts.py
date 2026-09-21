"""E2E-4 — WorkArtifact lifecycle.

Uses a fixture artifact corpus committed to Git.

The stable-UUID criterion is the one with teeth: move an artifact file, run
audit/reconcile, and the identity must survive. Issue #104 and the snapshot plan both
record the same class of failure -- a corpus audit that scanned a tree it could not see
and confidently reported 190 of 202 sources missing. This lane must therefore also
assert that an unreadable corpus path is an ERROR, never a clean parity report.

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
    "validate_fixture_corpus",
    "read_list_artifact_via_mcp",
    "link_two_artifacts",
    "legal_state_transition",
    "illegal_transition_rejected",
    "supersede_direction_and_status",
    "moved_file_keeps_stable_uuid",
    "unreadable_corpus_path_is_error_not_clean_parity",
    "restart_preserves_relationships_status_source",
]


async def test_e2e_04_workartifacts(
    running_stack,
    feature_combo: FeatureCombo,
    lane_evidence: LaneEvidence,
) -> None:
    declare_pending(lane_evidence, CRITERIA, note="Needs a committed fixture artifact corpus; not yet built.")
