"""Source-bound span, token, edge, cue, and marker evidence types for scalar dependencies."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from menhir.domain.scalar_dependency_evidence_limits import (
    MAX_CATEGORY_LENGTH,
    MAX_CUES,
    MAX_LABEL_LENGTH,
    MAX_POS_TAG_LENGTH,
    MAX_TOKENS,
)
from menhir.domain.scalar_dependency_evidence_validation import (
    _bounded_tuple,
    _hash,
    _nonnegative,
    _text,
    _validate_bounds,
)


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
