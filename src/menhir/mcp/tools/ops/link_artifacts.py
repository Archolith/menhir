"""MCP tool: link_artifacts."""

from __future__ import annotations

from menhir.mcp.ownership import foreign_object_refusal
from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def link_artifacts(
    source_uuid: str, target_uuid: str, relation: str, namespace: str = ""
) -> str:
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
        source_uuid=source_uuid, target_uuid=target_uuid, relation=relation,
        namespace=namespace,
    )


class LinkArtifactsTool(BaseTextTool):
    name = "link_artifacts"
    # NAMESPACED once the ownership guard exists (CF-33 step 4): an artifact uuid is
    # not proof of ownership, so each one the caller names is checked against the pin.
    scope = ToolScope.NAMESPACED
    title = "Link Artifacts"
    oauth_scopes = ("menhir:write",)
    read_only_hint = False
    destructive_hint = False
    open_world_hint = False
    description = "Declare that one artifact reviews, implements, or informs another."

    _REASONS = {
        "unsupported_relation": "not a supported relation (use reviews, implements, or informs)",
        "illegal_source_type": "that artifact type cannot be the source of this relation",
        "illegal_target_type": "that artifact type cannot be the target of this relation",
        "artifact_not_found": "one or both artifacts do not exist",
        "namespace_incompatible": "the artifacts are in incompatible namespaces",
    }

    async def endpoint(
        self, source_uuid: str, target_uuid: str, relation: str, namespace: str = ""
    ) -> str:
        backend = self.get_backend()
        # CF-33 step 4: ownership-at-load, on BOTH uuids. The existing check that the two
        # artifacts share a namespace is RELATIVE -- it stops a cross-silo link but is
        # equally satisfied by two artifacts that both live in someone else's silo. This is
        # the absolute check: each named artifact must be the caller's own.
        refusal = await foreign_object_refusal(
            uuid=source_uuid,
            namespace=namespace,
            # Resolved lazily so an UNPINNED call touches nothing it did not touch
            # before: `backend.get_artifact` is not looked up unless a namespace is set.
            lookup=lambda uuid, **kw: backend.get_artifact(uuid, **kw),
            label="artifact",
        )
        if refusal:
            return refusal
        refusal = await foreign_object_refusal(
            uuid=target_uuid,
            namespace=namespace,
            # Resolved lazily so an UNPINNED call touches nothing it did not touch
            # before: `backend.get_artifact` is not looked up unless a namespace is set.
            lookup=lambda uuid, **kw: backend.get_artifact(uuid, **kw),
            label="artifact",
        )
        if refusal:
            return refusal
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
