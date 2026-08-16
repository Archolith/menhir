from __future__ import annotations

import pytest

from menhir.domain.projection import ProjectionTarget
from menhir.domain.projection_lifecycle import (
    ProjectionDefinitionPublication,
    ProjectionFreshnessAssessment,
    ProjectionFreshnessCertificate,
    ProjectionWorkToken,
)


def _target() -> ProjectionTarget:
    return ProjectionTarget(
        namespace="tenant-a",
        subject_id="ent-1",
        key=("owned", "pre-1920"),
    )


@pytest.mark.unit
def test_work_token_requires_exact_positive_generation_and_boolean_presence() -> None:
    target = _target()
    token = ProjectionWorkToken(
        work_key="wk",
        definition_id="scalar",
        definition_version=2,
        target=target,
        generation=7,
        target_present=True,
        reason="assertion_written",
    )
    assert token.generation == 7
    assert token.target_present is True

    with pytest.raises(ValueError):
        ProjectionWorkToken(
            work_key="wk",
            definition_id="scalar",
            definition_version=True,
            target=target,
            generation=7,
            target_present=True,
            reason="bad-version",
        )
    with pytest.raises(ValueError):
        ProjectionWorkToken(
            work_key="wk",
            definition_id="scalar",
            definition_version=1,
            target=target,
            generation=0,
            target_present=True,
            reason="bad-generation",
        )
    with pytest.raises(TypeError):
        ProjectionWorkToken(
            work_key="wk",
            definition_id="scalar",
            definition_version=1,
            target=target,
            generation=1,
            target_present=1,  # type: ignore[arg-type]
            reason="bad-presence",
        )


@pytest.mark.unit
def test_definition_publication_can_report_zero_invalidations() -> None:
    publication = ProjectionDefinitionPublication(
        definition_id="scalar",
        version=1,
        semantic_hash="semantic-v1",
        changed=False,
        invalidated_targets=0,
    )
    assert publication.changed is False
    assert publication.invalidated_targets == 0


@pytest.mark.unit
def test_freshness_certificate_names_exact_version_generation_and_hash() -> None:
    certificate = ProjectionFreshnessCertificate(
        definition_id="scalar",
        definition_version=3,
        target=_target(),
        generation=9,
        target_present=True,
        projection_hash="sha256:abc",
        derivation_id="derive-9",
    )
    assert certificate.definition_version == 3
    assert certificate.generation == 9
    assert certificate.projection_hash == "sha256:abc"

    with pytest.raises(ValueError):
        ProjectionFreshnessCertificate(
            definition_id="scalar",
            definition_version=3,
            target=_target(),
            generation=9,
            target_present=True,
            projection_hash="",
            derivation_id="derive-9",
        )


@pytest.mark.unit
def test_freshness_assessment_does_not_equate_completed_generation_with_fresh() -> None:
    assessment = ProjectionFreshnessAssessment(
        state="stale",
        reason="generation_not_certified",
        definition_id="scalar",
        target=_target(),
        current_definition_version=2,
        target_present=True,
        work_generation=5,
        applied_generation=5,
        certified_definition_version=None,
        certified_generation=None,
        certified_projection_hash=None,
        current_projection_hash="hash-current",
        derivation_id=None,
    )
    assert assessment.fresh is False
    assert assessment.stale is True
