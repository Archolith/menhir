"""Helper for lanes that are scaffolded but not yet implemented.

A bare ``pytest.skip`` would be dangerous here. The release plan says plainly that "a
skip is not a pass", and Gate C requires knowing *which checklist items* were proven --
so an unimplemented lane that simply vanishes from the report is indistinguishable from
one that ran. :func:`declare_pending` therefore writes a full evidence directory first,
recording every acceptance criterion as UNPROVEN, and only then skips.

The result is that ``result.json`` for an unimplemented lane says
``status: "PENDING", criteria_passed: 0`` with every criterion named -- which a gate
report can count, and a human reading the evidence tree cannot mistake for success.
"""

from __future__ import annotations

import pytest

from tests.e2e._harness.evidence import LaneEvidence

__all__ = ["declare_pending"]


def declare_pending(evidence: LaneEvidence, criteria: list[str], *, note: str = "") -> None:
    """Record every criterion as unproven, close the evidence, and skip the lane."""

    for criterion in criteria:
        evidence.record(criterion, passed=False, detail="not implemented")
    evidence.manifest["pending_note"] = note or "lane scaffolded; assertions not implemented"
    evidence.close(status="PENDING")
    pytest.skip(
        f"{evidence.lane}: scaffolded, not implemented "
        f"({len(criteria)} acceptance criteria unproven). {note}"
    )
