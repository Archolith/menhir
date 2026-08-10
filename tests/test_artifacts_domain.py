"""Unit tests for the L4 artifact domain policy (R9-lite trust rules).

Pure functions, no graph. These encode the four hard L4 invariants the bench pinned
down. They run at home with the full env; remotely the same assertions are exercised by
scratch probe (the package conftest needs httpx/graphiti, unavailable in CI sandbox)."""

from __future__ import annotations

import pytest

from menhir.domain.artifacts import (
    ArtifactSource,
    ArtifactStatus,
    Evidence,
    can_promote,
    confidence_for,
    decide_status,
    has_promotable_evidence,
    scope_for_status,
)


@pytest.mark.unit
def test_human_with_promotable_evidence_is_trusted() -> None:
    assert decide_status(source=ArtifactSource.HUMAN, has_promotable_evidence=True) is ArtifactStatus.TRUSTED


@pytest.mark.unit
def test_human_without_promotable_evidence_is_candidate() -> None:
    assert decide_status(source=ArtifactSource.HUMAN, has_promotable_evidence=False) is ArtifactStatus.CANDIDATE


@pytest.mark.unit
def test_llm_is_never_trusted_on_create() -> None:
    # invariant 4 — even with evidence, an LLM artifact is review-gated.
    assert decide_status(source=ArtifactSource.LLM, has_promotable_evidence=True) is ArtifactStatus.CANDIDATE


@pytest.mark.unit
def test_promote_requires_promotable_evidence_and_refuses_historical() -> None:
    assert can_promote(has_promotable_evidence=True, is_historical=False) is True   # invariant 3 satisfied
    assert can_promote(has_promotable_evidence=False, is_historical=False) is False  # invariant 3
    assert can_promote(has_promotable_evidence=True, is_historical=True) is False    # invariant 7


@pytest.mark.unit
def test_agent_inference_is_not_promotable_evidence() -> None:
    # Fix 1: LLM self-evidence can't justify trust; non-self kinds can.
    assert has_promotable_evidence(["agent_inference"]) is False
    assert has_promotable_evidence(["agent_inference", "git"]) is True
    assert has_promotable_evidence(["user"]) is True
    assert has_promotable_evidence([]) is False
    assert Evidence(kind="agent_inference", ref="x").is_promotable is False
    assert Evidence(kind="log", ref="x").is_promotable is True


@pytest.mark.unit
def test_confidence_reflects_trust_level() -> None:
    # Fix 4: trusted artifacts aren't left at the neutral 0.5 default.
    assert confidence_for(status=ArtifactStatus.TRUSTED, source=ArtifactSource.HUMAN) == 0.9
    assert confidence_for(status=ArtifactStatus.CANDIDATE, source=ArtifactSource.HUMAN) == 0.5
    assert confidence_for(status=ArtifactStatus.CANDIDATE, source=ArtifactSource.LLM) == 0.4


@pytest.mark.unit
def test_scope_mapping_matches_node_scope() -> None:
    assert scope_for_status(ArtifactStatus.CANDIDATE) == "CANDIDATE"
    assert scope_for_status(ArtifactStatus.TRUSTED) == "PERSISTENT"
    assert scope_for_status(ArtifactStatus.HISTORICAL) == "PERSISTENT"


@pytest.mark.unit
def test_evidence_structural_flag() -> None:
    assert Evidence(kind="git", ref="e8da67d").is_structural is True
    assert Evidence(kind="test", ref="t::x").is_structural is True
    assert Evidence(kind="agent_inference", ref="foo.py").is_structural is False
