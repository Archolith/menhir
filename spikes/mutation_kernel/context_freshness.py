"""Freshness-preserving retrieval/context envelopes for mutation-kernel projections.

Experiment 17 proved a generic reader can distinguish fresh, stale, and unavailable projection state.
That protection is useless if downstream retrieval or rendering discards the freshness envelope and
passes only human-readable text to an agent.

This module makes freshness structural context metadata owned by generic composition machinery.
Extension renderers control presentation text and namespaced metadata only; they cannot author or
overwrite `core.*` fields such as freshness, semantic versions, projection hashes, or derivation IDs.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Protocol, Sequence

from .freshness import FreshProjectionRead
from .kernel import Abstention, Retirement, View
from .neo4j_store import ProjectionOutcome, _hash_parts, _stable_json
from .registry import ProjectionTarget


@dataclass(frozen=True)
class RenderedProjectionContent:
    """Extension-owned presentation result.

    Metadata is intentionally opaque, except the `core.*` namespace is reserved for generic
governance/freshness fields and cannot be written by renderers.
    """

    text: str
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("RenderedProjectionContent.text must be non-empty")
        canonical = tuple(sorted((str(name), str(value)) for name, value in self.metadata))
        names = [name for name, _ in canonical]
        if any(not name for name in names) or len(names) != len(set(names)):
            raise ValueError("rendered metadata names must be unique non-empty strings")
        if any(name == "core" or name.startswith("core.") for name in names):
            raise ValueError("extension renderer cannot write reserved core.* metadata")
        if self.metadata != canonical:
            raise ValueError("rendered metadata must be canonical")


class ProjectionContextRenderer(Protocol):
    def render(self, outcome: ProjectionOutcome) -> RenderedProjectionContent: ...


@dataclass(frozen=True)
class ProjectionContextCandidate:
    """One retrieval/context candidate with generic freshness and lineage that renderers cannot edit."""

    candidate_id: str
    target: ProjectionTarget
    text: str
    freshness: str
    freshness_reason: str
    resolver_id: str
    current_resolver_version: int
    certified_resolver_version: int | None
    projection_hash: str
    certified_projection_hash: str | None
    derivation_id: str | None
    contributor_ids: tuple[str, ...]
    counterevidence_ids: tuple[str, ...]
    effective_authority: str | None
    base_score: float
    extension_metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.freshness not in {"fresh", "stale"}:
            raise ValueError("context candidate freshness must be fresh or stale")
        for name in (
            "candidate_id",
            "text",
            "freshness_reason",
            "resolver_id",
            "projection_hash",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"ProjectionContextCandidate.{name} must be non-empty")
        if self.current_resolver_version < 1:
            raise ValueError("current_resolver_version must be >= 1")
        if not math.isfinite(self.base_score):
            raise ValueError("base_score must be finite")
        if self.contributor_ids != tuple(sorted(set(self.contributor_ids))):
            raise ValueError("context contributor IDs must be canonical")
        if self.counterevidence_ids != tuple(sorted(set(self.counterevidence_ids))):
            raise ValueError("context counterevidence IDs must be canonical")
        canonical_metadata = tuple(
            sorted((str(name), str(value)) for name, value in self.extension_metadata)
        )
        names = [name for name, _ in canonical_metadata]
        if any(name == "core" or name.startswith("core.") for name in names):
            raise ValueError("context extension metadata cannot use reserved core.* namespace")
        if self.extension_metadata != canonical_metadata:
            raise ValueError("context extension metadata must be canonical")
        if self.freshness == "fresh":
            if self.certified_resolver_version != self.current_resolver_version:
                raise ValueError("fresh context candidate must certify current resolver version")
            if self.certified_projection_hash != self.projection_hash:
                raise ValueError("fresh context candidate must certify current projection hash")
            if not self.derivation_id:
                raise ValueError("fresh context candidate requires certified derivation ID")

    @property
    def freshness_rank(self) -> int:
        return 1 if self.freshness == "fresh" else 0

    def to_context_record(self) -> dict[str, Any]:
        """Serialize with core governance structurally separate from extension presentation data."""
        return {
            "candidate_id": self.candidate_id,
            "core": {
                "freshness": self.freshness,
                "freshness_reason": self.freshness_reason,
                "resolver_id": self.resolver_id,
                "current_resolver_version": self.current_resolver_version,
                "certified_resolver_version": self.certified_resolver_version,
                "projection_hash": self.projection_hash,
                "certified_projection_hash": self.certified_projection_hash,
                "derivation_id": self.derivation_id,
                "target": {
                    "view_type": self.target.view_type,
                    "subject_id": self.target.subject_id,
                    "scope": self.target.scope,
                    "key": self.target.key,
                    "dimensions": [list(pair) for pair in self.target.dimensions],
                },
                "contributor_ids": list(self.contributor_ids),
                "counterevidence_ids": list(self.counterevidence_ids),
                "effective_authority": self.effective_authority,
            },
            "rendered": {
                "text": self.text,
                "metadata": [list(pair) for pair in self.extension_metadata],
            },
        }


def _outcome_lineage(
    outcome: ProjectionOutcome,
) -> tuple[tuple[str, ...], tuple[str, ...], str | None]:
    if isinstance(outcome, View):
        return (
            tuple(sorted(set(outcome.contributor_ids))),
            tuple(sorted(set(outcome.counterevidence_ids))),
            outcome.effective_authority,
        )
    if isinstance(outcome, Abstention):
        # Abstention assertion_ids represent the evidence responsible for no current answer. Preserve
        # them as counterevidence-like lineage rather than inventing contributors.
        return (), tuple(sorted(set(outcome.assertion_ids))), None
    if isinstance(outcome, Retirement):
        return tuple(sorted(set(outcome.contributor_ids))), (), None
    raise TypeError(f"unsupported projection outcome for context: {type(outcome)!r}")


class FreshProjectionContextComposer:
    """Turn a freshness-aware read into a context candidate without letting renderers relabel it."""

    def compose(
        self,
        read: FreshProjectionRead,
        *,
        renderer: ProjectionContextRenderer,
        base_score: float = 1.0,
    ) -> ProjectionContextCandidate | None:
        if read.state == "unavailable":
            # Critical: strict freshness refusal happens before extension rendering, so a renderer
            # never receives an outcome that the read contract said must not be served.
            return None
        if read.state not in {"fresh", "stale"} or read.outcome is None:
            raise ValueError("fresh/stale context read requires an outcome")
        if read.projection_hash is None or read.current_resolver_version is None:
            raise ValueError("context read is missing required semantic/projection identity")

        rendered = renderer.render(read.outcome)
        contributors, counterevidence, effective_authority = _outcome_lineage(read.outcome)
        identity_payload = {
            "resolver_id": read.resolver_id,
            "current_resolver_version": read.current_resolver_version,
            "projection_hash": read.projection_hash,
            "derivation_id": read.derivation_id,
            "target": {
                "view_type": read.target.view_type,
                "subject_id": read.target.subject_id,
                "scope": read.target.scope,
                "key": read.target.key,
                "dimensions": [list(pair) for pair in read.target.dimensions],
            },
        }
        candidate_id = _hash_parts("projection-context-candidate", _stable_json(identity_payload))
        return ProjectionContextCandidate(
            candidate_id=candidate_id,
            target=read.target,
            text=rendered.text,
            freshness=read.state,
            freshness_reason=read.reason,
            resolver_id=read.resolver_id,
            current_resolver_version=read.current_resolver_version,
            certified_resolver_version=read.certified_resolver_version,
            projection_hash=read.projection_hash,
            certified_projection_hash=read.certified_projection_hash,
            derivation_id=read.derivation_id,
            contributor_ids=contributors,
            counterevidence_ids=counterevidence,
            effective_authority=effective_authority,
            base_score=float(base_score),
            extension_metadata=rendered.metadata,
        )


def rank_context_candidates(
    candidates: Sequence[ProjectionContextCandidate],
    *,
    freshness_policy: str = "fresh_first",
) -> tuple[ProjectionContextCandidate, ...]:
    """Small retrieval pressure test: ranking may prefer stale less, but never rewrites freshness."""
    if freshness_policy == "fresh_first":
        return tuple(
            sorted(
                candidates,
                key=lambda item: (
                    -item.freshness_rank,
                    -item.base_score,
                    item.candidate_id,
                ),
            )
        )
    if freshness_policy == "score_only":
        return tuple(
            sorted(
                candidates,
                key=lambda item: (-item.base_score, item.candidate_id),
            )
        )
    raise ValueError("unsupported context freshness ranking policy")
