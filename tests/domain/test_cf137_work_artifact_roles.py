"""CF-137: the four unmapped ``work_artifact`` types no longer fall through to REFERENCE.

``domain.artifact_role._ARTIFACT_TYPE`` previously only knew the L4 institutional and
research/doc shapes, so ``handoff``, ``implementation_report``, ``investigation`` and
``review`` all derived the bare ``reference`` role and took a 20x affinity penalty in the
IntentOracle (PENALIZE 0.05 vs PREFER 1.0). These tests pin the new mappings AND the
fallthrough default, so both a regression and a taxonomy rewrite are caught.
"""

import pytest

from menhir.domain.artifact_role import (
    ContentRole,
    _ARTIFACT_TYPE,
    derive_content_role,
)
from menhir.domain.work_artifact import ARTIFACT_TYPES, ArtifactType


def _roles(artifact_type: str) -> set[ContentRole]:
    """Roles derived from a candidate carrying only this artifact_type (no anchors/kinds)."""
    return derive_content_role({"artifact_type": artifact_type})


@pytest.mark.unit
class TestEachWorkArtifactMappedAwayFromFallthrough:
    """Each of the four previously-falling-through types now derives a specific role."""

    def test_handoff_is_not_reference(self):
        assert _roles(ArtifactType.HANDOFF) == {ContentRole.EVIDENCE}

    def test_implementation_report_is_not_reference(self):
        assert _roles(ArtifactType.IMPLEMENTATION_REPORT) == {ContentRole.EVIDENCE}

    def test_investigation_is_not_reference(self):
        assert _roles(ArtifactType.INVESTIGATION) == {ContentRole.EVIDENCE}

    def test_review_is_not_reference(self):
        assert _roles(ArtifactType.REVIEW) == {ContentRole.DECISION}


@pytest.mark.unit
class TestFullArtifactCoverage:
    """Every declared work_artifact type maps to something other than the fallthrough.

    Iterates the real tuple rather than a hand-written list, so a future work_artifact
    type with no mapping fails here instead of silently regressing.
    """

    def test_every_artifact_type_derives_a_specific_role(self):
        for artifact_type in ARTIFACT_TYPES:
            roles = _roles(artifact_type)
            assert ContentRole.REFERENCE not in roles
            assert roles, f"{artifact_type!r} derived no role"


@pytest.mark.unit
class TestFallthroughPositiveControl:
    """An unknown/garbage type still falls through to reference."""

    def test_unknown_type_falls_through_to_reference(self):
        assert _roles("definitely-not-a-real-artifact-type") == {ContentRole.REFERENCE}

    def test_empty_artifact_type_falls_through_to_reference(self):
        assert derive_content_role({}) == {ContentRole.REFERENCE}


@pytest.mark.unit
class TestPreExistingTaxonomyUnchanged:
    """The pre-existing mapping is untouched by the CF-137 addition."""

    def test_existing_mapping_matches_baseline(self):
        expected = {
            "failure": ContentRole.FAILURE,
            "incident": ContentRole.INCIDENT,
            "decision": ContentRole.DECISION,
            "experiment": ContentRole.EXPERIMENT,
            "benchmark": ContentRole.BENCHMARK,
            "test": ContentRole.TEST,
            "plan": ContentRole.PLAN,
            "runbook": ContentRole.RUNBOOK,
            "evidence": ContentRole.EVIDENCE,
            "reference": ContentRole.REFERENCE,
            "doc": ContentRole.REFERENCE,
        }
        for artifact_type, role in expected.items():
            assert _ARTIFACT_TYPE[artifact_type] == role
