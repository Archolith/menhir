"""MCP tool: supersede_artifact."""

from __future__ import annotations

from menhir.mcp.ownership import foreign_object_refusal
from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def supersede_artifact(new_uuid: str, old_uuid: str, namespace: str = "") -> str:
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
    return await SupersedeArtifactTool().execute(
        new_uuid=new_uuid, old_uuid=old_uuid, namespace=namespace
    )


class SupersedeArtifactTool(BaseTextTool):
    name = "supersede_artifact"
    # NAMESPACED once the ownership guard exists (CF-33 step 4): an artifact uuid is
    # not proof of ownership, so each one the caller names is checked against the pin.
    scope = ToolScope.NAMESPACED
    description = "Record that one artifact supersedes another, moving status and edge together."
    title = "Supersede Artifact"
    oauth_scopes = ("menhir:write",)
    read_only_hint = False
    destructive_hint = False
    open_world_hint = False

    async def endpoint(
        self, new_uuid: str, old_uuid: str, namespace: str = ""
    ) -> str:
        backend = self.get_backend()
        # CF-33 step 4: ownership-at-load, on BOTH uuids. The existing check that the two
        # artifacts share a namespace is RELATIVE -- it stops a cross-silo link but is
        # equally satisfied by two artifacts that both live in someone else's silo. This is
        # the absolute check: each named artifact must be the caller's own.
        refusal = await foreign_object_refusal(
            uuid=new_uuid,
            namespace=namespace,
            # Resolved lazily so an UNPINNED call touches nothing it did not touch
            # before: `backend.get_artifact` is not looked up unless a namespace is set.
            lookup=lambda uuid, **kw: backend.get_artifact(uuid, **kw),
            label="artifact",
        )
        if refusal:
            return refusal
        refusal = await foreign_object_refusal(
            uuid=old_uuid,
            namespace=namespace,
            # Resolved lazily so an UNPINNED call touches nothing it did not touch
            # before: `backend.get_artifact` is not looked up unless a namespace is set.
            lookup=lambda uuid, **kw: backend.get_artifact(uuid, **kw),
            label="artifact",
        )
        if refusal:
            return refusal
        result = await backend.supersede_artifact(new_uuid, old_uuid)

        if result.get("applied"):
            return f"{old_uuid} is now SUPERSEDED by {new_uuid}"
        return (
            f"Refused: {old_uuid} was not superseded. Supersession is same-type only, the "
            "artifacts must share a namespace, and an already-superseded or deferred artifact "
            "is not re-superseded so the recorded replacement stays the one that applied."
        )
