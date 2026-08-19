"""MCP tool: supersede_artifact."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def supersede_artifact(new_uuid: str, old_uuid: str) -> str:
    """Record that one artifact replaces another, atomically.

    Writes the SUPERSEDES edge and moves the old artifact to SUPERSEDED in a
    single statement: an edge pointing at an artifact still marked APPROVED, or
    a SUPERSEDED artifact with no record of what replaced it, are both states
    the graph must never hold.

    Args:
        new_uuid: The replacing artifact.
        old_uuid: The artifact being replaced. Must be the same type and not
                  already terminal.

    Returns:
        Confirmation, or the reason it was refused.
    """
    return await SupersedeArtifactTool().execute(new_uuid=new_uuid, old_uuid=old_uuid)


class SupersedeArtifactTool(BaseTextTool):
    name = "supersede_artifact"
    scope = ToolScope.OBJECT
    description = "Record that one artifact supersedes another, moving status and edge together."

    async def endpoint(self, new_uuid: str, old_uuid: str) -> str:
        backend = self.get_backend()
        result = await backend.supersede_artifact(new_uuid, old_uuid)

        if result.get("applied"):
            return f"{old_uuid} is now SUPERSEDED by {new_uuid}"
        return (
            f"Refused: {old_uuid} was not superseded. Supersession is same-type only, the "
            "artifacts must share a namespace, and an already-superseded or deferred artifact "
            "is not re-superseded so the recorded replacement stays the one that applied."
        )
