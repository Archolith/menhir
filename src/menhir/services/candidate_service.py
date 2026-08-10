"""CandidateService - approve/reject workflow for the CANDIDATE review tier.

Candidates (scope='CANDIDATE') are low-trust, human-reviewable signals written
directly by emitters such as cth.painscan. They are not recalled until approved.

This service is the canonical, contradiction-checked approval path used by the
running backend (MCP / agent-driven approval):

  approve(uuid): fetch -> promote CANDIDATE -> PERSISTENT -> run the SAME
                 contradiction check consolidation uses on freshly-promoted nodes
                 (LifecycleService._check_contradictions_batch). The check is
                 best-effort: a search failure never blocks an approval.
  reject(uuid):  delete the CANDIDATE node.

Note: deliberately does NOT use the lifecycle "user_flagged" auto-promote shortcut,
which skips the contradiction check. The explorer review surface performs the same
scope transition directly against its Neo4j repo and relies on the scheduled conflict
scan (which already scans PERSISTENT nodes) for the contradiction check.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.services.lifecycle_service import LifecycleService

logger = logging.getLogger(__name__)


@dataclass
class CandidateService:
    """Approve/reject transitions for CANDIDATE-scope memory nodes."""

    graph_adapter: MemoryGraphAdapter
    lifecycle_service: LifecycleService

    async def list_candidates(
        self, *, source: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self.graph_adapter.list_candidates, source=source, limit=limit
        )

    async def approve(self, uuid: str) -> dict[str, Any]:
        """Approve a candidate: promote to PERSISTENT, then run the contradiction check.

        Returns a status dict. The contradiction check is best-effort - if semantic
        search is unavailable the promotion still stands and conflicts are caught by
        the next scheduled conflict scan.
        """
        candidate = await asyncio.to_thread(self.graph_adapter.fetch_candidate, uuid)
        if candidate is None:
            return {"status": "not_found", "uuid": uuid}

        promoted = await asyncio.to_thread(self.graph_adapter.promote_candidate, uuid)
        if not promoted:
            # Lost a race (already promoted/rejected) - treat as not found.
            return {"status": "not_found", "uuid": uuid}

        conflicts_detected = 0
        try:
            conflicts_detected = await self.lifecycle_service._check_contradictions_batch(
                [
                    {
                        "uuid": uuid,
                        "content": candidate.get("content") or "",
                        "name": candidate.get("name") or "",
                    }
                ]
            )
        except Exception:  # best-effort: never fail an approval on a check error
            logger.warning("Contradiction check failed for approved candidate=%s", uuid, exc_info=True)

        return {
            "status": "approved",
            "uuid": uuid,
            "scope": "PERSISTENT",
            "cluster_id": candidate.get("cluster_id"),
            "conflicts_detected": conflicts_detected,
        }

    async def reject(self, uuid: str) -> dict[str, Any]:
        """Reject a candidate: delete the CANDIDATE node. Returns a status dict."""
        candidate = await asyncio.to_thread(self.graph_adapter.fetch_candidate, uuid)
        if candidate is None:
            return {"status": "not_found", "uuid": uuid}

        rejected = await asyncio.to_thread(self.graph_adapter.reject_candidate, uuid)
        if not rejected:
            return {"status": "not_found", "uuid": uuid}

        return {
            "status": "rejected",
            "uuid": uuid,
            "cluster_id": candidate.get("cluster_id"),
        }
