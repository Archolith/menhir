"""MCP tool: review_candidate."""

from __future__ import annotations

from typing import Any

from menhir.mcp.contracts import ToolScope
from menhir.mcp.tools.base import BaseTextTool

_DECISIONS = ("approve", "reject")


async def review_candidate(uuid: str, decision: str, namespace: str = "") -> str:
    """Approve or reject a candidate staged with add_candidate.

    Approve makes it an ordinary memory: its name is embedded, it moves to PERSISTENT
    scope, and it is checked for contradictions with existing memories. Reject deletes it.
    Find candidates with list_candidates or review_inbox.

    Args:
        uuid: The candidate's uuid.
        decision: "approve" or "reject".
        namespace: Optional silo; a candidate outside it is refused.

    Returns:
        What happened, including any contradictions found on approval.
    """
    return await ReviewCandidateTool().execute(uuid=uuid, decision=decision, namespace=namespace)


class ReviewCandidateTool(BaseTextTool):
    name = "review_candidate"
    scope = ToolScope.NAMESPACED
    required_tier = "operator"
    title = "Review Candidate"
    oauth_scopes = ("menhir:admin",)
    read_only_hint = False
    destructive_hint = True
    open_world_hint = False
    description = (
        "Approve (ordinary memory, contradiction-checked) or reject (delete) a candidate "
        "staged with add_candidate."
    )

    async def endpoint(self, uuid: str, decision: str, namespace: str = "") -> str:
        """Approve or reject one CANDIDATE-scope memory.

        Args:
            uuid: The candidate's uuid.
            decision: "approve" or "reject".
            namespace: Restrict the operation to a single silo. A pinned client has this forced;
                the parameter existing is what makes that possible.
        """
        choice = (decision or "").strip().lower()
        if choice not in _DECISIONS:
            return f"Invalid decision {decision!r}: use 'approve' or 'reject'."
        uuid = (uuid or "").strip()
        if not uuid:
            return "A candidate uuid is required."
        backend = self.get_backend()
        ns = (namespace or "").strip() or None
        scope: dict[str, Any] = {"namespace": ns} if ns else {}
        # Ownership guard (ET-002), as in promote_memory: a uuid is not proof of tenancy. The
        # write below is also scoped in the same statement, so this read only picks the message;
        # it is never the authority. Only a candidate that demonstrably sits in another silo is
        # refused; one that does not exist is reported as not found.
        if ns is not None and await backend.fetch_candidate(uuid, namespace=ns) is None:
            if await backend.fetch_candidate(uuid) is not None:
                return f"Refused: candidate {uuid} exists but is outside namespace {ns}."
            return await self._not_found(backend, uuid, scope)

        if choice == "reject":
            if await backend.reject_candidate(uuid, **scope):
                return f"Rejected candidate {uuid}; it was deleted."
            return await self._not_found(backend, uuid, scope)

        result = await backend.approve_candidate(uuid, **scope)
        if not isinstance(result, dict) or result.get("status") != "approved":
            return await self._not_found(backend, uuid, scope)
        # The contradiction check can merge the new memory into a near-duplicate, which deletes
        # it; say so rather than report a memory that no longer exists.
        if await backend.fetch_memory_by_uuid(uuid, **scope) is None:
            return (
                f"Approved candidate {uuid}, then the contradiction check merged it into an "
                "existing near-duplicate memory; this uuid no longer exists."
            )
        lines = [f"Approved candidate {uuid}; it is now a PERSISTENT memory."]
        conflicts = int(result.get("conflicts_detected") or 0)
        if conflicts:
            lines.append(
                f"{conflicts} possible contradiction(s) with existing memories were found; "
                "see list_conflicts."
            )
        if result.get("name_embedded") is False:
            lines.append(
                "Its name could not be embedded, so semantic recall may miss it until it is "
                "re-embedded."
            )
        return "\n".join(lines)

    @staticmethod
    async def _not_found(backend: Any, uuid: str, scope: dict[str, Any]) -> str:
        # A retry after a timed-out approval lands here; an existing memory with this uuid means
        # the earlier approval committed.
        if await backend.fetch_memory_by_uuid(uuid, **scope) is not None:
            return (
                f"Candidate {uuid} is no longer awaiting review: a memory with this uuid exists, "
                "so it was already approved."
            )
        return f"No candidate awaiting review with uuid={uuid}."
