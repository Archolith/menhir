"""MCP tool: delete_namespace — tear down a namespace silo (operator only)."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseJsonTool


async def delete_namespace(
    namespace: str,
    max_nodes: int = 200,
    force: bool = False,
    dry_run: bool = False,
) -> str:
    """Tear down a throwaway/eval namespace silo. Refuses the default/shared namespace.

    Safety gate: counts nodes first. Refuses (with the actual count) when the
    namespace has more than max_nodes nodes, unless force=True. This is a
    blast-radius guard, not a backup — deletion is still irreversible once it
    proceeds. Use dry_run=True first to see the count and would-delete decision
    without deleting anything.

    Args:
        namespace: The namespace silo to delete. Cannot be the default/shared namespace.
        max_nodes: Safety gate — refuses deletion if the namespace has more nodes
            than this (default: 200).
        force: Bypass the max_nodes gate.
        dry_run: Report the node count and would-delete decision without deleting.

    Returns:
        JSON with the node count and outcome (dry_run report, deleted count, or error).
    """

    return await DeleteNamespaceTool().execute(
        namespace=namespace, max_nodes=max_nodes, force=force, dry_run=dry_run
    )


class DeleteNamespaceTool(BaseJsonTool):
    name = "delete_namespace"
    required_tier = "operator"
    description = "Tear down a throwaway/eval namespace silo, gated by node count."

    async def endpoint(
        self,
        namespace: str,
        max_nodes: int = 200,
        force: bool = False,
        dry_run: bool = False,
    ) -> str:
        backend = self.get_backend()
        try:
            result = await backend.delete_namespace(
                namespace, max_nodes=max_nodes, force=force, dry_run=dry_run
            )
        except ValueError as exc:
            return self.render_json({"error": str(exc), "namespace": namespace})
        return self.render_json(result)
