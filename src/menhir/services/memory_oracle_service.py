"""MemoryOracleService — read-only retrieval of L4 artifacts for a task.

The menhir-side projection of the bench MemoryOracle (archolith_bench/l4/memory_oracle.py).
Given a task (text + optional structural anchors) it returns the relevant artifacts ranked
by an explainable signal:

  - structural ANCHOR overlap (deterministic) — strong (+1.0);
  - topic token overlap (a lexical stand-in for embedding similarity) — weak.

It is READ-ONLY by construction: it only calls the adapter's find_artifacts and exposes no
create/promote/supersede. Artifacts are returned with their status intact (trusted /
candidate / historical); deciding how to *present* them — fact vs hypothesis vs flagged-
stale — is the ColdStartBrief's job, not the oracle's.

The ranking mirrors the bench oracle exactly so the bench stays the falsifiable spec for
this behavior; the only difference is the candidate set comes from Neo4j, not a list.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter


def _tokens(text: str) -> set[str]:
    return {t for t in "".join(c.lower() if c.isalnum() else " " for c in text or "").split() if len(t) > 1}


def _overlap_coefficient(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


@dataclass(frozen=True)
class ArtifactMatch:
    artifact: dict[str, Any]
    score: float
    matched_on: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class MemoryOracleService:
    """Read-only artifact retrieval. Never writes."""

    graph_adapter: "MemoryGraphAdapter"

    async def find(
        self, *, text: str, anchors: list[str] | None = None, limit: int = 10
    ) -> list[ArtifactMatch]:
        q_tokens = _tokens(text)
        q_anchors = set(anchors or [])
        # over-fetch from the store, then rank locally with the same logic as the bench.
        rows = await asyncio.to_thread(
            self.graph_adapter.find_artifacts,
            tokens=sorted(q_tokens),
            anchors=list(q_anchors),
            limit=max(limit * 3, limit),
        )
        matches: list[ArtifactMatch] = []
        for art in rows:
            matched: list[str] = []
            score = 0.0
            if q_anchors & set(art.get("anchors") or []):
                matched.append("anchor")
                score += 1.0
            topic = _overlap_coefficient(q_tokens, _tokens(f"{art.get('summary', '')} {art.get('body', '')}"))
            if topic > 0:
                matched.append("topic")
                score += topic
            if matched:
                matches.append(ArtifactMatch(artifact=art, score=round(score, 4), matched_on=tuple(matched)))
        matches.sort(key=lambda m: (-m.score, str(m.artifact.get("artifact_id", ""))))
        return matches[:limit]
