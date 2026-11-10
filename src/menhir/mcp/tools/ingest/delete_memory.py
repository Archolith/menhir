"""MCP tool: delete_memory."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def delete_memory(node_uuid: str, namespace: str = "") -> str:
    """Delete a specific memory node and all its relationships.

    Args:
        node_uuid: The UUID of the memory node to delete. Get this from recall_memories results.
        namespace: Restrict the operation to a single silo. A pinned client has this forced.

    Returns:
        Confirmation or failure message.
    """

    return await DeleteMemoryTool().execute(node_uuid=node_uuid, namespace=namespace)


class DeleteMemoryTool(BaseTextTool):
    name = "delete_memory"
    scope = ToolScope.NAMESPACED
    required_tier = "operator"
    title = "Delete Memory"
    oauth_scopes = ("menhir:admin",)
    read_only_hint = False
    destructive_hint = True
    open_world_hint = False
    description = "Delete a specific memory node and all its relationships."

    async def endpoint(self, node_uuid: str, namespace: str = "") -> str:
        """Delete a specific memory node and all its relationships.

        Args:
            node_uuid: The UUID of the memory node to delete. Get this from recall_memories results.
            namespace: Restrict the operation to a single silo. A pinned client has this forced.

        Returns:
            Confirmation or failure message.
        """
        backend = self.get_backend()
        # Ownership guard. Without the `namespace` parameter above, the pin cannot reach
        # this tool at all: _apply_pinned_namespace injects only into endpoints whose
        # signature names it, so a pinned client could erase any uuid it had learned.
        #
        # Two lookups, not one, and the second is the point. Refusing whenever the node is
        # not found IN this namespace would also refuse when it is not in the graph AT ALL
        # -- and that is a supported erasure path, not an error: `graph_already_absent` is
        # how a merge leaves the node it absorbed, whose stored content must still be
        # erasable. So absent-from-graph proceeds, and only a node that demonstrably
        # belongs to another silo is refused.
        ns = namespace.strip() or None
        if ns is not None and await backend.fetch_memory_by_uuid(
            node_uuid, namespace=ns
        ) is None:
            if await backend.fetch_memory_by_uuid(node_uuid) is not None:
                return (
                    f"Refused: memory {node_uuid} exists but is outside namespace {ns}."
                )
        outcome = await backend.erase_memory(node_uuid)
        reason = outcome.get("reason")
        purged = sum((outcome.get("purged") or {}).values())

        if reason == "nothing_to_erase":
            return f"No memory found with uuid={node_uuid}, and no stored content for it."
        if reason == "graph_already_absent":
            # Not "not found": the node was gone from the graph -- which is how a merge leaves
            # the node it absorbed -- but its stored content was still there and is now erased.
            return (
                f"Memory {node_uuid} was already absent from the graph, but {purged} stored "
                "record(s) still held its content. Those are now erased."
            )
        if reason == "residual_content_after_purge":
            return (
                f"Erasure of {node_uuid} did NOT complete: content remains after the purge and "
                "the operation is quarantined for review."
            )
        if reason == "prepare_failed":
            # The erase never started -- a lock or a paused saga refused it. Nothing was
            # touched, so say that plainly instead of implying a partial erasure.
            return (
                f"Erasure of {node_uuid} did NOT run: the operation could not be prepared "
                "(another operation holds this memory, or saga writes are paused). Nothing "
                "was erased; retry later."
            )
        if reason == "erased_incomplete":
            # The purge ran but provably missed something. Enumerate what, so the caller can
            # tell a benign leftover from a real leak; only claim what we can prove.
            derived = outcome.get("derived") or {}
            message = f"Erased memory {node_uuid}, but the erasure is INCOMPLETE:"
            gaps = 0
            unaddressable_count = len(outcome.get("unaddressable") or [])
            if unaddressable_count:
                message += (
                    f" {unaddressable_count} content location(s) could not be addressed and "
                    "were left untouched."
                )
                gaps += 1
            unknown_provenance = int(derived.get("unknown_provenance") or 0)
            if unknown_provenance:
                message += (
                    f" {unknown_provenance} derived fact(s) or entit(ies) have no traceable "
                    "source and were left in place."
                )
                gaps += 1
            if derived.get("twin_missing"):
                message += (
                    " The memory's extracted graph data could not be located (no twin link), "
                    "so derived facts may remain."
                )
                gaps += 1
            if outcome.get("stranded_known") is False:
                message += " The stored content census failed, so completeness is unknown."
                gaps += 1
            if not gaps:
                message += " completeness could not be confirmed."
            return message
        if reason == "erased":
            message = (
                f"Erased memory {node_uuid}: the graph node, all its relationships, and {purged} "
                "stored record(s) of its content."
            )
            derived = outcome.get("derived") or {}
            entities_deleted = int(derived.get("entities_deleted") or 0)
            edges_deleted = int(derived.get("edges_deleted") or 0)
            entities_flagged = int(derived.get("entities_flagged") or 0)
            edges_flagged = int(derived.get("edges_flagged") or 0)
            merge_audits_tombstoned = int(derived.get("merge_audits_tombstoned") or 0)
            if (
                entities_deleted
                + edges_deleted
                + entities_flagged
                + edges_flagged
                + merge_audits_tombstoned
            ) > 0:
                message += (
                    f" Derived data: {entities_deleted} entit(ies) and {edges_deleted} fact(s) "
                    f"removed; {entities_flagged + edges_flagged} shared item(s) hidden pending "
                    f"re-derivation; {merge_audits_tombstoned} merge(s) made non-reversible."
                )
            unaddressable = outcome.get("unaddressable") or []
            if unaddressable:
                # Never let an erasure claim a completeness it does not have.
                message += (
                    f" NOTE: {len(unaddressable)} content location(s) could not be addressed and "
                    "were left untouched."
                )
            return message
        # Unknown reason. An outcome we do not recognise is exactly the case where a
        # cheerful default message would be a lie, so never claim success.
        return (
            f"Erasure of {node_uuid} returned an unexpected result ({reason!r}); it may not "
            "have completed. Check get_memory_stats or the operator logs."
        )
