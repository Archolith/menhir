"""MCP tool: get_artifact_relationships."""

from __future__ import annotations

from menhir.mcp.ownership import foreign_object_refusal
from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def get_artifact_relationships(artifact_uuid: str, namespace: str = "") -> str:
    """Show what an artifact reviews, implements, informs, supersedes or is about.

    Every edge shown was explicitly declared. Menhir never infers artifact
    relationships from prose, so an absent edge means nobody declared it -- not
    that no connection exists.

    Args:
        artifact_uuid: The artifact's stable uuid.

    Returns:
        Outgoing and incoming relationships, plus subjects and referenced todos.
    """
    return await ArtifactRelationshipsTool().execute(
        artifact_uuid=artifact_uuid, namespace=namespace
    )


class ArtifactRelationshipsTool(BaseTextTool):
    name = "get_artifact_relationships"
    # NAMESPACED once the ownership guard exists (CF-33 step 4): an artifact uuid is
    # not proof of ownership, so each one the caller names is checked against the pin.
    scope = ToolScope.NAMESPACED
    required_tier = "readonly"
    title = "Get Artifact Relationships"
    oauth_scopes = ("menhir:read",)
    read_only_hint = True
    destructive_hint = False
    open_world_hint = False
    description = "Show an artifact's declared relationships, subjects, and referenced todos."

    async def endpoint(self, artifact_uuid: str, namespace: str = "") -> str:
        backend = self.get_backend()
        # CF-33 step 4: ownership-at-load.
        refusal = await foreign_object_refusal(
            uuid=artifact_uuid,
            namespace=namespace,
            # Resolved lazily so an UNPINNED call touches nothing it did not touch
            # before: `backend.get_artifact` is not looked up unless a namespace is set.
            lookup=lambda uuid, **kw: backend.get_artifact(uuid, **kw),
            label="artifact",
        )
        if refusal:
            return refusal
        data = await backend.get_artifact_relationships(artifact_uuid)
        if not data:
            return f"Artifact {artifact_uuid} not found"

        lines: list[str] = []
        for row in data.get("outgoing") or []:
            lines.append(
                f"-> {row.get('relation', '?')}: {row.get('target_title') or row.get('target_uuid')}"
                f" [{row.get('target_type', '?')}]"
            )
        for row in data.get("incoming") or []:
            lines.append(
                f"<- {row.get('relation', '?')}: {row.get('source_title') or row.get('source_uuid')}"
                f" [{row.get('source_type', '?')}]"
            )
        for row in data.get("subjects") or []:
            lines.append(f"   ABOUT: {row.get('name') or row.get('entity_uuid')}")
        for row in data.get("todos") or []:
            lines.append(f"   REFERENCES_TODO: {row.get('todo_uuid')} ({row.get('status', '?')})")

        if not lines:
            return (
                f"Artifact {artifact_uuid} has no declared relationships.\n"
                "Relationships are never inferred, so this means none were declared."
            )
        return "\n".join(lines)
