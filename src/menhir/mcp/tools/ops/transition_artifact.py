"""MCP tool: transition_artifact."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def transition_artifact(
    artifact_uuid: str, to_status: str, namespace: str = ""
) -> str:
    """Move a work artifact to a new lifecycle status.

    Transitions are checked against the artifact's stored type and current
    status, so a step cannot be skipped: a PROPOSED plan cannot jump to
    IMPLEMENTED without passing through review and approval.

    Args:
        artifact_uuid: The artifact to move.
        to_status: Target status, e.g. REVIEWED, APPROVED, IMPLEMENTING,
                   IMPLEMENTED, COMPLETE, READY_FOR_REVIEW, SUPERSEDED, DEFERRED.
        namespace: Restrict to a single silo. A pinned client has this forced.

    Returns:
        Confirmation with the previous status, or the reason it was refused.
    """
    return await TransitionArtifactTool().execute(
        artifact_uuid=artifact_uuid, to_status=to_status, namespace=namespace
    )


class TransitionArtifactTool(BaseTextTool):
    name = "transition_artifact"
    scope = ToolScope.NAMESPACED
    description = "Move a work artifact to a new lifecycle status, if the transition is legal."
    title = "Transition Artifact Status"
    oauth_scopes = ("menhir:write",)
    read_only_hint = False
    destructive_hint = False
    open_world_hint = False

    async def endpoint(
        self, artifact_uuid: str, to_status: str, namespace: str = ""
    ) -> str:
        """Move a work artifact to a new lifecycle status.

        Args:
            artifact_uuid: The artifact to move.
            to_status: Target lifecycle status.
            namespace: Restrict to a single silo. A pinned client has this forced --
                and the parameter existing is what makes that possible, since the pin
                is injected only into endpoints whose signature declares it.
        """
        backend = self.get_backend()
        result = await backend.transition_artifact_status(
            artifact_uuid, to_status, namespace=namespace.strip() or None
        )

        if result.get("applied"):
            return (
                f"{artifact_uuid}: {result.get('from_status', '?')} -> "
                f"{result.get('to_status', to_status)}"
            )

        reason = result.get("reason") or "unknown"
        if reason == "artifact_not_found":
            return f"Artifact {artifact_uuid} not found"
        valid = result.get("valid_transitions")
        suffix = f" Legal from here: {', '.join(sorted(valid))}" if valid else ""
        return (
            f"Refused: {result.get('from_status', '?')} -> {to_status} is not legal for a "
            f"{result.get('artifact_type', '?')}.{suffix}"
        )
