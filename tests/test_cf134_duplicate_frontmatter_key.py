"""CF-134: a duplicate frontmatter key fails closed instead of silently overwriting.

The module's contract is "Ambiguity fails closed": a detector may say CONFLICT or UNRESOLVED, it may
not pick a value. ``parse_frontmatter`` used to do ``mapping[key] = value`` with no prior-binding
check, so a document declaring ``artifact_uuid`` twice collapsed the ambiguity before any detector
could see it and carried the SECOND uuid into DECLARED_UUID matching -- the strongest evidence
available -- producing REFRESH/RELOCATE/ATTACH against the WRONG artifact.

These tests pin the fail-closed behavior: a duplicated key is a metadata error, no ambiguous value is
bound, ``DocumentMetadata.is_valid`` is False, and the entry never reaches DECLARED_UUID matching.
"""

from __future__ import annotations

import pytest

from menhir.domain.artifact_reconciliation import (
    ActionKind,
    ConflictKind,
    CorpusEntry,
    CorpusLane,
    MatchBasis,
    plan_reconciliation,
    read_document_metadata,
)
from menhir.domain.work_artifact import ArtifactType

REPO = "menhir"
UUID_1 = "11111111-1111-4111-8111-111111111111"
UUID_2 = "22222222-2222-4222-8222-222222222222"


def _entry(*, declared_uuid, metadata_errors) -> CorpusEntry:
    return CorpusEntry(
        repository=REPO,
        path=".agent/plans/p.md",
        medium="markdown",
        lane=CorpusLane.ACTIVE,
        integrity="h",
        size_bytes=1,
        route_type=ArtifactType.PLAN,
        declared_uuid=declared_uuid,
        title="p",
        metadata_errors=metadata_errors,
    )


def _plan(entries, snapshots=()):
    return plan_reconciliation(
        repository=REPO,
        entries=entries,
        snapshots=snapshots,
        observed_commit="c0ffee",
    )


# --------------------------------------------------------------------- the finding


@pytest.mark.unit
def test_a_duplicate_artifact_uuid_yields_an_error_and_invalid_metadata() -> None:
    text = (
        "---\n"
        f"artifact_uuid: {UUID_1}\n"
        f"artifact_uuid: {UUID_2}\n"
        "---\n"
        "\n# A Plan\n"
    )
    meta = read_document_metadata(text, route_type=ArtifactType.PLAN)
    assert any(
        e.startswith("duplicate_frontmatter_key:artifact_uuid") for e in meta.errors
    )
    assert not meta.is_valid
    # Fail-closed: no ambiguous value is bound at all -- not first, not last.
    assert meta.artifact_uuid is None


# ----------------------------------------------------------------- the consequence


@pytest.mark.unit
def test_a_duplicate_uuid_entry_never_reaches_declared_uuid_matching() -> None:
    """The metadata gate (``entry.metadata_errors``) must refuse the entry before any DECLARED_UUID
    match can use it -- even if the entry still carried a uuid from a stale binding."""
    meta = read_document_metadata(
        "---\n"
        f"artifact_uuid: {UUID_1}\n"
        f"artifact_uuid: {UUID_2}\n"
        "---\n"
        "# T\n"
    )
    entry = _entry(declared_uuid=UUID_1, metadata_errors=meta.errors)
    report = _plan([entry])

    assert not any(a.basis == MatchBasis.DECLARED_UUID for a in report.actions)
    conflicts = [a for a in report.actions if a.kind == ActionKind.CONFLICT]
    assert any(
        a.conflict_kind == ConflictKind.INVALID_DECLARED_METADATA for a in conflicts
    ), f"expected INVALID_DECLARED_METADATA conflict, got {report.actions}"


# -------------------------------------------------------------- the guard is general


@pytest.mark.unit
def test_a_duplicate_key_other_than_artifact_uuid_is_also_reported() -> None:
    text = "---\nartifact_type: plan\nartifact_type: review\n---\n# T\n"
    meta = read_document_metadata(text, route_type=ArtifactType.PLAN)
    assert any(
        e.startswith("duplicate_frontmatter_key:artifact_type") for e in meta.errors
    )
    assert not meta.is_valid


# ------------------------------------------------------------- positive control: clean doc


@pytest.mark.unit
def test_an_ordinary_document_with_distinct_keys_parses_cleanly() -> None:
    text = (
        "---\n"
        f"artifact_uuid: {UUID_1}\n"
        "artifact_type: plan\n"
        "artifact_status: APPROVED\n"
        "artifact_schema: 1\n"
        "\n"
        "# an author comment\n"
        "---\n"
        "\n# A Plan\n"
    )
    meta = read_document_metadata(text, route_type=ArtifactType.PLAN)
    assert meta.errors == ()
    assert meta.is_valid
    assert meta.artifact_uuid == UUID_1
    assert meta.artifact_type == ArtifactType.PLAN


# ---------------------- positive control: same value repeated (decided: still an error)


@pytest.mark.unit
def test_a_key_repeated_with_the_same_value_is_still_rejected() -> None:
    """Decision: ANY second binding of a key is ambiguous about which line is authoritative, even
    when the text matches, so it is an error too -- refuse rather than guess."""
    text = "---\nartifact_type: plan\nartifact_type: plan\n---\n# T\n"
    meta = read_document_metadata(text, route_type=ArtifactType.PLAN)
    assert any(
        e.startswith("duplicate_frontmatter_key:artifact_type") for e in meta.errors
    )
    assert not meta.is_valid
