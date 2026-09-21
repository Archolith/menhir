"""E2E-5 — TODO lifecycle.

The repeat-close criterion is the interesting one: closing an already-closed TODO
must be safe and say so, rather than silently succeeding or corrupting state.

Acceptance criteria are the ``CRITERIA`` list below, taken from the approved release
plan's Phase C section.
"""

from __future__ import annotations

import pytest

from tests.e2e._harness.evidence import LaneEvidence
from tests.e2e._harness.features import FeatureCombo
from tests.e2e._harness.pending import declare_pending

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(1800)]

CRITERIA = [
    "add_repository_relative_todo",
    "list_and_read_todo",
    "location_and_project_metadata",
    "close_todo",
    "open_closed_filtering_semantics",
    "repeat_close_is_safe",
    "invalid_close_is_safe",
    "restart_persistence",
]


async def test_e2e_05_todos(
    running_stack,
    feature_combo: FeatureCombo,
    lane_evidence: LaneEvidence,
) -> None:
    declare_pending(lane_evidence, CRITERIA, note="Straightforward lane; implement early since it needs no provider.")
