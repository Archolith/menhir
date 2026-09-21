"""E2E-8 — isolation and adversarial cases.

Carries the permanent regression pin for #88: two namespaces sharing one session_id
with similar content must stay isolated. #88 closed on a live-LLM reproducer at
9f101e29 (both namespaces READY, own entities, same-group MENTIONS, zero cross-group
MENTIONS); this lane is where that assertion lives for the release.

Every criterion here is a negative test. Per Gate C, a negative test that fails is not a
lane failure to be retried -- it is a release-blocking issue with a reproduced failure.

Acceptance criteria are the ``CRITERIA`` list below, taken from the approved release
plan's Phase C section.
"""

from __future__ import annotations

import pytest

from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.features import FeatureCombo
from tests.e2e._harness.pending import declare_pending

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(3000)]

CRITERIA = [
    "same_session_id_two_namespaces_stay_isolated",
    "same_filenames_two_projects_do_not_cross_link",
    "malformed_artifact_metadata_fails_without_corruption",
    "oversized_memory_or_diff_refused_as_documented",
    "capped_scan_does_not_authorize_destructive_prune",
    "stale_or_unknown_coverage_never_reports_safe",
    "invalid_beacon_input_does_not_clobber_manifest",
    "provider_failure_does_not_silently_pass",
]


async def test_e2e_08_isolation_adversarial(
    running_stack,
    feature_combo: FeatureCombo,
    lane_evidence: LaneEvidence,
) -> None:
    declare_pending(lane_evidence, CRITERIA, note="The #88 isolation pin needs a live LLM; the rest do not. Split accordingly.")
