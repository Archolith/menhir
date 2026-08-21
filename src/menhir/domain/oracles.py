"""RetrievalOracle interface (R4) — the menhir-side port of the bench prototype.

The *retrieval* oracle altitude: per-candidate evidence scoring. An oracle OBSERVES a
candidate and returns an OracleResult (evidence, never final truth); it does not decide
(Warden) or write (Mutator). The objects are immutable value objects so oracle execution is
thread-safe and deterministic by construction.

This is the interface half (R4). The cheap oracles (R6) and the one-pass combiner (R7) build
on it. Mirrors `docs/research/oracle-amplified-retrieval.md` and the bench prototype
(`archolith_bench/oracle/models.py`), minus the bench-only fixture objects.

The R4 guarantee — *oracles are read-only* — is enforced structurally: a CandidateMemory's
metadata is an immutable mapping, so an oracle literally cannot mutate the candidate it is
handed (a stronger guarantee than the bench's "by convention").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Protocol, runtime_checkable

from menhir.domain.temporal_lens import TemporalLens

SCOPE_KEYS: tuple[str, ...] = ("repo", "branch", "project", "namespace")


class OraclePolarity(str, Enum):
    SUPPORT = "support"
    CONTRADICT = "contradict"
    MISSING = "missing"
    NEUTRAL = "neutral"


class OracleTarget(str, Enum):
    """Which retrieval property the oracle speaks to."""

    RELEVANCE = "relevance"
    CURRENTNESS = "currentness"
    HISTORICALITY = "historicality"
    CONFLICT = "conflict"
    SCOPE = "scope"
    SAFETY = "safety"


class Role(str, Enum):
    """Role-specific logits the combiner maintains (not one hidden score)."""

    RELEVANT = "relevant"
    CURRENT = "current"
    HISTORICAL = "historical"
    CONFLICT = "conflict"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class QueryContext:
    """Immutable query snapshot handed to every oracle."""

    text: str
    intent: str = TemporalLens.CURRENT  # one of TemporalLens: current | historical | any
    as_of_time: str | None = None
    repo: str | None = None
    branch: str | None = None
    project: str | None = None
    namespace: str | None = None
    files: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateMemory:
    """Immutable candidate handed to every oracle.

    Oracles read ``metadata`` (a prefetched snapshot) — they never fetch the world, and they
    cannot mutate it: ``__post_init__`` wraps metadata in an immutable MappingProxyType, so
    the R4 read-only invariant is structural, not just documented."""

    id: str
    content: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Freeze the mapping so an oracle cannot write through it (read-only guarantee).
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class OracleResult:
    """One oracle's evidence about one candidate. Evidence, never final truth."""

    oracle: str
    probability: float
    confidence: float = 1.0
    polarity: OraclePolarity = OraclePolarity.SUPPORT
    target: OracleTarget = OracleTarget.RELEVANCE
    directness: float = 1.0
    scope_match: float = 1.0
    source_family: str = "semantic"
    note: str = ""

    @staticmethod
    def neutral(oracle: str, *, note: str = "missing") -> "OracleResult":
        """A neutral/missing result — what an oracle returns when it has nothing to say
        (or on executor timeout). Missing is not falsity."""
        return OracleResult(
            oracle=oracle, probability=0.5, confidence=0.0,
            polarity=OraclePolarity.MISSING, note=note,
        )


@runtime_checkable
class RetrievalOracle(Protocol):
    """A read-only per-candidate evidence scorer. Implementations expose ONLY ``evaluate``."""

    name: str

    def evaluate(self, query: QueryContext, candidate: CandidateMemory) -> OracleResult: ...


@dataclass
class OraclePacket:
    """Per-candidate reduction of all oracle results (the R7 OraclePacket).

    This is the retrieval-shaped evidence packet — NOT the task-shaped ColdStartBrief. It
    carries role logits (relevant/current/historical/conflict/blocked), a combined ranking
    probability, an uncertainty (from missing oracles), and an explainable rationale. It is
    what the combiner produces and what the Wardens consume downstream."""

    candidate_id: str
    results: list[OracleResult]
    role_logits: dict[str, float]
    combined_probability: float
    uncertainty: float
    rationale: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "role_logits": {k: round(v, 4) for k, v in self.role_logits.items()},
            "combined_probability": round(self.combined_probability, 4),
            "uncertainty": round(self.uncertainty, 4),
            "rationale": self.rationale,
        }
