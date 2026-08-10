"""Shape validation for artifact documents."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from menhir.domain.artifact_shape import (
    SHAPE_CONTRACTS,
    WRAPUP_REQUIRED_FIELDS,
    WRAPUP_REQUIRED_SECTIONS,
    ShapeSeverity,
    ShapeStatus,
    extract_fields,
    extract_sections,
    validate_shape,
)
from menhir.domain.work_artifact import ARTIFACT_TYPES, ArtifactType
from menhir.infrastructure.work_artifact_repository import WorkArtifactRepository

CONFORMING_WRAPUP = """# WRAPUP - menhir / thing

**Date:** 2026-08-03
**Agent:** Claude Code
**Model:** claude-opus-4
**Status:** READY FOR REVIEW
**Commits:** abc1234

## Summary
Did the thing.

## Files Changed
- a.py

## Verification
- pytest PASS

## Claim Cross-Check
- summary matches diff: yes

## Completion Checklist
- [x] done

## Assumptions
None.

## Risks / Gaps
None.

## Follow-Up Tasks
None.
"""


@dataclass
class _StubNeo4j:
    responses: list[list[dict]] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def execute(self, query: str, params: dict | None = None) -> list[dict]:
        self.calls.append({"query": query, "params": params or {}})
        if self.responses:
            return self.responses.pop(0)
        return []


@pytest.mark.unit
def test_a_conforming_wrapup_passes() -> None:
    report = validate_shape(ArtifactType.IMPLEMENTATION_REPORT, CONFORMING_WRAPUP)

    assert report.status == ShapeStatus.CONFORMING
    assert report.required_failures == []


@pytest.mark.unit
def test_missing_sections_are_reported_individually() -> None:
    stripped = CONFORMING_WRAPUP.split("## Claim Cross-Check")[0]

    report = validate_shape(ArtifactType.IMPLEMENTATION_REPORT, stripped)
    codes = {f.code for f in report.required_failures}

    assert report.status == ShapeStatus.NONCONFORMING
    assert "missing_section:claim_cross-check" in codes
    assert "missing_section:follow-up_tasks" in codes
    # Sections that ARE present must not be reported.
    assert not any("summary" in c for c in codes)


@pytest.mark.unit
def test_missing_required_field_fails() -> None:
    no_commits = CONFORMING_WRAPUP.replace("**Commits:** abc1234", "")

    report = validate_shape(ArtifactType.IMPLEMENTATION_REPORT, no_commits)

    assert "missing_field:commits" in {f.code for f in report.required_failures}


@pytest.mark.unit
def test_recommended_findings_do_not_make_a_document_nonconforming() -> None:
    """A plan without Open Questions is unusual, not invalid."""
    report = validate_shape(ArtifactType.PLAN, "# Plan\n\n**Status:** PROPOSED\n")

    assert report.status == ShapeStatus.CONFORMING
    assert any(f.severity == ShapeSeverity.RECOMMENDED for f in report.findings)


@pytest.mark.unit
def test_empty_document_is_a_finding_not_an_exception() -> None:
    """Ingest must still record that the artifact exists."""
    report = validate_shape(ArtifactType.PLAN, "")

    assert report.status == ShapeStatus.NONCONFORMING
    assert [f.code for f in report.required_failures] == ["empty_document"]


@pytest.mark.unit
def test_unknown_type_is_unchecked_not_conforming() -> None:
    """'No contract' must never read as 'passed'."""
    report = validate_shape("adr", "# anything")  # still a deferred type

    assert report.status == ShapeStatus.UNCHECKED


@pytest.mark.unit
def test_heading_and_field_extraction_ignores_emphasis_and_depth() -> None:
    doc = "# Title\n## Summary\n### Detail\n**Date:** x\n- Agent: y\n"

    assert extract_sections(doc) == ["Summary", "Detail"]
    assert "Date" in extract_fields(doc)
    assert "Agent" in extract_fields(doc)


@pytest.mark.unit
def test_nonconforming_document_is_stored_never_rejected() -> None:
    neo4j = _StubNeo4j()
    repo = WorkArtifactRepository(neo4j)

    report = repo.record_shape("a1", ArtifactType.IMPLEMENTATION_REPORT, "# only a title")

    assert report.status == ShapeStatus.NONCONFORMING
    params = neo4j.calls[0]["params"]
    assert params["props"]["shape_status"] == ShapeStatus.NONCONFORMING
    assert params["props"]["shape_violations"]


@pytest.mark.unit
def test_ingest_without_a_document_leaves_no_verdict() -> None:
    """Absent must not masquerade as conforming."""
    neo4j = _StubNeo4j()
    repo = WorkArtifactRepository(neo4j)

    out = repo.create_artifact(artifact_type=ArtifactType.PLAN, title="t")

    assert out["shape"] is None
    assert not any("shape_status" in str(c["params"]) for c in neo4j.calls)


@pytest.mark.unit
def test_ingest_with_a_document_validates_it() -> None:
    neo4j = _StubNeo4j()
    repo = WorkArtifactRepository(neo4j)

    out = repo.create_artifact(
        artifact_type=ArtifactType.IMPLEMENTATION_REPORT,
        title="t",
        document=CONFORMING_WRAPUP,
    )

    assert out["shape"]["shape_status"] == ShapeStatus.CONFORMING


@pytest.mark.unit
def test_get_artifact_actually_selects_the_shape_fields() -> None:
    """Storing a verdict the read query never returns makes it invisible.

    Caught live rather than by the unit tests: a stubbed backend happily
    reported a shape that the real Cypher never selected.
    """
    neo4j = _StubNeo4j(responses=[[]])
    repo = WorkArtifactRepository(neo4j)

    repo.get_artifact("a1")

    query = neo4j.calls[0]["query"]
    for field in ("shape_status", "shape_violations", "status_raw",
                  "status_unresolved_reason"):
        assert field in query, f"get_artifact does not return {field}"


# ---------------------------------------------------------------------------
# Drift alarm
# ---------------------------------------------------------------------------


def _load_agentsmith_checks():
    """cth.agentsmith's wrapup_validator is the authority for the contract."""
    root = Path(__file__).resolve().parents[3] / "ctharvey" / "cth.agentsmith" / "scripts"
    if not (root / "wrapup_validator" / "checks.py").exists():
        return None
    if str(root.parent) not in sys.path:
        sys.path.insert(0, str(root.parent))
    try:
        from scripts.wrapup_validator import checks  # type: ignore
    except Exception:
        return None
    return checks


@pytest.mark.unit
def test_wrapup_contract_matches_the_authority() -> None:
    """Menhir validates the same contract wrapup_validator enforces.

    Two disagreeing definitions of "valid wrapup" would be worse than one, so
    this fails loudly on drift rather than letting the copies diverge.
    """
    checks = _load_agentsmith_checks()
    if checks is None:
        pytest.skip("cth.agentsmith wrapup_validator not present in this checkout")

    assert WRAPUP_REQUIRED_SECTIONS == frozenset(checks.REQUIRED_SECTIONS)
    assert WRAPUP_REQUIRED_FIELDS == frozenset(checks.REQUIRED_FIELDS.values())


@pytest.mark.unit
def test_every_artifact_type_has_a_contract() -> None:
    """A new type must not silently land in the UNCHECKED bucket."""
    for artifact_type in ARTIFACT_TYPES:
        assert artifact_type in SHAPE_CONTRACTS


@pytest.mark.unit
def test_handoff_requires_only_a_title() -> None:
    """Derived from 14 real handoffs: all have an H1, none share sections."""
    report = validate_shape(ArtifactType.HANDOFF, "# HANDOFF - some context\n\nbody\n")

    assert report.status == ShapeStatus.CONFORMING
    # Date is a recommendation, so it is noted without failing the document.
    assert "missing_recommended_field:date" in {f.code for f in report.findings}
    assert report.required_failures == []


@pytest.mark.unit
def test_handoff_without_a_title_still_fails() -> None:
    report = validate_shape(ArtifactType.HANDOFF, "some notes with no heading")

    assert report.status == ShapeStatus.NONCONFORMING
    assert [f.code for f in report.required_failures] == ["missing_title"]


@pytest.mark.unit
def test_a_handoff_is_not_held_to_the_wrapup_contract() -> None:
    """The whole reason the type exists: 11 handoffs were failing a contract
    that was never theirs."""
    doc = "# HANDOFF - context\n\n**Date:** 2026-08-03\n\nprose\n"

    assert validate_shape(ArtifactType.HANDOFF, doc).status == ShapeStatus.CONFORMING
    assert validate_shape(
        ArtifactType.IMPLEMENTATION_REPORT, doc
    ).status == ShapeStatus.NONCONFORMING
