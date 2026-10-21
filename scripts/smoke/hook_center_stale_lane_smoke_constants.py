"""Shared constants for the Hook Center stale-anchor lane smoke harness.

Extracted verbatim from ``hook_center_stale_lane_smoke.py`` so the facade and the
``hook_center_stale_lane_smoke_*`` sibling modules share one definition of each
value. The facade re-imports every name, so attribute access on the original
module (used by the unit tests) keeps working unchanged.
"""

from __future__ import annotations

RESULT_PASS = "PASS"
RESULT_PASS_WITH_SKIPS = "PASS_WITH_SKIPS"
RESULT_FAIL = "FAIL"

#: how long main() waits for the (self-served) server to answer; overridable by tests.
SERVER_READY_TIMEOUT_S = 30.0

DEFAULT_PROJECT = "smoke-hook-center-stale-lane"
DEFAULT_PATH = "src/smoke_target.py"
DEFAULT_MEMORY_UUID = "smoke-memory-001"
CONTROL_UUID = "smoke-memory-control-001"
WRONG_PATH = "src/other_file.py"
ANCHORED_AT = "2026-07-01T00:00:00Z"
QUERY = "smoke target behavior stale anchor"
# A unique token embedded in the fixture memory body. It appears when the memory is
# rendered into context, but never in the generated stale-warning prose — so the
# atomicity check can distinguish "memory present" from "warning present".
MEMORY_SENTINEL = "smoke-lane-memory-body-sentinel-7c1f"
EVENT_HASH = "smoke-synthetic-hash"

CHECK_KEYS = (
    "tool_event_accepted",
    "dirty_file_visible",
    "stale_anchor_visible",
    "recall_stale_label",
    "formatter_stale_advisory",
    "context_warning_atomic",
    "verification_receipt_recorded",
    "post_dirty_receipt_enriches",
    "wrong_path_receipt_ignored",
    "pre_dirty_receipt_ignored",
    "malformed_timestamp_conservative",
    "outdated_receipt_recommends_no_lifecycle_mutation",
)
