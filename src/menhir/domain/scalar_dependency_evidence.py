"""Immutable, parser-neutral transport for scalar dependency evidence.

This module deliberately contains no parser or source-revalidation logic.  Parser output is an
untrusted, versioned observation; callers must revalidate every span against the original source
before allowing it to influence a proposal or composition.
"""

from __future__ import annotations

from dataclasses import dataclass

from menhir.domain.scalar_dependency_evidence_hashing import (
    canonical_evidence_json,
    compute_evidence_hash,
    verify_evidence_hash,
)
from menhir.domain.scalar_dependency_evidence_limits import (
    MAX_CATEGORY_LENGTH,
    MAX_CHECK_NAME_LENGTH,
    MAX_CHECK_NAMES,
    MAX_COMPOSER_VERSION_LENGTH,
    MAX_CUES,
    MAX_EDGES,
    MAX_LABEL_LENGTH,
    MAX_MARKERS,
    MAX_OUTCOME_LENGTH,
    MAX_PARSER_ID_LENGTH,
    MAX_POS_TAG_LENGTH,
    MAX_REASON_LENGTH,
    MAX_RULE_VERSION_LENGTH,
    MAX_SOURCE_LENGTH,
    MAX_TOKENS,
    MAX_VERSION_LENGTH,
    SUPPORTED_EVIDENCE_VERSIONS,
    SUPPORTED_SCHEMA_VERSIONS,
)
from menhir.domain.scalar_dependency_evidence_receipt import ScalarDependencyEvidenceReceipt
from menhir.domain.scalar_dependency_evidence_spans import (
    DependencyEdge,
    MarkerEvidence,
    ScalarCueEvidence,
    SourceBoundSpan,
    TokenEvidence,
    _inside,
    _span,
    _token_bounds_inside,
    source_slice_sha256,
)
from menhir.domain.scalar_dependency_evidence_validation import (
    _bounded_tuple,
    _hash,
    _nonnegative,
    _text,
    _version_token,
)


@dataclass(frozen=True, slots=True)
class ScalarDependencyEvidence:
    """Versioned parser evidence envelope with structural, source-bound invariants."""

    schema_version: str
    evidence_version: str
    parser_id: str
    parser_version: str
    model_hash: str
    pipeline_hash: str
    source_hash: str
    source_length: int
    candidate_hash: str
    clause_span: SourceBoundSpan
    tokens: tuple[TokenEvidence, ...]
    edges: tuple[DependencyEdge, ...]
    cues: ScalarCueEvidence
    markers: tuple[MarkerEvidence, ...]
    evidence_sha256: str = ""

    def __post_init__(self) -> None:
        _version_token("schema_version", self.schema_version)
        _version_token("evidence_version", self.evidence_version)
        _text("parser_id", self.parser_id, MAX_PARSER_ID_LENGTH)
        _text("parser_version", self.parser_version, MAX_VERSION_LENGTH)
        if self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError("schema_version is unsupported")
        if self.evidence_version not in SUPPORTED_EVIDENCE_VERSIONS:
            raise ValueError("evidence_version is unsupported")
        for name, value in (
            ("model_hash", self.model_hash),
            ("pipeline_hash", self.pipeline_hash),
            ("source_hash", self.source_hash),
            ("candidate_hash", self.candidate_hash),
        ):
            _hash(name, value)
        source_length = _nonnegative("source_length", self.source_length)
        if source_length > MAX_SOURCE_LENGTH:
            raise ValueError(f"source_length exceeds maximum {MAX_SOURCE_LENGTH}")
        clause_span = _span("clause_span", self.clause_span)
        if clause_span.end > source_length:
            raise ValueError("clause_span exceeds source_length")

        tokens = _bounded_tuple("tokens", self.tokens, MAX_TOKENS)
        if not tokens:
            raise ValueError("tokens must not be empty")
        for index, token in enumerate(tokens):
            if not isinstance(token, TokenEvidence):
                raise ValueError(f"tokens[{index}] must be TokenEvidence")
        if clause_span.token_start is not None and (
            clause_span.token_start != 0 or clause_span.token_end != len(tokens)
        ):
            raise ValueError("clause_span token bounds must be (0, len(tokens))")
        _token_bounds_inside(clause_span, tokens, "clause_span")
        if tuple(token.token_index for token in tokens) != tuple(range(len(tokens))):
            raise ValueError("token indices must be unique and contiguous from zero")
        previous_end: int | None = None
        for index, token in enumerate(tokens):
            _span(f"tokens[{index}].span", token.span)
            _inside(token.span, clause_span, f"tokens[{index}].span")
            if previous_end is not None and token.span.start < previous_end:
                raise ValueError("token source spans must be monotonic and non-overlapping")
            previous_end = token.span.end
            if token.span.token_start is not None and (
                token.span.token_start != token.token_index
                or token.span.token_end != token.token_index + 1
            ):
                raise ValueError("token span token bounds must identify its token")
            if token.head_index >= len(tokens):
                raise ValueError("token head_index is outside the token range")

        edges = _bounded_tuple("edges", self.edges, MAX_EDGES)
        for index, edge in enumerate(edges):
            if not isinstance(edge, DependencyEdge):
                raise ValueError(f"edges[{index}] must be DependencyEdge")
        for index, edge in enumerate(edges):
            if edge.head_index == edge.dependent_index:
                raise ValueError(f"edges[{index}] must not be a self-loop")
            if edge.head_index >= len(tokens) or edge.dependent_index >= len(tokens):
                raise ValueError(f"edges[{index}] index is outside the token range")
        if tuple(edge.dependent_index for edge in edges) != tuple(
            sorted(edge.dependent_index for edge in edges)
        ):
            raise ValueError("edges must be canonically ordered by dependent_index")
        if len(edges) != len(tokens) - 1:
            raise ValueError("each nonroot token must have exactly one dependency edge")

        if not isinstance(self.cues, ScalarCueEvidence):
            raise ValueError("cues must be ScalarCueEvidence")
        roots = tuple(token.token_index for token in tokens if token.head_index == -1)
        if len(roots) != 1:
            raise ValueError("evidence must contain exactly one root token")
        if self.cues.clause_root_token >= len(tokens):
            raise ValueError("clause_root_token is outside the token range")
        if roots[0] != self.cues.clause_root_token:
            raise ValueError("cues.clause_root_token must identify the root token")

        edge_by_dependent: dict[int, DependencyEdge] = {}
        for edge in edges:
            if edge.dependent_index in edge_by_dependent:
                raise ValueError("each dependent token must have exactly one dependency edge")
            if edge.dependent_index == roots[0]:
                raise ValueError("root token must not have a dependency edge")
            edge_by_dependent[edge.dependent_index] = edge
        for token in tokens:
            if token.token_index == roots[0]:
                continue
            edge = edge_by_dependent.get(token.token_index)
            if edge is None or (edge.head_index, edge.dependent_index, edge.label) != (
                token.head_index,
                token.token_index,
                token.dependency_label,
            ):
                raise ValueError("dependency edges must match each nonroot token")
        for token in tokens:
            seen: set[int] = set()
            current = token.token_index
            while current != -1:
                if current in seen:
                    raise ValueError("dependency graph contains a cycle")
                seen.add(current)
                current = tokens[current].head_index

        cue_spans: list[tuple[str, SourceBoundSpan]] = []
        for name, span in (
            ("subject", self.cues.subject),
            ("predicate", self.cues.predicate),
            ("numeric_value", self.cues.numeric_value),
            ("unit", self.cues.unit),
            ("target", self.cues.target),
            ("scope", self.cues.scope),
        ):
            if span is not None:
                cue_spans.append((name, span))
        cue_spans.extend((f"modifiers[{i}]", span) for i, span in enumerate(self.cues.modifiers))
        for name, span in cue_spans:
            _inside(span, clause_span, f"cues.{name}")
            if span.end > source_length:
                raise ValueError(f"cues.{name} exceeds source_length")
            _token_bounds_inside(span, tokens, f"cues.{name}")
        modifier_keys = [(span.start, span.end) for span in self.cues.modifiers]
        if modifier_keys != sorted(modifier_keys):
            raise ValueError("cue modifier spans must be in canonical source order")

        markers = _bounded_tuple("markers", self.markers, MAX_MARKERS)
        for index, marker in enumerate(markers):
            if not isinstance(marker, MarkerEvidence):
                raise ValueError(f"markers[{index}] must be MarkerEvidence")
            _inside(marker.span, clause_span, f"markers[{index}].span")
            if marker.span.end > source_length:
                raise ValueError(f"markers[{index}].span exceeds source_length")
            _token_bounds_inside(marker.span, tokens, f"markers[{index}].span")
            for token_index in marker.token_indices:
                if token_index >= len(tokens):
                    raise ValueError(f"markers[{index}] token index is outside the token range")
            if marker.token_indices:
                if marker.span.token_start is None or marker.span.token_end is None:
                    raise ValueError("marker token_indices require span token bounds")
                if (
                    marker.span.token_start > marker.token_indices[0]
                    or marker.span.token_end < marker.token_indices[-1] + 1
                ):
                    raise ValueError("marker span token bounds must contain marker token_indices")
        marker_keys = [
            (marker.span.start, marker.span.end, marker.category, marker.token_indices)
            for marker in markers
        ]
        if marker_keys != sorted(marker_keys):
            raise ValueError("markers must be canonically ordered")

        if self.evidence_sha256:
            _hash("evidence_sha256", self.evidence_sha256)
            expected = compute_evidence_hash(self)
            if self.evidence_sha256 != expected:
                raise ValueError("evidence_sha256 does not match canonical evidence")
        else:
            object.__setattr__(self, "evidence_sha256", compute_evidence_hash(self))


__all__ = [
    "DependencyEdge",
    "MarkerEvidence",
    "ScalarCueEvidence",
    "ScalarDependencyEvidence",
    "ScalarDependencyEvidenceReceipt",
    "SourceBoundSpan",
    "SUPPORTED_EVIDENCE_VERSIONS",
    "SUPPORTED_SCHEMA_VERSIONS",
    "TokenEvidence",
    "canonical_evidence_json",
    "compute_evidence_hash",
    "source_slice_sha256",
    "verify_evidence_hash",
]
