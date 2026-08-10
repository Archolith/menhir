"""Tests for content-role derivation (IntentOracle producer, pure domain)."""

from __future__ import annotations

import pytest

from menhir.domain.artifact_role import ContentRole, derive_content_role


@pytest.mark.parametrize("artifact_type,role", [
    ("failure", ContentRole.FAILURE),
    ("incident", ContentRole.INCIDENT),
    ("decision", ContentRole.DECISION),
    ("experiment", ContentRole.EXPERIMENT),
    ("benchmark", ContentRole.BENCHMARK),
    ("plan", ContentRole.PLAN),
    ("evidence", ContentRole.EVIDENCE),
    ("doc", ContentRole.REFERENCE),
])
def test_role_from_artifact_type(artifact_type: str, role: ContentRole) -> None:
    assert role in derive_content_role({"artifact_type": artifact_type})


def test_default_is_reference() -> None:
    assert derive_content_role({}) == {ContentRole.REFERENCE}


def test_role_from_anchor() -> None:
    assert ContentRole.PLAN in derive_content_role({"anchors": (".agent/plans/x-plan.md",)})
    assert ContentRole.BENCHMARK in derive_content_role({"anchors": ("archolith_bench/intent/",)})


def test_role_from_test_evidence() -> None:
    assert ContentRole.TEST in derive_content_role({"evidence_kinds": ("test",)})


def test_multi_role() -> None:
    roles = derive_content_role(
        {"artifact_type": "benchmark", "evidence_kinds": ("test",), "anchors": (".agent/plans/p-plan.md",)}
    )
    assert {ContentRole.BENCHMARK, ContentRole.TEST, ContentRole.PLAN} <= roles
