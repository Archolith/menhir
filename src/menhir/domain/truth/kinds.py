"""Evidence-kind definitions — the single source of truth for source provenance strings.

All evidence_kind / source strings used across self_reinforcement, belief_evidence,
and the frontier recall service are defined here so they stay consistent. Three places
previously defined overlapping subsets independently; this module is the SSOT.

The default registry preserves Menhir's existing built-in evidence vocabulary. Extensions
may derive an additive registry with ``EvidenceKindRegistry.with_definition`` without
mutating the default registry or editing this module. Legacy constants remain compatibility
projections of the default registry so existing callers keep today's behavior.

Consumers:
- ``domain/self_reinforcement.py``  — imports ANCHOR_KINDS, SELF_SOURCE_KINDS, SOURCE_LABEL_TO_KIND
- ``domain/belief_evidence.py``     — imports KIND_TO_SIGNAL
- ``services/recall_service.py``    — imports DIVERSITY_FAMILY
- ``infrastructure/structure_queries.py`` — imports SOURCE_CONFIDENCE_* constants
"""

from __future__ import annotations

from dataclasses import dataclass

from menhir.domain.belief import EvidenceSignal


# ---------------------------------------------------------------------------
# Evidence-kind registry
# ---------------------------------------------------------------------------


def _normalise_token(value: str) -> str:
    return value.strip().lower()


@dataclass(frozen=True, slots=True)
class EvidenceKindDefinition:
    """One evidence-kind vocabulary entry.

    ``source_labels`` are freeform episode-source labels that may resolve to this canonical
    kind. ``is_anchor`` and ``is_self_source`` are mutually exclusive assertion-boundary
    classifications. ``belief_signal`` and ``diversity_family`` are optional because not
    every provenance kind participates in those policies.
    """

    name: str
    source_labels: tuple[str, ...] = ()
    is_anchor: bool = False
    is_self_source: bool = False
    belief_signal: EvidenceSignal | None = None
    diversity_family: str | None = None

    def __post_init__(self) -> None:
        canonical_name = _normalise_token(self.name)
        if not canonical_name:
            raise ValueError("evidence kind name must not be empty")
        if canonical_name != self.name:
            raise ValueError("evidence kind name must already be trimmed lowercase")
        if self.is_anchor and self.is_self_source:
            raise ValueError("evidence kind cannot be both anchor and self source")
        if self.belief_signal is not None and not isinstance(self.belief_signal, EvidenceSignal):
            raise TypeError("belief_signal must be an EvidenceSignal or None")

        labels = tuple(_normalise_token(label) for label in self.source_labels)
        if any(not label for label in labels):
            raise ValueError("source labels must not be empty")
        if len(set(labels)) != len(labels):
            raise ValueError("source labels must be unique within an evidence kind")
        object.__setattr__(self, "source_labels", labels)

        if self.diversity_family is not None:
            family = self.diversity_family.strip()
            if not family:
                raise ValueError("diversity_family must not be blank")
            object.__setattr__(self, "diversity_family", family)


@dataclass(frozen=True, slots=True)
class EvidenceKindRegistry:
    """Immutable collection of evidence-kind definitions.

    The registry owns vocabulary metadata only. Source-confidence thresholds, belief signal
    weights, admission policy, and domain-specific trust semantics remain outside this type.
    """

    definitions: tuple[EvidenceKindDefinition, ...] = ()

    def __post_init__(self) -> None:
        definitions = tuple(self.definitions)
        object.__setattr__(self, "definitions", definitions)

        by_name: dict[str, EvidenceKindDefinition] = {}
        for definition in definitions:
            if not isinstance(definition, EvidenceKindDefinition):
                raise TypeError("registry definitions must be EvidenceKindDefinition instances")
            if definition.name in by_name:
                raise ValueError(f"duplicate evidence kind: {definition.name}")
            by_name[definition.name] = definition

        source_owners: dict[str, str] = {}
        for definition in definitions:
            for label in definition.source_labels:
                canonical_owner = by_name.get(label)
                if canonical_owner is not None and canonical_owner.name != definition.name:
                    raise ValueError(
                        f"source label {label!r} collides with evidence kind {canonical_owner.name!r}"
                    )
                previous = source_owners.get(label)
                if previous is not None and previous != definition.name:
                    raise ValueError(
                        f"source label {label!r} is registered by both {previous!r} and "
                        f"{definition.name!r}"
                    )
                source_owners[label] = definition.name

    def with_definition(self, definition: EvidenceKindDefinition) -> EvidenceKindRegistry:
        """Return a new registry containing ``definition``; never mutate this registry."""
        return EvidenceKindRegistry(self.definitions + (definition,))

    def get(self, name: str) -> EvidenceKindDefinition | None:
        """Return the canonical definition for ``name`` after source-style normalization."""
        canonical = _normalise_token(name)
        return next((d for d in self.definitions if d.name == canonical), None)

    @property
    def anchor_kinds(self) -> frozenset[str]:
        return frozenset(d.name for d in self.definitions if d.is_anchor)

    @property
    def self_source_kinds(self) -> frozenset[str]:
        return frozenset(d.name for d in self.definitions if d.is_self_source)

    @property
    def source_label_to_kind(self) -> dict[str, str]:
        return {
            label: definition.name
            for definition in self.definitions
            for label in definition.source_labels
        }

    @property
    def kind_to_signal(self) -> dict[str, EvidenceSignal]:
        return {
            definition.name: definition.belief_signal
            for definition in self.definitions
            if definition.belief_signal is not None
        }

    @property
    def diversity_family(self) -> dict[str, str]:
        return {
            definition.name: definition.diversity_family
            for definition in self.definitions
            if definition.diversity_family is not None
        }

    def evidence_kind_for_source(self, source: str | None) -> str:
        """Resolve freeform source provenance using the existing rule-zero fallback.

        Explicit source labels resolve first. A canonical self-source kind is preserved as-is.
        Everything else collapses to ``agent_inference`` so unknown agent/harness labels cannot
        masquerade as externally anchored evidence.
        """
        label = _normalise_token(source or "")
        mapped = self.source_label_to_kind.get(label)
        if mapped is not None:
            return mapped
        if label in self.self_source_kinds:
            return label
        return "agent_inference"

    def has_external_anchor(self, support_kinds: list[str] | tuple[str, ...]) -> bool:
        """Return whether at least one support kind is an anchor in this registry."""
        anchors = self.anchor_kinds
        return any(kind in anchors for kind in support_kinds)

    def is_synthetic_only(self, support_kinds: list[str] | tuple[str, ...]) -> bool:
        """Return whether support is empty or consists entirely of registered self sources."""
        kinds = list(support_kinds)
        if not kinds:
            return True
        self_sources = self.self_source_kinds
        return all(kind in self_sources for kind in kinds)


_DEFAULT_DEFINITIONS: tuple[EvidenceKindDefinition, ...] = (
    EvidenceKindDefinition(
        "user",
        source_labels=("user",),
        is_anchor=True,
        belief_signal=EvidenceSignal.SOURCE_IS_USER,
        diversity_family="user_log_test",
    ),
    EvidenceKindDefinition(
        "log",
        source_labels=("log",),
        is_anchor=True,
        belief_signal=EvidenceSignal.SOURCE_IS_LOG,
        diversity_family="user_log_test",
    ),
    EvidenceKindDefinition(
        "test",
        source_labels=("test",),
        is_anchor=True,
        belief_signal=EvidenceSignal.TEST_PASSED,
        diversity_family="user_log_test",
    ),
    EvidenceKindDefinition(
        "git",
        source_labels=("git",),
        is_anchor=True,
        belief_signal=EvidenceSignal.SOURCE_IS_GIT,
        diversity_family="git_temporal",
    ),
    EvidenceKindDefinition(
        "file",
        source_labels=("file",),
        is_anchor=True,
        belief_signal=EvidenceSignal.FILE_CHANGED,
        diversity_family="structure",
    ),
    EvidenceKindDefinition(
        "external",
        source_labels=("external",),
        is_anchor=True,
        diversity_family="external",
    ),
    EvidenceKindDefinition(
        "manual",
        source_labels=("manual",),
        is_anchor=True,
        diversity_family="external",
    ),
    EvidenceKindDefinition(
        "timestamp",
        source_labels=("timestamp",),
        is_anchor=True,
        diversity_family="git_temporal",
    ),
    EvidenceKindDefinition(
        "agent_inference",
        is_self_source=True,
        diversity_family="synthetic",
    ),
    EvidenceKindDefinition(
        "llm_summary",
        is_self_source=True,
        diversity_family="synthetic",
    ),
    EvidenceKindDefinition(
        "retrieval_trace",
        is_self_source=True,
        diversity_family="synthetic",
    ),
    EvidenceKindDefinition(
        "memory_call_hint",
        is_self_source=True,
        diversity_family="synthetic",
    ),
    # ``graphiti`` is intentionally neither an anchor nor a self source. It is an internal
    # extraction provenance retained for belief scoring compatibility.
    EvidenceKindDefinition(
        "graphiti",
        belief_signal=EvidenceSignal.SOURCE_IS_GRAPHITI,
    ),
)

DEFAULT_EVIDENCE_KIND_REGISTRY = EvidenceKindRegistry(_DEFAULT_DEFINITIONS)


# ---------------------------------------------------------------------------
# Legacy compatibility projections (rule zero: retrieval is attention, not truth)
# ---------------------------------------------------------------------------

# Non-self anchors: external/productive evidence that can ground a current-truth claim.
ANCHOR_KINDS: frozenset[str] = DEFAULT_EVIDENCE_KIND_REGISTRY.anchor_kinds

# Self / non-anchor sources: retrieval is attention, not truth. These cannot, alone,
# ground a current-truth assertion (rule zero).
SELF_SOURCE_KINDS: frozenset[str] = DEFAULT_EVIDENCE_KIND_REGISTRY.self_source_kinds

# Freeform episode ``source`` provenance labels that ARE external anchors.
# Anything else — agent harness labels like "claude-code"/"codex", or unknown provenance
# — collapses to "agent_inference" inside ``evidence_kind_for_source``.
SOURCE_LABEL_TO_KIND: dict[str, str] = DEFAULT_EVIDENCE_KIND_REGISTRY.source_label_to_kind

# evidence_kind -> EvidenceSignal for the belief-scoring model.
# Unknown kinds are ignored by the scorer (returns None for unrecognised kinds).
#
# Note on "graphiti": this kind is NOT in ANCHOR_KINDS (it's a system-internal extraction
# source, not an external user/code anchor) and NOT in SELF_SOURCE_KINDS (it's not purely
# agent-generated). It does NOT appear in DIVERSITY_FAMILY (the diversity gate returns
# "semantic" for it). In practice "graphiti" does not surface as an evidence_kind in
# production memory nodes — the entry is preserved from the original belief_evidence.py for
# completeness and in case internal provenance nodes use it in future.
KIND_TO_SIGNAL: dict[str, EvidenceSignal] = DEFAULT_EVIDENCE_KIND_REGISTRY.kind_to_signal

# evidence_kind -> diversity family string for the oracle combiner's diversity gate.
# Families group kinds whose results should not all surface together (source-aware spread).
DIVERSITY_FAMILY: dict[str, str] = DEFAULT_EVIDENCE_KIND_REGISTRY.diversity_family


# ---------------------------------------------------------------------------
# Source-confidence constants (match MemoryNode.source_confidence production values)
# ---------------------------------------------------------------------------

#: User-confirmed / user-stated fact: highest trust tier (ReviewState.HUMAN_REVIEWED).
#: Use this when writing a memory that came directly from a user message or explicit
#: user confirmation. Intended for ingest paths that stamp user-provenance.
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
