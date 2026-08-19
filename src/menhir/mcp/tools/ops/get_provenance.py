"""MCP tool: get_provenance — show a node's receipts (its source episodes + evidence)."""

from __future__ import annotations

from menhir.mcp.ownership import foreign_object_refusal
from menhir.mcp.tools.base import BaseJsonTool
from menhir.mcp.contracts import ToolScope

#: default cap on returned episode content (chars); keeps the receipt compact.
_DEFAULT_CONTENT_CHARS = 500
_MAX_CONTENT_CHARS = 5000


async def get_provenance(
    node_uuid: str, content_chars: int = _DEFAULT_CONTENT_CHARS, namespace: str = ""
) -> str:
    """Show the receipts for a memory or View node: the source episodes it was built from,
    plus its first-class evidence anchors, so you can verify a summary/claim against its sources.

    Pass a `node_uuid` from a recall result (e.g. a View/counter node) to expand it into the
    episodes that `MENTIONS` it. Read-only.

    Args:
        node_uuid: UUID of the node to expand.
        content_chars: max characters of each episode's content to return (0-5000).

    Returns:
        JSON with the node's name/view_kind, its source episodes (uuid/source/content/created_at),
        SUPPORTED_BY evidence, and ANCHORED_TO structural paths.
    """

    return await GetProvenanceTool().execute(
        node_uuid=node_uuid, content_chars=content_chars, namespace=namespace
    )


class GetProvenanceTool(BaseJsonTool):
    name = "get_provenance"
    # NAMESPACED once the ownership guard exists (CF-33 step 4): a uuid is not proof of
    # ownership, so the object the caller names is checked against the pin at load.
    scope = ToolScope.NAMESPACED
    required_tier = "readonly"
    description = (
        "Show a memory/View node's receipts: the source episodes it was built from, plus evidence "
        "anchors, so you can verify a summary or claim against its sources."
    )

    async def endpoint(
        self, node_uuid: str, content_chars: int = _DEFAULT_CONTENT_CHARS,
        namespace: str = "",
    ) -> str:
        backend = self.get_backend()
        # CF-33 step 4: ownership-at-load. This returns a node's source episodes and its
        # evidence anchors -- provenance IS content, so an unowned uuid must not resolve.
        refusal = await foreign_object_refusal(
            uuid=node_uuid,
            namespace=namespace,
            # Resolved lazily so an UNPINNED call touches nothing it did not touch before.
            lookup=lambda uuid, **kw: backend.fetch_memory_by_uuid(uuid, **kw),
            label="node",
        )
        if refusal:
            return self.render_json(
                {"ok": False, "tool": self.operation, "node_uuid": node_uuid,
                 "error": {"message": refusal}}
            )
        row = await backend.fetch_node_receipts(node_uuid)
        if row is None:
            return self.render_json(
                {
                    "ok": False,
                    "tool": self.operation,
                    "node_uuid": node_uuid,
                    "error": {"message": f"No node found with uuid={node_uuid}."},
                }
            )

        try:
            cap = max(0, min(int(content_chars), _MAX_CONTENT_CHARS))
        except (TypeError, ValueError):
            cap = _DEFAULT_CONTENT_CHARS

        def _clip(text: object) -> object:
            if isinstance(text, str) and len(text) > cap:
                return text[:cap] + "…"
            return text

        episodes = [
            {
                "uuid": ep.get("uuid"),
                "source": ep.get("source"),
                "created_at": ep.get("created_at"),
                "content": _clip(ep.get("content")),
            }
            for ep in (row.get("episodes") or [])
        ]

        return self.render_json(
            {
                "ok": True,
                "tool": self.operation,
                "node_uuid": row.get("uuid") or node_uuid,
                "name": row.get("name"),
                "view_kind": row.get("view_kind"),
                "episode_count": len(episodes),
                "episodes": episodes,
                "evidence": row.get("evidence") or [],
                "anchor_paths": row.get("anchor_paths") or [],
            }
        )
