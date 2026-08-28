"""MCP tool: flag_memory."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def flag_memory(
    node_uuid: str, bootstrap_scope: str = "", namespace: str = ""
) -> str:
    """Flag a memory node for permanent retention. Flagged nodes survive lifecycle decay.

    Args:
        node_uuid: The UUID of the memory node to flag. Get this from recall_memories results.
        bootstrap_scope: Optional startup pin: general, workspace:<key>, or none.
        namespace: Restrict the operation to a single silo. A pinned client has this forced.

    Returns:
        Confirmation or failure message.
    """

    return await FlagMemoryTool().execute(
        node_uuid=node_uuid, bootstrap_scope=bootstrap_scope, namespace=namespace
    )


class FlagMemoryTool(BaseTextTool):
    name = "flag_memory"
    scope = ToolScope.NAMESPACED
    description = "Flag a memory node for permanent retention."
    title = "Flag Memory"
    oauth_scopes = ("menhir:write",)
    read_only_hint = False
    destructive_hint = False
    open_world_hint = False

    async def endpoint(
        self, node_uuid: str, bootstrap_scope: str = "", namespace: str = ""
    ) -> str:
        """Flag a memory node for permanent retention. Flagged nodes survive lifecycle decay.

        Args:
            node_uuid: The UUID of the memory node to flag. Get this from recall_memories results.
            namespace: Restrict the operation to a single silo. A pinned client has this forced.

        Returns:
            Confirmation or failure message.
        """
        backend = self.get_backend()
        # Ownership guard. Without the `namespace` parameter above, the pin cannot reach
        # this tool at all: _apply_pinned_namespace injects only into endpoints whose
        # signature names it. This one runs at the default `agent` tier, a lower bar than
        # delete_memory's `operator`.
        #
        # A node absent from the graph entirely falls through deliberately, so the existing
        # "No memory found" path below still reports it as such rather than as a namespace
        # refusal -- only a node that demonstrably belongs to another silo is refused.
        ns = namespace.strip() or None
        if ns is not None and await backend.fetch_memory_by_uuid(
            node_uuid, namespace=ns
        ) is None:
            if await backend.fetch_memory_by_uuid(node_uuid) is not None:
                return (
                    f"Refused: memory {node_uuid} exists but is outside namespace {ns}."
                )
        try:
            scope_arg = bootstrap_scope if bootstrap_scope.strip() else None
            flagged = await backend.flag_memory(
                node_uuid, bootstrap_scope=scope_arg
            )
        except ValueError as exc:
            return f"Cannot flag: {exc}"
        if flagged:
            row = await backend.fetch_memory_by_uuid(node_uuid)
            effective_scope = (row or {}).get("bootstrap_scope")
            return (
                f"Flagged memory {node_uuid} for permanent retention; "
                f"bootstrap_scope={effective_scope or 'none'}."
            )
        return f"No memory found with uuid={node_uuid}."
