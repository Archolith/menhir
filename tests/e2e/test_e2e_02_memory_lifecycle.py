"""E2E-2 — memory lifecycle. Also carries #118's remaining MVP acceptance.

Per tracking issue #123, #118's outstanding obligation *is* this lane: prove the
documented original-write -> status observation -> recall/context workflow through real
stdio, including timeout/failure and restart. It is not a separate feature project.

Acceptance criteria (Phase C, E2E-2) are the ``CRITERIA`` list below.

NOTE ON PROVIDERS: enrichment needs a real LLM/embedder to reach READY. This lane must
therefore either receive provider credentials deliberately (see ``child_environment``,
which withholds them by default so an accidental live call fails loudly rather than
spending budget) or assert only the states reachable without one -- and say which it
did in its evidence. A lane that quietly degrades to "accepted" and calls that a pass
would satisfy the checklist without testing the workflow.
"""

from __future__ import annotations

import pytest

from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.features import FeatureCombo
from tests.e2e._harness.pending import declare_pending

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(2400)]

CRITERIA = [
    "add_durable_memory",
    "observe_accepted_processing_ready",
    "tracked_write_receipt_survives_diagnostic_failure",  # #118 / PR #122
    "recall_by_direct_wording",
    "recall_by_paraphrase",
    "build_context_from_memory",
    "correction_current_vs_historical",
    "provenance_points_to_source_episode",
    "restart_then_recall_again",
]


async def test_e2e_02_memory_lifecycle(
    running_stack,
    feature_combo: FeatureCombo,
    lane_evidence: LaneEvidence,
) -> None:
    declare_pending(
        lane_evidence,
        CRITERIA,
        note=(
            "Implement against #118's merged tracked-write path (PR #122). Requires a "
            "provider decision: see module docstring."
        ),
    )
