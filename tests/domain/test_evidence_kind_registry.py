"""Focused compatibility tests for the evidence-kind extension seam."""

from __future__ import annotations

import pytest

from menhir.domain.belief import EvidenceSignal
from menhir.domain.truth.kinds import (
    ANCHOR_KINDS,
    DEFAULT_EVIDENCE_KIND_REGISTRY,
    DIVERSITY_FAMILY,
    KIND_TO_SIGNAL,
    SELF_SOURCE_KINDS,
    SOURCE_LABEL_TO_KIND,
    EvidenceKindDefinition,
    EvidenceKindRegistry,
)


def test_default_registry_projects_exact_legacy_vocabulary() -> None:
    assert ANCHOR_KINDS == frozenset(
        {"user", "log", "test", "git", "file", "external", "manual", "timestamp"}
    )
    assert SELF_SOURCE_KINDS == frozenset(
        {"agent_inference", "llm_summary", "retrieval_trace", "memory_call_hint"}
    )
    assert SOURCE_LABEL_TO_KIND == {
        "user": "user",
        "git": "git",
        "test": "test",
        "log": "log",
        "file": "file",
        "manual": "manual",
        "external": "external",
        "timestamp": "timestamp",
    }
    assert KIND_TO_SIGNAL == {
        "user": EvidenceSignal.SOURCE_IS_USER,
        "log": EvidenceSignal.SOURCE_IS_LOG,
        "git": EvidenceSignal.SOURCE_IS_GIT,
        "graphiti": EvidenceSignal.SOURCE_IS_GRAPHITI,
        "file": EvidenceSignal.FILE_CHANGED,
        "test": EvidenceSignal.TEST_PASSED,
    }
    assert DIVERSITY_FAMILY == {
        "user": "user_log_test",
        "log": "user_log_test",
        "test": "user_log_test",
        "git": "git_temporal",
        "timestamp": "git_temporal",
        "file": "structure",
        "external": "external",
        "manual": "external",
        "agent_inference": "synthetic",
        "llm_summary": "synthetic",
        "retrieval_trace": "synthetic",
        "memory_call_hint": "synthetic",
    }

    registry = DEFAULT_EVIDENCE_KIND_REGISTRY
    assert registry.anchor_kinds == ANCHOR_KINDS
    assert registry.self_source_kinds == SELF_SOURCE_KINDS
    assert registry.source_label_to_kind == SOURCE_LABEL_TO_KIND
    assert registry.kind_to_signal == KIND_TO_SIGNAL
    assert registry.diversity_family == DIVERSITY_FAMILY


def test_default_source_resolution_preserves_rule_zero_behavior() -> None:
    registry = DEFAULT_EVIDENCE_KIND_REGISTRY

    assert registry.evidence_kind_for_source("user") == "user"
    assert registry.evidence_kind_for_source(" GIT ") == "git"
    assert registry.evidence_kind_for_source("llm_summary") == "llm_summary"
    assert registry.evidence_kind_for_source("claude-code") == "agent_inference"
    assert registry.evidence_kind_for_source(None) == "agent_inference"

    assert registry.has_external_anchor(("git", "agent_inference")) is True
    assert registry.has_external_anchor(("agent_inference", "llm_summary")) is False
    assert registry.is_synthetic_only(("agent_inference", "llm_summary")) is True
    assert registry.is_synthetic_only(("agent_inference", "git")) is False
    assert registry.is_synthetic_only(()) is True


def test_extension_adds_non_coding_evidence_without_mutating_default() -> None:
    deed = EvidenceKindDefinition(
        "investigation.deed",
        source_labels=("deed", "county-deed"),
        is_anchor=True,
        diversity_family="public_record",
    )

    extended = DEFAULT_EVIDENCE_KIND_REGISTRY.with_definition(deed)

    assert DEFAULT_EVIDENCE_KIND_REGISTRY.get("investigation.deed") is None
    assert "investigation.deed" not in ANCHOR_KINDS
    assert "investigation.deed" not in DIVERSITY_FAMILY

    assert extended.get("investigation.deed") == deed
    assert extended.evidence_kind_for_source(" DEED ") == "investigation.deed"
    assert extended.evidence_kind_for_source("county-deed") == "investigation.deed"
    assert extended.has_external_anchor(("investigation.deed",)) is True
    assert extended.diversity_family["investigation.deed"] == "public_record"
    assert "investigation.deed" not in extended.kind_to_signal

    # Module compatibility projections remain tied to the unchanged default registry.
    assert "deed" not in SOURCE_LABEL_TO_KIND
    assert DEFAULT_EVIDENCE_KIND_REGISTRY.evidence_kind_for_source("deed") == "agent_inference"


def test_extension_can_add_self_source_without_making_it_an_anchor() -> None:
    reflection = EvidenceKindDefinition(
        "personality.model_reflection",
        is_self_source=True,
        diversity_family="synthetic",
    )
    extended = DEFAULT_EVIDENCE_KIND_REGISTRY.with_definition(reflection)

    assert extended.evidence_kind_for_source("personality.model_reflection") == (
        "personality.model_reflection"
    )
    assert extended.is_synthetic_only(("personality.model_reflection",)) is True
    assert extended.has_external_anchor(("personality.model_reflection",)) is False


def test_definition_rejects_conflicting_semantics() -> None:
    with pytest.raises(ValueError, match="both anchor and self source"):
        EvidenceKindDefinition("bad", is_anchor=True, is_self_source=True)

    with pytest.raises(ValueError, match="trimmed lowercase"):
        EvidenceKindDefinition(" Investigation.Deed ")

    with pytest.raises(TypeError, match="EvidenceSignal"):
        EvidenceKindDefinition("bad-signal", belief_signal="source_is_user")  # type: ignore[arg-type]


def test_registry_rejects_name_and_source_label_collisions() -> None:
    with pytest.raises(ValueError, match="duplicate evidence kind"):
        EvidenceKindRegistry(
            (
                EvidenceKindDefinition("same"),
                EvidenceKindDefinition("same"),
            )
        )

    with pytest.raises(ValueError, match="source label 'shared'"):
        EvidenceKindRegistry(
            (
                EvidenceKindDefinition("one", source_labels=("shared",)),
                EvidenceKindDefinition("two", source_labels=("shared",)),
            )
        )

    with pytest.raises(ValueError, match="collides with evidence kind 'agent_inference'"):
        DEFAULT_EVIDENCE_KIND_REGISTRY.with_definition(
            EvidenceKindDefinition("investigation.synthetic", source_labels=("agent_inference",))
        )
