"""Shape validation for artifact documents.

Answers one question at ingest and update time: does this document match the
shape its type is supposed to have? A non-conforming document is *recorded*,
never rejected -- refusing the write would leave the graph unaware of a
document that exists on disk, which is strictly worse than knowing about it and
knowing it is malformed.

The implementation_report contract is not invented here. It mirrors
WRAPUP-TEMPLATE.md, enforced by cth.agentsmith's wrapup_validator, which stays
the authority; ``tests/test_artifact_shape.py`` fails if the two drift. Menhir
validates the same contract at a different moment -- the validator gates review,
this gates what the graph believes -- and two disagreeing definitions of "valid
wrapup" would be worse than none.

Pure functions over document text. No filesystem, no git, no graph.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from menhir.domain.work_artifact import ArtifactType

#: Bumped when shape rules change, so a stored verdict is interpretable
#: against the rules that produced it rather than today's.
SHAPE_SCHEMA_VERSION = 1


class ShapeStatus:
    CONFORMING = "conforming"
    NONCONFORMING = "nonconforming"
    #: No contract defined for this type -- distinct from "checked and passed".
    UNCHECKED = "unchecked"


class ShapeSeverity:
    REQUIRED = "required"
    RECOMMENDED = "recommended"


#: Mirrors wrapup_validator.checks.REQUIRED_SECTIONS (WRAPUP-TEMPLATE.md).
WRAPUP_REQUIRED_SECTIONS: frozenset[str] = frozenset({
    "Summary",
    "Files Changed",
    "Verification",
    "Claim Cross-Check",
    "Completion Checklist",
    "Assumptions",
    "Risks / Gaps",
    "Follow-Up Tasks",
})

#: Mirrors wrapup_validator.checks.REQUIRED_FIELDS.
WRAPUP_REQUIRED_FIELDS: frozenset[str] = frozenset({
    "Date", "Agent", "Model", "Status", "Commits",
})

#: A plan or review has no template as strict as the wrapup one, so the
#: contract is deliberately thin. Requiring sections nobody agreed to would
#: manufacture violations rather than detect them.
_MINIMAL_FIELDS: frozenset[str] = frozenset({"Status"})


@dataclass(frozen=True)
class ShapeContract:
    required_sections: frozenset[str] = frozenset()
    required_fields: frozenset[str] = frozenset()
    recommended_sections: frozenset[str] = frozenset()
    recommended_fields: frozenset[str] = frozenset()
    #: Whether a single H1 title is required.
    requires_title: bool = True


SHAPE_CONTRACTS: dict[str, ShapeContract] = {
    ArtifactType.IMPLEMENTATION_REPORT: ShapeContract(
        required_sections=WRAPUP_REQUIRED_SECTIONS,
        required_fields=WRAPUP_REQUIRED_FIELDS,
    ),
    ArtifactType.PLAN: ShapeContract(
        required_fields=_MINIMAL_FIELDS,
        recommended_sections=frozenset({"Open Questions"}),
    ),
    ArtifactType.REVIEW: ShapeContract(required_fields=_MINIMAL_FIELDS),
    ArtifactType.INVESTIGATION: ShapeContract(required_fields=_MINIMAL_FIELDS),
    # Derived from the corpus, not invented. Across 14 handoffs every single
    # one has an H1 title and no section appears in even 4 of them, so a title
    # is the only defensible requirement. Date shows up in 9, which makes it a
    # recommendation. Requiring more would manufacture violations rather than
    # detect them -- the failure this whole validator exists to avoid.
    ArtifactType.HANDOFF: ShapeContract(recommended_fields=frozenset({"Date"})),
}


@dataclass(frozen=True)
class ShapeFinding:
    code: str
    severity: str
    detail: str


@dataclass
class ShapeReport:
    status: str
    findings: list[ShapeFinding] = field(default_factory=list)
    sections_found: list[str] = field(default_factory=list)
    fields_found: list[str] = field(default_factory=list)
    schema_version: int = SHAPE_SCHEMA_VERSION

    @property
    def required_failures(self) -> list[ShapeFinding]:
        return [f for f in self.findings if f.severity == ShapeSeverity.REQUIRED]

    def as_properties(self) -> dict[str, object]:
        """Flattened for storage. Violation codes are a list property, not owned
        records: they are recomputed wholesale on every check and none of them
        is ever addressed individually, which is what an Owned Record is for."""
        return {
            "shape_status": self.status,
            "shape_schema_version": self.schema_version,
            "shape_violations": [f.code for f in self.required_failures],
            "shape_warnings": [
                f.code for f in self.findings if f.severity == ShapeSeverity.RECOMMENDED
            ],
        }


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
#: `**Date:** x`, `- Date: x`, `Date: x`.
_FIELD_RE = re.compile(r"^\s*[-*]?\s*\*{0,2}([A-Za-z][A-Za-z /]{1,30}?)\*{0,2}\s*:\s*\S", re.MULTILINE)


def _normalize(text: str) -> str:
    return " ".join(text.replace("*", "").split()).strip().lower()


def extract_sections(document: str) -> list[str]:
    """Every heading at H2 or deeper, in document order."""
    return [m.group(2).strip() for m in _HEADING_RE.finditer(document) if len(m.group(1)) >= 2]


def extract_fields(document: str) -> list[str]:
    """Metadata field labels, e.g. the `**Status:**` line."""
    return [m.group(1).strip() for m in _FIELD_RE.finditer(document)]


def has_title(document: str) -> bool:
    return any(len(m.group(1)) == 1 for m in _HEADING_RE.finditer(document))


def validate_shape(artifact_type: str, document: str | None) -> ShapeReport:
    """Check a document against its type's contract.

    An unreadable or empty document is nonconforming with an explicit code
    rather than an exception: ingest must still record that the artifact
    exists, and "we could not read it" is itself a finding worth storing.
    """
    contract = SHAPE_CONTRACTS.get(artifact_type)
    if contract is None:
        return ShapeReport(status=ShapeStatus.UNCHECKED)

    if not document or not document.strip():
        return ShapeReport(
            status=ShapeStatus.NONCONFORMING,
            findings=[ShapeFinding("empty_document", ShapeSeverity.REQUIRED,
                                   "document is empty or unreadable")],
        )

    sections = extract_sections(document)
    fields = extract_fields(document)
    seen_sections = {_normalize(s) for s in sections}
    seen_fields = {_normalize(f) for f in fields}
    findings: list[ShapeFinding] = []

    if contract.requires_title and not has_title(document):
        findings.append(ShapeFinding("missing_title", ShapeSeverity.REQUIRED,
                                     "no H1 heading"))

    for name in sorted(contract.required_sections):
        if _normalize(name) not in seen_sections:
            findings.append(ShapeFinding(
                f"missing_section:{_normalize(name).replace(' ', '_')}",
                ShapeSeverity.REQUIRED,
                f"required section not found: {name}",
            ))

    for name in sorted(contract.required_fields):
        if _normalize(name) not in seen_fields:
            findings.append(ShapeFinding(
                f"missing_field:{_normalize(name).replace(' ', '_')}",
                ShapeSeverity.REQUIRED,
                f"required field not found: {name}",
            ))

    for name in sorted(contract.recommended_sections):
        if _normalize(name) not in seen_sections:
            findings.append(ShapeFinding(
                f"missing_recommended_section:{_normalize(name).replace(' ', '_')}",
                ShapeSeverity.RECOMMENDED,
                f"recommended section not found: {name}",
            ))

    for name in sorted(contract.recommended_fields):
        if _normalize(name) not in seen_fields:
            findings.append(ShapeFinding(
                f"missing_recommended_field:{_normalize(name).replace(' ', '_')}",
                ShapeSeverity.RECOMMENDED,
                f"recommended field not found: {name}",
            ))

    required_failed = any(f.severity == ShapeSeverity.REQUIRED for f in findings)
    return ShapeReport(
        status=ShapeStatus.NONCONFORMING if required_failed else ShapeStatus.CONFORMING,
        findings=findings,
        sections_found=sections,
        fields_found=fields,
    )
