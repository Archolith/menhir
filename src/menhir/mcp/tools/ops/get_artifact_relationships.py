"""MCP tool: get_artifact_relationships."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool


async def get_artifact_relationships(artifact_uuid: str) -> str:
    """Show what an artifact reviews, implements, informs, supersedes or is about.

    Every edge shown was explicitly declared. Menhir never infers artifact
    relationships from prose, so an absent edge means nobody declared it -- not
    that no connection exists.

    Args:
        artifact_uuid: The artifact's stable uuid.

    Returns:
        Outgoing and incoming relationships, plus subjects and referenced todos.
    """
    return await ArtifactRelationshipsTool().execute(artifact_uuid=artifact_uuid)


class ArtifactRelationshipsTool(BaseTextTool):
    name = "get_artifact_relationships"
    required_tier = "readonly"
    description = "Show an artifact's declared relationships, subjects, and referenced todos."

    async def endpoint(self, artifact_uuid: str) -> str:
        backend = self.get_backend()
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
