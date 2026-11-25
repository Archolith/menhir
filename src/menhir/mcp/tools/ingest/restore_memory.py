"""MCP tool: restore_memory."""

from __future__ import annotations

import json

from menhir.mcp.telemetry.tracker import McpToolRefusal
from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def restore_memory(node_uuid: str, namespace: str = "") -> str:
    """Restore a retired memory to exactly its prior visibility (undoes retire_memory).

    Idempotent: restoring a memory that is not retired succeeds with status "not_retired".

    Args:
        node_uuid: The UUID of the memory to restore.

    Returns:
        JSON with status ("restored" or "not_retired") and uuid.
    """

    return await RestoreMemoryTool().execute(node_uuid=node_uuid, namespace=namespace)


class RestoreMemoryTool(BaseTextTool):
    name = "restore_memory"
    scope = ToolScope.NAMESPACED
    required_tier = "operator"
    title = "Restore Memory"
    oauth_scopes = ("menhir:admin",)
    read_only_hint = False
    destructive_hint = False
    open_world_hint = False
    description = "Restore a retired memory to its prior visibility (undoes retire_memory)."

    async def endpoint(self, node_uuid: str, namespace: str = "") -> str:
        """Restore a retired memory to exactly its prior visibility (undoes retire_memory).

        Args:
            node_uuid: The UUID of the memory to restore.
            namespace: Restrict the operation to a single silo. A pinned client has this
                forced -- and the parameter existing is what makes that possible, since the pin
                is injected only into endpoints whose signature declares it.

        Returns:
            JSON with status ("restored" or "not_retired") and uuid.
        """
        backend = self.get_backend()
        # Ownership guard (ET-002), as in promote_memory.
        ns = namespace.strip() or None
        if ns is not None and await backend.fetch_memory_by_uuid(
            node_uuid, namespace=ns
        ) is None:
            if await backend.fetch_memory_by_uuid(node_uuid) is not None:
                raise McpToolRefusal(
                    f"Refused: memory {node_uuid} exists but is outside namespace {ns}.",
                    code="outside_namespace",
                )
        result = await backend.restore_memory(node_uuid)
        status = result.get("status")
        if status in ("restored", "not_retired"):
            return json.dumps({"status": status, "uuid": node_uuid}, sort_keys=True)
        raise McpToolRefusal(f"No memory found with uuid={node_uuid}.", code="not_found")
