"""CF-122: `status_from_header` skipped every width between `len(words)` and 2.

The loop was `for width in (len(words), 2, 1)`. `"ready for review"` is the only three-word key in
`STATUS_HEADER_ALIASES`, so any header that survived the delimiter split with four or more words
could never reach it -- width 3 was simply never tried. A wrapup declaring
`Status: READY FOR REVIEW pending orchestrator` was registered as DRAFT, with no conflict and no
warning, because `_plan_registrations` falls back to `INITIAL_STATUS[IMPLEMENTATION_REPORT]`.

The docstring already described the correct algorithm -- "matching walks from the whole string
inward to the first word and stops at the first hit" -- so the fix is to make the loop do that.

Two things about `artifact_type` that make this finding easy to mis-triage:

* `implementation_report` is the only type whose `valid_statuses` includes READY_FOR_REVIEW, so
  the finding can only be reproduced against that type.
* Any other type returns `status_invalid_for_type`, which LOOKS like a refutation and is not. The
  two reason codes distinguish "alias matched, wrong type" from "alias never matched"; only the
  second is this finding.
"""

from __future__ import annotations

import pytest

from menhir.domain.work_artifact import (
    STATUS_HEADER_ALIASES,
    ArtifactStatus,
    status_from_header,
    valid_statuses,
)

REPORT = "implementation_report"


@pytest.mark.unit
@pytest.mark.parametrize(
    "header",
    [
        "READY FOR REVIEW pending orchestrator",          # 5 words
        "Ready for review awaiting sign off",             # 6 words
        "**READY FOR REVIEW** pending final verification",
        "ready for review once the suite is green",
    ],
)
def test_a_four_plus_word_ready_header_is_no_longer_registered_as_draft(header: str) -> None:
    """The finding. Every one of these returned (None, 'unrecognized_status') before."""
    assert status_from_header(header, REPORT) == (ArtifactStatus.READY_FOR_REVIEW, None)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("header", "artifact_type", "expected"),
    [
        ("READY FOR REVIEW", REPORT, ArtifactStatus.READY_FOR_REVIEW),
        ("READY FOR REVIEW - blocked", REPORT, ArtifactStatus.READY_FOR_REVIEW),
        ("**SUPERSEDED** by menhir-todo-links", "plan", ArtifactStatus.SUPERSEDED),
        ("APPROVED as the basis for future implementation", "plan", ArtifactStatus.APPROVED),
        ("in progress", "plan", ArtifactStatus.IMPLEMENTING),
        ("complete", "review", ArtifactStatus.COMPLETE),
    ],
)
def test_headers_that_already_worked_still_work(
    header: str, artifact_type: str, expected: str
) -> None:
    """POSITIVE CONTROL: widening the loop must not disturb the widths it already tried."""
    assert status_from_header(header, artifact_type) == (expected, None)


@pytest.mark.unit
@pytest.mark.parametrize(
    "header",
    [
        "waiting until this is approved",
        "not yet ready for review",
        "we are unsure whether this is complete",
    ],
)
def test_the_leading_token_rule_still_holds(header: str) -> None:
    """The rule the docstring states: a state named mid-sentence is being DISCUSSED, not declared.

    Widening the loop must not start scanning inward past the leading token. Each header below
    contains an alias, but not at the front, and must stay unresolved.
    """
    assert status_from_header(header, REPORT) == (None, "unrecognized_status")


@pytest.mark.unit
def test_an_unknown_header_is_still_refused_rather_than_guessed() -> None:
    """POSITIVE CONTROL: without this, a loop that matched anything would pass everything above."""
    assert status_from_header("marinating", REPORT) == (None, "unrecognized_status")
    assert status_from_header("   ", REPORT) == (None, "no_status_header")
    assert status_from_header(None, REPORT) == (None, "no_status_header")


@pytest.mark.unit
@pytest.mark.parametrize("alias", sorted(STATUS_HEADER_ALIASES))
def test_every_alias_is_reachable_at_its_own_width_plus_trailing_prose(alias: str) -> None:
    """The general defect, not just the one instance.

    The old loop could reach an alias of width W only when W was in {len(words), 2, 1}. Assert
    every alias in the table still resolves with trailing prose appended, so a future three- or
    four-word alias cannot reintroduce this silently.

    Checked against a type that permits the alias's status, so a `status_invalid_for_type` result
    -- which is a different finding -- cannot mask an unreachable alias.
    """
    expected = STATUS_HEADER_ALIASES[alias]
    artifact_type = next(
        (t for t in ("implementation_report", "plan", "review") if expected in valid_statuses(t)),
        None,
    )
    assert artifact_type is not None, f"no artifact type accepts {expected}"

    mapped, reason = status_from_header(f"{alias} with some trailing commentary here", artifact_type)

    assert reason is None, (
        f"alias {alias!r} (width {len(alias.split())}) is unreachable with trailing prose: {reason}"
    )
    assert mapped == expected


@pytest.mark.unit
def test_type_mismatch_still_reports_a_different_reason_than_no_match() -> None:
    """The two reason codes must stay distinguishable -- conflating them is what made this finding
    look like a refutation during triage."""
    mapped, reason = status_from_header("READY FOR REVIEW pending orchestrator", "plan")
    assert mapped is None
    assert reason == "status_invalid_for_type"
