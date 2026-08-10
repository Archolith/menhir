"""Evidence-kind constants — the single source of truth for source provenance strings.

All evidence_kind / source strings used across self_reinforcement, belief_evidence,
and the frontier recall service are defined here so they stay consistent.  Three places
previously defined overlapping subsets independently; this module is the SSOT.

Consumers:
- ``domain/self_reinforcement.py``  — imports ANCHOR_KINDS, SELF_SOURCE_KINDS, SOURCE_LABEL_TO_KIND
- ``domain/belief_evidence.py``     — imports KIND_TO_SIGNAL
- ``services/recall_service.py``    — imports DIVERSITY_FAMILY
- ``infrastructure/structure_queries.py`` — imports SOURCE_CONFIDENCE_* constants
"""

from __future__ import annotations

from menhir.domain.belief import EvidenceSignal

# ---------------------------------------------------------------------------
# Source-provenance sets (rule zero: retrieval is attention, not truth)
# ---------------------------------------------------------------------------

# Non-self anchors: external/productive evidence that can ground a current-truth claim.
ANCHOR_KINDS: frozenset[str] = frozenset(
    {"user", "log", "test", "git", "file", "external", "manual", "timestamp"}
)

# Self / non-anchor sources: retrieval is attention, not truth.  These cannot, alone,
# ground a current-truth assertion (rule zero).
SELF_SOURCE_KINDS: frozenset[str] = frozenset(
    {"agent_inference", "llm_summary", "retrieval_trace", "memory_call_hint"}
)

# Freeform episode ``source`` provenance labels that ARE external anchors.
# Anything else — agent harness labels like "claude-code"/"codex", or unknown provenance
# — collapses to "agent_inference" inside ``evidence_kind_for_source``.
SOURCE_LABEL_TO_KIND: dict[str, str] = {
    "user": "user", "git": "git", "test": "test", "log": "log",
    "file": "file", "manual": "manual", "external": "external", "timestamp": "timestamp",
}

# ---------------------------------------------------------------------------
# Belief-evidence signal mapping
# ---------------------------------------------------------------------------

# evidence_kind -> EvidenceSignal for the belief-scoring model.
# Unknown kinds are ignored by the scorer (returns None for unrecognised kinds).
#
# Note on "graphiti": this kind is NOT in ANCHOR_KINDS (it's a system-internal extraction
# source, not an external user/code anchor) and NOT in SELF_SOURCE_KINDS (it's not purely
# agent-generated). It does NOT appear in DIVERSITY_FAMILY (the diversity gate returns
# "semantic" for it). In practice "graphiti" does not surface as an evidence_kind in
# production memory nodes — the entry is preserved from the original belief_evidence.py for
# completeness and in case internal provenance nodes use it in future.
KIND_TO_SIGNAL: dict[str, EvidenceSignal] = {
    "user":     EvidenceSignal.SOURCE_IS_USER,
    "log":      EvidenceSignal.SOURCE_IS_LOG,
    "git":      EvidenceSignal.SOURCE_IS_GIT,
    "graphiti": EvidenceSignal.SOURCE_IS_GRAPHITI,
    "file":     EvidenceSignal.FILE_CHANGED,
    "test":     EvidenceSignal.TEST_PASSED,
}

# ---------------------------------------------------------------------------
# Diversity family mapping (frontier oracle recall diversity gate)
# ---------------------------------------------------------------------------

# evidence_kind -> diversity family string for the oracle combiner's diversity gate.
# Families group kinds whose results should not all surface together (source-aware spread).
DIVERSITY_FAMILY: dict[str, str] = {
    "user":             "user_log_test",
    "log":              "user_log_test",
    "test":             "user_log_test",
    "git":              "git_temporal",
    "timestamp":        "git_temporal",
    "file":             "structure",
    "external":         "external",
    "manual":           "external",
    "agent_inference":  "synthetic",
    "llm_summary":      "synthetic",
    "retrieval_trace":  "synthetic",
    "memory_call_hint": "synthetic",
}

# ---------------------------------------------------------------------------
# Source-confidence constants (match MemoryNode.source_confidence production values)
# ---------------------------------------------------------------------------

#: User-confirmed / user-stated fact: highest trust tier (ReviewState.HUMAN_REVIEWED).
#: Use this when writing a memory that came directly from a user message or explicit
#: user confirmation.  Intended for ingest paths that stamp user-provenance.
SOURCE_CONFIDENCE_USER: float = 1.0

#: Structural / deterministic ground truth (git, file, test, project-scan).
#: Deterministic means verifiable without a human reviewer (ReviewState.HUMAN_REVIEWED).
#: Also used as the HUMAN_REVIEWED review-state threshold in review_state_from_confidence.
SOURCE_CONFIDENCE_STRUCTURAL: float = 0.9

#: Agent-reviewed confidence boundary: at or above this value, a claim is ReviewState.AGENT_REVIEWED.
#: Below this value (and below SOURCE_CONFIDENCE_STRUCTURAL) the claim is ReviewState.UNREVIEWED.
#: Use this constant as the threshold in review_state_from_confidence rather than a bare 0.7 literal.
SOURCE_CONFIDENCE_AGENT_REVIEWED: float = 0.7

#: Raw LLM inference / unvalidated agent discovery: lowest trust tier (ReviewState.UNREVIEWED).
#: Default fallback when source_confidence is None.
SOURCE_CONFIDENCE_AGENT: float = 0.5
