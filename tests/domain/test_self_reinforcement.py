"""Tests for self-reinforcement guards (R8 anti-spiral rails)."""

from __future__ import annotations

from menhir.domain.self_reinforcement import (
    ReinforcementVerdict,
    SupportProfile,
    TouchOutcome,
    assess_reinforcement,
    durable_heat_allowed,
    evidence_kind_for_source,
    has_external_anchor,
    is_synthetic_only,
    resolve_touch,
    within_meta_depth_budget,
)


# --- Guard 5: EvidenceAnchorGate -------------------------------------------

def test_external_anchor_detection() -> None:
    assert has_external_anchor(["git", "agent_inference"]) is True
    assert has_external_anchor(["test"]) is True
    assert has_external_anchor(["agent_inference", "llm_summary"]) is False
    assert has_external_anchor([]) is False


# --- episode source -> evidence kind mapping (frontier provenance) ----------

def test_evidence_kind_for_source_maps_external_anchors() -> None:
    assert evidence_kind_for_source("user") == "user"
    assert evidence_kind_for_source("GIT") == "git"          # case-insensitive
    assert evidence_kind_for_source(" test ") == "test"      # trimmed


def test_evidence_kind_for_source_collapses_agent_and_unknown() -> None:
    # agent-harness labels and unknown provenance are self -> cannot anchor (rule zero).
    assert evidence_kind_for_source("claude-code") == "agent_inference"
    assert evidence_kind_for_source("codex") == "agent_inference"
    assert evidence_kind_for_source(None) == "agent_inference"
    assert evidence_kind_for_source("") == "agent_inference"
    # an already-self kind is preserved as-is.
    assert evidence_kind_for_source("llm_summary") == "llm_summary"


# --- Guard 2: SyntheticSupportCap ------------------------------------------

def test_synthetic_only_detection() -> None:
    assert is_synthetic_only(["agent_inference", "llm_summary", "retrieval_trace"]) is True
    assert is_synthetic_only(["agent_inference", "git"]) is False   # one real anchor
    assert is_synthetic_only([]) is True                            # nothing external


# --- Guard 1: ProductiveTouchGate ------------------------------------------

def test_touch_resolution() -> None:
    assert resolve_touch(outcome="test_passed") is TouchOutcome.PRODUCTIVE
    assert resolve_touch(outcome=None) is TouchOutcome.PENDING
    assert resolve_touch(outcome="retrieved_again") is TouchOutcome.UNPRODUCTIVE


def test_durable_heat_only_on_productive_outcome() -> None:
    assert durable_heat_allowed(outcome="user_confirmed") is True
    assert durable_heat_allowed(outcome=None) is False           # retrieval alone never heats
    assert durable_heat_allowed(outcome="just_retrieved") is False


# --- Guard 3: MetaMemoryDepthBudget ----------------------------------------

def test_meta_depth_budget() -> None:
    assert within_meta_depth_budget(1) is True
    assert within_meta_depth_budget(2) is True
    assert within_meta_depth_budget(3) is False
    assert within_meta_depth_budget(3, budget=4) is True


# --- combined assertion-boundary verdict (Guards 2/3/5) --------------------

def test_assess_ok_with_anchor_and_shallow_chain() -> None:
    p = SupportProfile(support_kinds=("git", "agent_inference"), meta_depth=1)
    assert assess_reinforcement(p) is ReinforcementVerdict.OK


def test_assess_needs_evidence_when_synthetic_only() -> None:
    p = SupportProfile(support_kinds=("agent_inference", "llm_summary"), meta_depth=1)
    assert assess_reinforcement(p) is ReinforcementVerdict.NEEDS_EVIDENCE


def test_assess_needs_evidence_when_empty_support() -> None:
    assert assess_reinforcement(SupportProfile()) is ReinforcementVerdict.NEEDS_EVIDENCE


def test_assess_over_depth_flagged_first() -> None:
    # deep meta chain is suspect even with a real anchor
    p = SupportProfile(support_kinds=("git",), meta_depth=5)
    assert assess_reinforcement(p) is ReinforcementVerdict.OVER_DEPTH
