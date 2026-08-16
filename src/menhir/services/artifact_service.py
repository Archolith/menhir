"""ArtifactService — R9-lite single-writer facade for L4 institutional artifacts.

The menhir-side projection of the bench ArtifactMutator (archolith_bench/l4/mutator.py;
plan .agent/plans/l4-artifact-loop-v0.md). It is the one place application/agent code
goes through to write an artifact, so the L4 invariants are enforced in exactly one spot:

  capture(...)  — applies the create-time trust policy (domain.artifacts.decide_status):
                  an LLM artifact is never born TRUSTED (inv. 4); a human one is TRUSTED
                  only with >=1 evidence anchor (inv. 5). There is no status parameter,
                  so trust cannot be forged by the caller.
  promote(id)   — review gate. Refuses without evidence (inv. 3) and refuses a superseded
                  artifact (inv. 7) before touching the graph; the repository Cypher
                  enforces the same guard as defense-in-depth.
  supersede(...)— refuses self-supersession, then marks the old artifact historical and
                  links the new one. Never deletes (inv. 7).

Mirrors CandidateService: thin, async (offloads blocking Neo4j calls to a thread),
returns plain status dicts. The contradiction/decay machinery that governs PERSISTENT
nodes already applies once an artifact is trusted — no parallel governance is added here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from menhir.domain.artifacts import (
    ArtifactSource,
    ArtifactStatus,
    Evidence,
    can_promote,
    confidence_for,
    decide_status,
    has_promotable_evidence,
)

if TYPE_CHECKING:
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

logger = logging.getLogger(__name__)


def _evidence_dicts(evidence: Sequence[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for e in evidence or ():
        if isinstance(e, Evidence):
            out.append(e.to_dict())
        elif isinstance(e, Mapping):
            out.append(dict(e))
    return out


@dataclass
class ArtifactService:
    """Single-writer facade enforcing the L4 trust invariants (R9-lite)."""

    graph_adapter: "MemoryGraphAdapter"

    async def capture(
        self,
        *,
        artifact_id: str,
        artifact_type: Any,
        summary: str,
        source: Any,
        body: str = "",
        evidence: Sequence[Any] = (),
        anchors: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Write an artifact at the policy-decided trust level. No status param by design."""
        src = ArtifactSource(getattr(source, "value", source))
        ev = _evidence_dicts(evidence)
        promotable = has_promotable_evidence(str(e.get("kind", "")) for e in ev)
        status = decide_status(source=src, has_promotable_evidence=promotable)
        row = await asyncio.to_thread(
            self.graph_adapter.create_artifact,
            artifact_id=artifact_id,
            artifact_type=str(getattr(artifact_type, "value", artifact_type)),
            summary=summary,
            source=src.value,
            status=status.value,
            body=body,
            evidence=ev,
            anchors=list(anchors),
            source_confidence=confidence_for(status=status, source=src),
        )
        return {"status": status.value, "artifact_id": artifact_id, **row}

    async def promote(self, artifact_id: str, *, reviewed_by: str = "human") -> dict[str, Any]:
        """Promote a CANDIDATE artifact to TRUSTED — the review action. Fail-closed."""
        art = await asyncio.to_thread(self.graph_adapter.fetch_artifact, artifact_id)
        if art is None:
            return {"status": "not_found", "artifact_id": artifact_id}
        promotable = has_promotable_evidence(str(e.get("kind", "")) for e in art.get("evidence") or [])
        is_historical = art.get("status") == "historical" or bool(art.get("superseded_by"))
        if not can_promote(has_promotable_evidence=promotable, is_historical=is_historical):
            reason = "historical" if is_historical else "no_promotable_evidence"
            return {"status": "refused", "reason": reason, "artifact_id": artifact_id}
        trusted_confidence = confidence_for(status=ArtifactStatus.TRUSTED, source=ArtifactSource.HUMAN)
        promoted = await asyncio.to_thread(
            self.graph_adapter.promote_artifact, artifact_id, trusted_confidence=trusted_confidence
        )
        if not promoted:
            return {"status": "not_found", "artifact_id": artifact_id}
        return {"status": "trusted", "artifact_id": artifact_id, "reviewed_by": reviewed_by}

    async def supersede(self, old_id: str, new_id: str) -> dict[str, Any]:
        """Mark old_id historical and link the superseding new_id. Never deletes."""
        if old_id == new_id:
            return {"status": "refused", "reason": "self_supersede", "artifact_id": old_id}
        ok = await asyncio.to_thread(self.graph_adapter.supersede_l4_artifact, old_id, new_id)
        if not ok:
            return {"status": "not_found", "old_id": old_id, "new_id": new_id}
        return {"status": "superseded", "old_id": old_id, "new_id": new_id}
