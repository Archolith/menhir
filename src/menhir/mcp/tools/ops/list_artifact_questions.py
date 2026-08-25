"""MCP tool: list_artifact_questions."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def list_artifact_questions(
    artifact_uuid: str = "", status: str = "open", namespace: str = "", limit: int = 25
) -> str:
    """List open design questions recorded on work artifacts.

    Answers "what design questions remain?" and "which plans are blocked?"
    without reading markdown.

    Args:
        artifact_uuid: Restrict to one artifact. Empty spans every artifact.
        status: open (default) | answered | deferred.
        namespace: Optional silo, read off the owning artifact.
        limit: Max rows (default 25, max 200).

    Returns:
        Questions in the order their author wrote them, with the owning artifact.
    """
    return await ArtifactQuestionsTool().execute(
        artifact_uuid=artifact_uuid, status=status, namespace=namespace, limit=limit
    )


class ArtifactQuestionsTool(BaseTextTool):
    name = "list_artifact_questions"
    scope = ToolScope.NAMESPACED
    required_tier = "readonly"
    title = "List Artifact Questions"
    oauth_scopes = ("menhir:read",)
    read_only_hint = True
    destructive_hint = False
    open_world_hint = False
    description = "List open, answered, or deferred design questions on work artifacts."

    async def endpoint(
        self, artifact_uuid: str = "", status: str = "open", namespace: str = "", limit: int = 25
    ) -> str:
        backend = self.get_backend()
        rows = await backend.list_artifact_questions(
            artifact_uuid=artifact_uuid or None,
            status=status or None,
            namespace=namespace or None,
            limit=min(limit, 200),
        )

        lines = [f"{status or 'all'} questions ({len(rows)})"]
        if not rows:
            lines.append("(none)")
            return "\n".join(lines)

        for row in rows:
            ordinal = row.get("ordinal")
            marker = f"#{ordinal + 1}" if isinstance(ordinal, int) else "-"
            lines.append(f"{marker} {row.get('text') or '(empty)'}")
            owner = row.get("artifact_title") or row.get("artifact_uuid") or "?"
            detail = f"      on: {owner}"
            if row.get("question_uuid"):
                detail += f"  question_uuid={row['question_uuid']}"
            lines.append(detail)
        return "\n".join(lines)
