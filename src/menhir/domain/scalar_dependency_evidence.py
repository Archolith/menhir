"""Immutable, parser-neutral transport for scalar dependency evidence.

This module deliberately contains no parser or source-revalidation logic.  Parser output is an
untrusted, versioned observation; callers must revalidate every span against the original source
before allowing it to influence a proposal or composition.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


MAX_SOURCE_LENGTH = 1_000_000
MAX_TOKENS = 512
MAX_EDGES = 1_024
MAX_MARKERS = 128
MAX_CUES = 64
MAX_CHECK_NAMES = 64
MAX_VERSION_LENGTH = 64
MAX_PARSER_ID_LENGTH = 64
MAX_LABEL_LENGTH = 64
MAX_POS_TAG_LENGTH = 32
MAX_CATEGORY_LENGTH = 64
MAX_OUTCOME_LENGTH = 32
MAX_REASON_LENGTH = 256
MAX_RULE_VERSION_LENGTH = 64
MAX_COMPOSER_VERSION_LENGTH = 64
MAX_CHECK_NAME_LENGTH = 64
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_TOKEN_RE = re.compile(r"^[a-z0-9._-]{1,64}$")
SUPPORTED_SCHEMA_VERSIONS = frozenset({"scalar-dependency-v1"})
SUPPORTED_EVIDENCE_VERSIONS = frozenset({"parser-evidence-v1"})


def _int(name: str, value: object) -> int:
    if type(value) is not int:  # bool is an int subclass, but never a valid bound/index.
        raise ValueError(f"{name} must be an int, not bool")
    return value


def _nonnegative(name: str, value: object) -> int:
    value = _int(name, value)
    if value < 0:
        raise ValueError(f"{name} must be nonnegative")
    return value


def _text(name: str, value: object, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonblank string")
    if value != value.strip():
        raise ValueError(f"{name} must not have leading or trailing whitespace")
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds maximum length {maximum}")
    return value


def _hash(name: str, value: object) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 hex string")
    return value


def _version_token(name: str, value: object) -> str:
    if not isinstance(value, str) or _VERSION_TOKEN_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase version token")
    return value


def _tuple(name: str, value: object) -> tuple[Any, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{name} must be a tuple")
    return value


def _bounded_tuple(name: str, value: object, maximum: int) -> tuple[Any, ...]:
    value = _tuple(name, value)
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds maximum size {maximum}")
    return value


def _span(name: str, value: object) -> "SourceBoundSpan":
    if not isinstance(value, SourceBoundSpan):
        raise ValueError(f"{name} must be a SourceBoundSpan")
    return value


def _inside(inner: "SourceBoundSpan", outer: "SourceBoundSpan", name: str) -> None:
    if inner.start < outer.start or inner.end > outer.end:
        raise ValueError(f"{name} must be contained by the clause span")


def _token_bounds_inside(
    span: "SourceBoundSpan", tokens: tuple["TokenEvidence", ...], name: str
) -> None:
    if span.token_start is None:
        return
    assert span.token_end is not None
    if span.token_end > len(tokens):
        raise ValueError(f"{name} token bounds are outside the token range")
    expected_start = tokens[span.token_start].span.start
    expected_end = tokens[span.token_end - 1].span.end
    if span.start != expected_start or span.end != expected_end:
        raise ValueError(f"{name} source bounds do not align with token bounds")


def source_slice_sha256(source: str, start: int, end: int) -> str:
    """Hash an exact half-open source slice without retaining the source text."""

    if not isinstance(source, str):
        raise ValueError("source must be a string")
    _validate_bounds("source slice", start, end, len(source))
    return hashlib.sha256(source[start:end].encode("utf-8")).hexdigest()


def _validate_bounds(name: str, start: object, end: object, limit: int | None = None) -> None:
    start = _nonnegative(f"{name}.start", start)
    end = _nonnegative(f"{name}.end", end)
    if start >= end:
        raise ValueError(f"{name} must be a non-empty half-open span")
    if limit is not None and end > limit:
        raise ValueError(f"{name} exceeds containing bounds")


@dataclass(frozen=True, slots=True)
class SourceBoundSpan:
    """A source offset and hash, optionally bounded by parser token indices."""

    start: int
    end: int
    surface_sha256: str
    token_start: int | None = None
    token_end: int | None = None

    def __post_init__(self) -> None:
        _validate_bounds("span", self.start, self.end)
        _hash("surface_sha256", self.surface_sha256)
        if (self.token_start is None) != (self.token_end is None):
            raise ValueError("token_start and token_end must be supplied together")
        if self.token_start is not None and self.token_end is not None:
            _validate_bounds("token span", self.token_start, self.token_end)


@dataclass(frozen=True, slots=True)
class TokenEvidence:
    token_index: int
    span: SourceBoundSpan
    head_index: int
    dependency_label: str
    pos_tag: str
    lemma_sha256: str

    def __post_init__(self) -> None:
        _nonnegative("token_index", self.token_index)
        _span("span", self.span)
        if type(self.head_index) is not int or self.head_index < -1:
            raise ValueError("head_index must be an int >= -1")
        _text("dependency_label", self.dependency_label, MAX_LABEL_LENGTH)
        _text("pos_tag", self.pos_tag, MAX_POS_TAG_LENGTH)
        _hash("lemma_sha256", self.lemma_sha256)


@dataclass(frozen=True, slots=True)
class DependencyEdge:
    head_index: int
    dependent_index: int
    label: str

    def __post_init__(self) -> None:
        _nonnegative("head_index", self.head_index)
        _nonnegative("dependent_index", self.dependent_index)
        _text("label", self.label, MAX_LABEL_LENGTH)


@dataclass(frozen=True, slots=True)
class ScalarCueEvidence:
    """Parser-declared scalar cues, represented only by source-bound spans."""

    subject: SourceBoundSpan | None
    predicate: SourceBoundSpan | None
    numeric_value: SourceBoundSpan
    unit: SourceBoundSpan | None
    target: SourceBoundSpan | None
    modifiers: tuple[SourceBoundSpan, ...]
    scope: SourceBoundSpan | None
    clause_root_token: int

    def __post_init__(self) -> None:
        for name, value in (
            ("subject", self.subject),
            ("predicate", self.predicate),
            ("numeric_value", self.numeric_value),
            ("unit", self.unit),
            ("target", self.target),
            ("scope", self.scope),
        ):
            if value is not None:
                _span(name, value)
        modifiers = _bounded_tuple("modifiers", self.modifiers, MAX_CUES)
        for index, modifier in enumerate(modifiers):
            _span(f"modifiers[{index}]", modifier)
        _nonnegative("clause_root_token", self.clause_root_token)


@dataclass(frozen=True, slots=True)
class MarkerEvidence:
    """A parser-declared marker category and its source-bound occurrence.

    Marker tuples in an evidence envelope are canonically sorted by source span, category, and
    token indices.  Token indices themselves are sorted and unique; this ordering is semantic for
    deterministic transport hashes, not parser authority.
    """

    category: str
    span: SourceBoundSpan
    token_indices: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        _text("category", self.category, MAX_CATEGORY_LENGTH)
        _span("span", self.span)
        indices = _bounded_tuple("token_indices", self.token_indices, MAX_TOKENS)
        for index, token_index in enumerate(indices):
            _nonnegative(f"token_indices[{index}]", token_index)
        if indices != tuple(sorted(set(indices))):
            raise ValueError("token_indices must be sorted and unique")

    @property
    def kind(self) -> str:
        """Compatibility spelling for callers that call marker categories ``kind``."""

        return self.category


def _span_payload(span: SourceBoundSpan | None) -> object:
    if span is None:
        return None
    return {
        "end": span.end,
        "start": span.start,
        "surface_sha256": span.surface_sha256,
        "token_end": span.token_end,
        "token_start": span.token_start,
    }


def _cue_payload(cues: ScalarCueEvidence) -> dict[str, object]:
    return {
        "clause_root_token": cues.clause_root_token,
        "modifiers": [_span_payload(span) for span in cues.modifiers],
        "numeric_value": _span_payload(cues.numeric_value),
        "predicate": _span_payload(cues.predicate),
        "scope": _span_payload(cues.scope),
        "subject": _span_payload(cues.subject),
        "target": _span_payload(cues.target),
        "unit": _span_payload(cues.unit),
    }


def _marker_payload(marker: MarkerEvidence) -> dict[str, object]:
    return {
        "category": marker.category,
        "span": _span_payload(marker.span),
        "token_indices": list(marker.token_indices),
    }


def _evidence_payload(evidence: "ScalarDependencyEvidence") -> dict[str, object]:
    return {
        "candidate_hash": evidence.candidate_hash,
        "clause_span": _span_payload(evidence.clause_span),
        "cues": _cue_payload(evidence.cues),
        "edges": [
            {"dependent_index": edge.dependent_index, "head_index": edge.head_index, "label": edge.label}
            for edge in evidence.edges
        ],
        "evidence_version": evidence.evidence_version,
        "markers": [_marker_payload(marker) for marker in evidence.markers],
        "model_hash": evidence.model_hash,
        "parser_id": evidence.parser_id,
        "parser_version": evidence.parser_version,
        "pipeline_hash": evidence.pipeline_hash,
        "schema_version": evidence.schema_version,
        "source_hash": evidence.source_hash,
        "source_length": evidence.source_length,
        "tokens": [
            {
                "dependency_label": token.dependency_label,
                "head_index": token.head_index,
                "lemma_sha256": token.lemma_sha256,
                "pos_tag": token.pos_tag,
                "span": _span_payload(token.span),
                "token_index": token.token_index,
            }
            for token in evidence.tokens
        ],
    }


def canonical_evidence_json(evidence: "ScalarDependencyEvidence") -> str:
    """Return deterministic JSON for evidence, excluding ``evidence_sha256``."""

    return json.dumps(_evidence_payload(evidence), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_evidence_hash(evidence: "ScalarDependencyEvidence") -> str:
    return hashlib.sha256(canonical_evidence_json(evidence).encode("utf-8")).hexdigest()


def verify_evidence_hash(evidence: "ScalarDependencyEvidence") -> bool:
    return evidence.evidence_sha256 == compute_evidence_hash(evidence)


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


@dataclass(frozen=True, slots=True)
class ScalarDependencyEvidenceReceipt:
    """Quote-free, bounded receipt suitable for offline persistence."""

    schema_version: str
    evidence_version: str
    bridge_version: str
    source_hash: str
    candidate_hash: str
    evidence_sha256: str
    clause_start: int
    clause_end: int
    token_count: int
    edge_count: int
    marker_count: int
    outcome: str
    reason: str
    rule_version: str
    composer_version: str
    check_names: tuple[str, ...]

    def __post_init__(self) -> None:
        _version_token("schema_version", self.schema_version)
        _version_token("evidence_version", self.evidence_version)
        _version_token("bridge_version", self.bridge_version)
        _version_token("rule_version", self.rule_version)
        _version_token("composer_version", self.composer_version)
        _text("outcome", self.outcome, MAX_OUTCOME_LENGTH)
        _text("reason", self.reason, MAX_REASON_LENGTH)
        for name, value in (
            ("source_hash", self.source_hash),
            ("candidate_hash", self.candidate_hash),
            ("evidence_sha256", self.evidence_sha256),
        ):
            _hash(name, value)
        _validate_bounds("clause", self.clause_start, self.clause_end, MAX_SOURCE_LENGTH)
        for name, value in (
            ("token_count", self.token_count),
            ("edge_count", self.edge_count),
            ("marker_count", self.marker_count),
        ):
            _nonnegative(name, value)
        if self.token_count > MAX_TOKENS or self.edge_count > MAX_EDGES or self.marker_count > MAX_MARKERS:
            raise ValueError("receipt count exceeds transport bounds")
        checks = _bounded_tuple("check_names", self.check_names, MAX_CHECK_NAMES)
        for index, check in enumerate(checks):
            _text(f"check_names[{index}]", check, MAX_CHECK_NAME_LENGTH)


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
