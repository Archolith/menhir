"""Domain-neutral primitives for the Menhir mutation spike.

This module intentionally lives outside ``src/menhir``.  It is a research seam, not a
replacement implementation.  The question it tests is whether the durable parts of Menhir's
current scalar path can be expressed without coding- or scalar-specific vocabulary.

The kernel owns:
* source-grounded assertion identity;
* provenance and authority;
* explicit supersession;
* deterministic View envelopes.

Extensions own:
* assertion value semantics;
* fold algebra;
* admission rules;
* rendering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Iterable, Protocol, Sequence


AUTHORITY_ORDER: tuple[str, ...] = ("agent", "trusted_tool", "manual", "user")


def authority_rank(authority: str) -> int:
    """Return a stable trust ordering. Unknown authorities rank below the known set."""
    try:
        return AUTHORITY_ORDER.index(authority)
    except ValueError:
        return -1


@dataclass(frozen=True)
class EvidenceRef:
    """One source receipt behind an assertion.

    ``source_id`` is intentionally generic: an episode UUID, incident UUID, document ID,
    sensor event ID, etc.  Span offsets are optional because not every source is text.
    """

    source_id: str
    source_kind: str
    span_start: int | None = None
    span_end: int | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise ValueError("EvidenceRef.source_id must be non-empty")
        if not self.source_kind.strip():
            raise ValueError("EvidenceRef.source_kind must be non-empty")
        if (self.span_start is None) != (self.span_end is None):
            raise ValueError("span_start and span_end must be supplied together")
        if self.span_start is not None and (
            self.span_start < 0 or self.span_end is None or self.span_end < self.span_start
        ):
            raise ValueError("invalid evidence span")


def build_source_key(
    source_id: str,
    *,
    span_start: int | None = None,
    span_end: int | None = None,
    ordinal: int = 0,
) -> str:
    """Stable source locator with no extracted semantics.

    This mirrors the useful property of Menhir's current TypedAssertion source identity:
    reinterpretation can change the asserted meaning without inventing a new source event.
    """

    if ordinal < 0:
        raise ValueError("ordinal must be >= 0")
    h = hashlib.sha256()
    for part in (
        source_id.strip(),
        "" if span_start is None else str(span_start),
        "" if span_end is None else str(span_end),
        str(ordinal),
    ):
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def _stable_value(value: Any) -> str:
    """Best-effort deterministic representation for assertion identity."""

    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        value = asdict(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def build_assertion_id(
    *,
    source_key: str,
    assertion_type: str,
    subject_id: str,
    scope: str,
    key: str,
    value: Any,
    interpreter_version: str = "v1",
) -> str:
    """Hash one interpretation of a grounded source observation."""

    h = hashlib.sha256()
    for part in (
        source_key,
        interpreter_version.strip(),
        assertion_type.strip().lower(),
        subject_id.strip(),
        scope.strip().lower(),
        key.strip().lower(),
        _stable_value(value),
    ):
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


@dataclass(frozen=True)
class Assertion:
    """One immutable, provenance-bearing interpretation produced by an extension."""

    assertion_id: str
    source_key: str
    assertion_type: str
    subject_id: str
    scope: str
    key: str
    value: Any
    valid_at: str
    learned_at: str
    authority: str = "agent"
    confidence: float = 1.0
    evidence: tuple[EvidenceRef, ...] = ()
    supersedes: tuple[str, ...] = ()
    interpreter_version: str = "v1"
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for name, value in (
            ("assertion_id", self.assertion_id),
            ("source_key", self.source_key),
            ("assertion_type", self.assertion_type),
            ("subject_id", self.subject_id),
            ("scope", self.scope),
            ("key", self.key),
            ("valid_at", self.valid_at),
            ("learned_at", self.learned_at),
        ):
            if not str(value).strip():
                raise ValueError(f"Assertion.{name} must be non-empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Assertion.confidence must be between 0 and 1")
        if self.assertion_id in self.supersedes:
            raise ValueError("an assertion cannot supersede itself")


def make_assertion(
    *,
    source: EvidenceRef,
    assertion_type: str,
    subject_id: str,
    scope: str,
    key: str,
    value: Any,
    valid_at: str,
    learned_at: str | None = None,
    authority: str = "agent",
    confidence: float = 1.0,
    ordinal: int = 0,
    supersedes: Sequence[str] = (),
    interpreter_version: str = "v1",
    metadata: Sequence[tuple[str, str]] = (),
) -> Assertion:
    """Convenience constructor that preserves source-key / interpretation-key separation."""

    source_key = build_source_key(
        source.source_id,
        span_start=source.span_start,
        span_end=source.span_end,
        ordinal=ordinal,
    )
    assertion_id = build_assertion_id(
        source_key=source_key,
        assertion_type=assertion_type,
        subject_id=subject_id,
        scope=scope,
        key=key,
        value=value,
        interpreter_version=interpreter_version,
    )
    return Assertion(
        assertion_id=assertion_id,
        source_key=source_key,
        assertion_type=assertion_type,
        subject_id=subject_id,
        scope=scope,
        key=key,
        value=value,
        valid_at=valid_at,
        learned_at=learned_at or valid_at,
        authority=authority,
        confidence=confidence,
        evidence=(source,),
        supersedes=tuple(supersedes),
        interpreter_version=interpreter_version,
        metadata=tuple(metadata),
    )


def current_assertions(assertions: Iterable[Assertion]) -> list[Assertion]:
    """Apply explicit supersession without inventing any domain semantics."""

    rows = list(assertions)
    superseded = {target for row in rows for target in row.supersedes}
    return sorted(
        (row for row in rows if row.assertion_id not in superseded),
        key=lambda row: (
            row.subject_id,
            row.scope,
            row.assertion_type,
            row.key,
            row.valid_at,
            row.learned_at,
            row.assertion_id,
        ),
    )


def effective_authority(assertions: Iterable[Assertion]) -> str:
    """A derived View is only as authoritative as its weakest required contributor."""

    rows = list(assertions)
    if not rows:
        return "agent"
    return min((row.authority for row in rows), key=authority_rank)


@dataclass(frozen=True)
class View:
    """A rebuildable derived answer plus the exact assertions that produced it."""

    view_type: str
    subject_id: str
    scope: str
    key: str
    value: Any
    confidence: float
    contributor_ids: tuple[str, ...]
    counterevidence_ids: tuple[str, ...] = ()
    effective_authority: str = "agent"
    rationale: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("View.confidence must be between 0 and 1")


@dataclass(frozen=True)
class Abstention:
    """A deterministic refusal to materialize a View."""

    view_type: str
    subject_id: str
    scope: str
    key: str
    reason: str
    assertion_ids: tuple[str, ...] = ()


class Fold(Protocol):
    """Minimal extension seam: immutable assertions in, View or Abstention out."""

    def fold(self, assertions: Iterable[Assertion]) -> View | Abstention:
        ...


def parse_when(value: str) -> datetime:
    """Small ISO-8601 parser for the spike; invalid timestamps fail closed."""

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
