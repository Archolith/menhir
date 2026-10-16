"""MCP tool: get_scalar_view_items."""

from __future__ import annotations

from typing import Any

from menhir.mcp.tools.base import BaseJsonTool
from menhir.mcp.contracts import ToolScope


async def get_scalar_view_items(
    view_uuid: str,
    namespace: str = "",
    offset: int = 0,
    limit: int = 20,
) -> str:
    """List the items a scalar View was built from, page by page.

    Args:
        view_uuid: The scalar View uuid shown in recall's authority layer.
        namespace: Optional silo to scope this operation to. Empty = default/global behavior.
        offset: Offset into the View's items (default: 0).
        limit: Max items per page (default: 20, max: 50).
    """
    return await GetScalarViewItemsTool().execute(
        view_uuid=view_uuid, namespace=namespace, offset=offset, limit=limit
    )


class GetScalarViewItemsTool(BaseJsonTool):
    name = "get_scalar_view_items"
    scope = ToolScope.NAMESPACED
    required_tier = "readonly"
    title = "Get Scalar View Items"
    oauth_scopes = ("menhir:read",)
    read_only_hint = True
    destructive_hint = False
    open_world_hint = False
    description = (
        "List the items a scalar View (a count or current value in recall's authority layer) "
        "was built from, page by page, given its view_uuid."
    )

    async def endpoint(
        self,
        view_uuid: str,
        namespace: str = "",
        offset: int = 0,
        limit: int = 20,
    ) -> str:
        """Page the assertions behind one scalar View. Read-only: no access updates.

        namespace scopes the operation, subject to the client's configured pin.
        """
        backend = self.get_backend()
        result: dict[str, Any] = await backend.get_scalar_view_items(
            view_uuid=view_uuid,
            namespace=namespace or None,
            offset=offset,
            limit=limit,
        )
        return self.render_json(result)
