"""MCP tool: link_artifacts."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def link_artifacts(source_uuid: str, target_uuid: str, relation: str) -> str:
    """Declare a relationship between two work artifacts.

    Args:
        source_uuid: The artifact making the claim.
        target_uuid: The artifact being claimed about.
        relation: reviews | implements | informs. Legality is checked against
                  both artifacts' stored types, so a review can review a plan
                  but a plan cannot review anything.

    Returns:
        Confirmation, or the reason the declaration was refused.
    """
    return await LinkArtifactsTool().execute(
        source_uuid=source_uuid, target_uuid=target_uuid, relation=relation
    )


class LinkArtifactsTool(BaseTextTool):
    name = "link_artifacts"
    scope = ToolScope.OBJECT
    description = "Declare that one artifact reviews, implements, or informs another."

    _REASONS = {
        "unsupported_relation": "not a supported relation (use reviews, implements, or informs)",
        "illegal_source_type": "that artifact type cannot be the source of this relation",
        "illegal_target_type": "that artifact type cannot be the target of this relation",
        "artifact_not_found": "one or both artifacts do not exist",
        "namespace_incompatible": "the artifacts are in incompatible namespaces",
    }

    async def endpoint(self, source_uuid: str, target_uuid: str, relation: str) -> str:
        backend = self.get_backend()
        result = await backend.link_artifacts(source_uuid, target_uuid, relation)

        if result.get("linked"):
            return f"Declared {source_uuid} -[{result.get('edge_type', relation)}]-> {target_uuid}"

        reason = result.get("reason") or "unknown"
        # Supersession is refused here on purpose: it is a lifecycle change, not
        # an edge, and must go through supersede_artifact so the old artifact's
        # status moves with it.
        if relation == "supersedes":
            return (
                "Refused: supersedes is not a declarable relation. Use supersede_artifact, "
                "which writes the edge and moves the superseded artifact's status together."
            )
        return f"Refused: {self._REASONS.get(reason, reason)}"
