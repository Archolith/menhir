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
        self, *, source: str | None = None, limit: int = 100, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        scope = {"namespace": namespace} if namespace else {}
        return await asyncio.to_thread(
            self.graph_adapter.list_candidates, source=source, limit=limit, **scope
        )

    async def _name_embedding(self, name: str) -> list[float] | None:
        """Embed the candidate's name the way Graphiti embeds an entity name, or None.

        Candidates are written by raw Cypher with no ``name_embedding``; without one an approved
        candidate is invisible to vector recall (#431). Best-effort: no embedder or a failed call
        still approves, and the result reports ``name_embedded: false``.
        """
        text = (name or "").replace("\n", " ").strip()
        client = getattr(self.lifecycle_service, "graphiti_client", None)
        embed = getattr(client, "embed_query", None)
        if not text or embed is None:
            return None
        try:
            vector = await embed(text)
        except Exception:  # best-effort: never fail an approval on an embed error
            logger.warning("Name embedding failed for approved candidate", exc_info=True)
            return None
        return list(vector) if vector else None

    async def approve(self, uuid: str, *, namespace: str | None = None) -> dict[str, Any]:
        """Approve a candidate: promote to PERSISTENT, then run the contradiction check.

        Returns a status dict. The contradiction check is best-effort - if semantic
        search is unavailable the promotion still stands and conflicts are caught by
        the next scheduled conflict scan. ``namespace`` (opt-in) confines the fetch and the
        promotion to that silo; a candidate in another silo reads as not_found.
        """
        scope: dict[str, Any] = {"namespace": namespace} if namespace else {}
        candidate = await asyncio.to_thread(self.graph_adapter.fetch_candidate, uuid, **scope)
        if candidate is None:
            return {"status": "not_found", "uuid": uuid}

        embedding = await self._name_embedding(str(candidate.get("name") or ""))
        promote_kwargs = dict(scope)
        if embedding is not None:
            promote_kwargs["name_embedding"] = embedding
        promoted = await asyncio.to_thread(
            self.graph_adapter.promote_candidate, uuid, **promote_kwargs
        )
        if not promoted:
            # Lost a race (already promoted/rejected) - treat as not found.
            return {"status": "not_found", "uuid": uuid}

        conflicts_detected = 0
        try:
            # The contradiction check scopes semantic search by namespace. The key is set
            # only when the fetch projection carried one, so batch shape stays stable for
            # adapters whose candidates have no namespace.
            batch_entry: dict[str, Any] = {
                "uuid": uuid,
                "content": candidate.get("content") or "",
                "name": candidate.get("name") or "",
            }
            if candidate.get("namespace"):
                batch_entry["namespace"] = candidate.get("namespace")
            conflicts_detected = await self.lifecycle_service._check_contradictions_batch(
                [batch_entry]
            )
        except Exception:  # best-effort: never fail an approval on a check error
            logger.warning("Contradiction check failed for approved candidate=%s", uuid, exc_info=True)

        return {
            "status": "approved",
            "uuid": uuid,
            "scope": "PERSISTENT",
            "cluster_id": candidate.get("cluster_id"),
            "conflicts_detected": conflicts_detected,
            "name_embedded": embedding is not None,
        }

    async def reject(self, uuid: str, *, namespace: str | None = None) -> dict[str, Any]:
        """Reject a candidate: delete the CANDIDATE node. Returns a status dict.

        ``namespace`` (opt-in) confines the fetch and the delete to that silo.
        """
        scope: dict[str, Any] = {"namespace": namespace} if namespace else {}
        candidate = await asyncio.to_thread(self.graph_adapter.fetch_candidate, uuid, **scope)
        if candidate is None:
            return {"status": "not_found", "uuid": uuid}

        rejected = await asyncio.to_thread(self.graph_adapter.reject_candidate, uuid, **scope)
        if not rejected:
            return {"status": "not_found", "uuid": uuid}

        return {
            "status": "rejected",
            "uuid": uuid,
            "cluster_id": candidate.get("cluster_id"),
        }
