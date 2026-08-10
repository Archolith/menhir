"""MCP tool: transition_artifact."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool


async def transition_artifact(artifact_uuid: str, to_status: str) -> str:
    """Move a work artifact to a new lifecycle status.

    Transitions are checked against the artifact's stored type and current
    status, so a step cannot be skipped: a PROPOSED plan cannot jump to
    IMPLEMENTED without passing through review and approval.

    Args:
        artifact_uuid: The artifact to move.
        to_status: Target status, e.g. REVIEWED, APPROVED, IMPLEMENTING,
                   IMPLEMENTED, COMPLETE, READY_FOR_REVIEW, SUPERSEDED, DEFERRED.

    Returns:
        Confirmation with the previous status, or the reason it was refused.
    """
    return await TransitionArtifactTool().execute(
        artifact_uuid=artifact_uuid, to_status=to_status
    )


class TransitionArtifactTool(BaseTextTool):
    name = "transition_artifact"
    description = "Move a work artifact to a new lifecycle status, if the transition is legal."

    async def endpoint(self, artifact_uuid: str, to_status: str) -> str:
        backend = self.get_backend()
        result = await backend.transition_artifact_status(artifact_uuid, to_status)

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
