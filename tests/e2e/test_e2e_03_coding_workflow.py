"""E2E-3 — integrated coding workflow.

Uses the generated fixture repo (``_harness/fixture_repo.py``), whose true import,
caller and test relationships are declared alongside its content as
``EXPECTED_IMPORTS`` / ``EXPECTED_BLAST_RADIUS`` / ``EXPECTED_AFFECTED_TESTS``.

The load-bearing criterion is the coverage caveat. ``UNINDEXED_PATH`` is a file the
fixture deliberately never contains: asking for its blast radius must produce an
explicit "not indexed / unknown coverage" answer, not an empty result. An empty result
reads as "nothing depends on this, safe to change", which is indistinguishable from a
correct answer right up until it authorizes a deletion. Issue #104 is the same shape --
a completeness answer derived from a tree the server could not observe.

WARNING, 2026-09-20: the snapshot read path landing in the working tree makes a
published canonical view win over local structure for the same project. That changes
what ``query_structure`` returns and therefore what this lane asserts. Pin the lane to a
project with no published view, and add an explicit assertion that no view is published,
or this lane will silently start testing the snapshot path instead of the local one.
"""

from __future__ import annotations

import pytest

from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.features import FeatureCombo
from tests.e2e._harness.pending import declare_pending

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(2400)]

CRITERIA = [
    "ingest_fixture_project_via_mcp",
    "query_project_files_symbols_context",
    "blast_radius_for_known_file",
    "affected_tests_for_known_file",
    "expected_import_relationships",
    "expected_caller_relationships",
    "unindexed_path_returns_coverage_caveat",
    "memory_with_git_diff_is_code_anchored",
    "code_context_recall_includes_anchored_memory",
    "restart_then_repeat_structure_queries",
    "no_published_snapshot_view_in_effect",
]


async def test_e2e_03_coding_workflow(
    running_stack,
    e2e_fixture_repo,
    feature_combo: FeatureCombo,
    lane_evidence: LaneEvidence,
) -> None:
    lane_evidence.record_stack(**e2e_fixture_repo.as_evidence())
    declare_pending(
        lane_evidence,
        CRITERIA,
        note="Fixture repo builds; assertions against EXPECTED_* tables not written yet.",
    )
