"""MCP tool: retire_memory."""

from __future__ import annotations

import json

from menhir.mcp.telemetry.tracker import McpToolRefusal
from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def retire_memory(node_uuid: str, reason: str = "", namespace: str = "") -> str:
    """Retire a memory: hide it from default recall without changing anything else.

    Retiring is a visibility change only. Edges, typed assertions, scalar Views, counts,
    history and erasure reach are untouched, and restore_memory returns the memory to exactly
    its prior visibility. Default recall, pins and listings skip a retired memory; recall_history
    and recall_timeline still show it, labelled retired. Automatic decay, TTL deletion and merging
    leave a retired memory alone.

    Refused for Views, CANDIDATE memories, episodes still PENDING/ENRICHING, and flagged or
    retention-protected memories (unflag first). Idempotent: retiring a retired memory succeeds
    and keeps its first retirement time.

    Args:
        node_uuid: The UUID of the memory to retire.
        reason: Why it is retired (kept on the node for later review).

    Returns:
        JSON with status ("retired" or "already_retired"), uuid and retired_at.
    """

    return await RetireMemoryTool().execute(node_uuid=node_uuid, reason=reason, namespace=namespace)


class RetireMemoryTool(BaseTextTool):
    name = "retire_memory"
    scope = ToolScope.NAMESPACED
    required_tier = "operator"
    title = "Retire Memory"
    oauth_scopes = ("menhir:admin",)
    read_only_hint = False
    destructive_hint = False
    open_world_hint = False
    description = "Hide a memory from default recall (visibility only; reversible with restore_memory)."

    async def endpoint(self, node_uuid: str, reason: str = "", namespace: str = "") -> str:
        """Retire a memory: hide it from default recall without changing anything else.

        Args:
            node_uuid: The UUID of the memory to retire. Get this from recall_memories results.
            reason: Why it is retired (kept on the node for later review).
            namespace: Restrict the operation to a single silo. A pinned client has this
                forced -- and the parameter existing is what makes that possible, since the pin
                is injected only into endpoints whose signature declares it.

        Returns:
            JSON with status ("retired" or "already_retired"), uuid and retired_at.
        """
        backend = self.get_backend()
        # Ownership guard (ET-002), as in promote_memory: a uuid is an identifier, not proof of
        # tenancy. Only a node that demonstrably belongs to another silo is refused; a node that
        # does not exist at all stays a plain not_found.
        ns = namespace.strip() or None
        if ns is not None and await backend.fetch_memory_by_uuid(
            node_uuid, namespace=ns
        ) is None:
            if await backend.fetch_memory_by_uuid(node_uuid) is not None:
                raise McpToolRefusal(
                    f"Refused: memory {node_uuid} exists but is outside namespace {ns}.",
                    code="outside_namespace",
                )
        result = await backend.retire_memory(node_uuid, reason=reason)
        status = result.get("status")
        if status in ("retired", "already_retired"):
            return json.dumps(
                {"status": status, "uuid": node_uuid, "retired_at": result.get("retired_at")},
                sort_keys=True,
            )
        if status == "refused":
            raise McpToolRefusal(
                f"Refused: cannot retire memory {node_uuid}: {result.get('detail')}",
                code=str(result.get("code") or "refused"),
            )
        raise McpToolRefusal(f"No memory found with uuid={node_uuid}.", code="not_found")
