"""MCP tool: relocate_artifact_source."""

from __future__ import annotations

from menhir.mcp.ownership import foreign_object_refusal
from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def relocate_artifact_source(
    artifact_uuid: str,
    old_path: str,
    new_path: str,
    namespace: str = "",
    repository: str = "",
    expected_old_integrity: str = "",
    observed_integrity: str = "",
) -> str:
    """Move one artifact source's locator after a file moved.

    This is the escape hatch, not the routine path. Ordinary moves are picked up
    by the file-event hook or by the next corpus audit; use this to resolve an
    audited ambiguity, or to repair a move where Git evidence is unavailable.

    Identity is preserved: the artifact UUID, the source record, and every
    relationship survive. Only the locator changes. The move is refused if the
    old path belongs to a different artifact, if the old path identifies more
    than one source, or if the destination is already claimed.

    Args:
        artifact_uuid: The artifact you believe currently lives at ``old_path``.
        old_path: Repository-relative path recorded on the source today.
        new_path: Repository-relative path the document now lives at.
        repository: Repository name recorded on the source. Required in
                    practice -- locators are repository-scoped, so omitting it
                    cannot match anything.
        expected_old_integrity: Optional sha256 the caller believes is current.
                                Supplying it makes the write refuse stale input.
        observed_integrity: Optional sha256 of the file at its new path.

    Returns:
        Confirmation of the move, or the reason it was refused.
    """
    return await RelocateArtifactSourceTool().execute(
        artifact_uuid=artifact_uuid,
        old_path=old_path,
        new_path=new_path,
        namespace=namespace,
        repository=repository,
        expected_old_integrity=expected_old_integrity,
        observed_integrity=observed_integrity,
    )


class RelocateArtifactSourceTool(BaseTextTool):
    name = "relocate_artifact_source"
    # NAMESPACED once the ownership guard exists (CF-33 step 4): an artifact uuid is
    # not proof of ownership, so each one the caller names is checked against the pin.
    scope = ToolScope.NAMESPACED
    description = (
        "Move one work-artifact source's locator, preserving identity and every relationship."
    )

    async def endpoint(
        self,
        artifact_uuid: str,
        old_path: str,
        new_path: str,
        namespace: str = "",
        repository: str = "",
        expected_old_integrity: str = "",
        observed_integrity: str = "",
    ) -> str:
        backend = self.get_backend()
        # CF-33 step 4: ownership-at-load. `repository` is a locator identity, not a tenancy
        # boundary -- relocating a source is a write against the artifact that owns it.
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
        result = await backend.relocate_artifact_source(
            artifact_uuid=artifact_uuid,
            old_path=old_path,
            new_path=new_path,
            repository=repository or None,
            expected_old_integrity=expected_old_integrity,
            observed_integrity=observed_integrity,
        )

        if result.get("applied"):
            return f"{artifact_uuid}: {old_path} -> {new_path}"

        reason = result.get("reason") or "unknown"
        explanations = {
            "repository_required": (
                "Pass the repository name recorded on the source; locators are "
                "repository-scoped and an empty one matches nothing."
            ),
            "locator_not_found": f"No source is recorded at {old_path}.",
            "locator_is_ambiguous": (
                f"More than one source claims {old_path}; resolve the duplicate first."
            ),
            "source_uuid_not_backfilled": (
                "The source predates addressable source UUIDs. Run "
                "`menhir artifacts reconcile --prepare` first."
            ),
            "uuid_locator_disagreement": (
                f"{old_path} belongs to {result.get('locator_owner')}, not {artifact_uuid}."
            ),
            "destination_already_claimed": f"Another source already claims {new_path}.",
            "stale_old_locator": "The source has moved since you read it.",
            "stale_expected_integrity": "The file changed since the integrity you supplied.",
        }
        return f"Refused ({reason}): {explanations.get(reason, 'no change was made.')}"
