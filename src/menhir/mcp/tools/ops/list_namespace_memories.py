"""MCP tool: list_namespace_memories -- page a silo's erasable records by id (#476, operator)."""

from __future__ import annotations

from menhir.mcp.contracts import ToolScope
from menhir.mcp.tools.base import BaseJsonTool

KINDS = ("memory", "turn_evidence", "typed_assertion")


async def list_namespace_memories(
    namespace: str = "default",
    kind: str = "memory",
    limit: int = 50,
    after_id: str = "",
    source: str = "",
) -> str:
    """List one page of a namespace's records, with the ids delete_memories accepts.

    The way to remove data from the default namespace, which delete_namespace refuses: page
    through here, then pass the ids to delete_memories. Never lists structure rows, artifacts,
    Todos or WorkArtifacts.

    Args:
        namespace: The silo to list ('default' covers every stored spelling of it). A pinned
            client has this forced.
        kind: 'memory' (default), 'turn_evidence' or 'typed_assertion'.
        limit: Page size, 1-100 (default 50).
        after_id: The previous page's next_after_id, to continue.
        source: Only memories from this source (kind='memory' only).

    Returns:
        JSON: items (id, kind, recorded_at, preview, source, processing_state) and next_after_id
        (null on the last page). Ordered by id, not by time.
    """

    return await ListNamespaceMemoriesTool().execute(
        namespace=namespace, kind=kind, limit=limit, after_id=after_id, source=source
    )


class ListNamespaceMemoriesTool(BaseJsonTool):
    name = "list_namespace_memories"
    scope = ToolScope.NAMESPACED
    required_tier = "operator"
    title = "List Namespace Memories"
    oauth_scopes = ("menhir:admin",)
    read_only_hint = True
    destructive_hint = False
    open_world_hint = False
    description = (
        "Page a namespace's memories, TurnEvidence or typed assertions by id, for delete_memories."
    )

    async def endpoint(
        self,
        namespace: str = "default",
        kind: str = "memory",
        limit: int = 50,
        after_id: str = "",
        source: str = "",
    ) -> str:
        if kind not in KINDS:
            return self.render_json(
                {"error": f"unknown kind {kind!r}; expected one of {list(KINDS)}"}
            )
        result = await self.get_backend().list_namespace_records(
            namespace,
            kind=kind,
            limit=limit,
            after_id=after_id.strip() or None,
            source=source.strip() or None,
        )
        return self.render_json(result)
