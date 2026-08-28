"""MCP tool: scan_for_conflicts."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseJsonTool
from menhir.mcp.contracts import ToolScope


async def scan_for_conflicts(
    limit: int = 150, cursor: str = "", namespace: str = ""
) -> str:
    """Scan persistent entity nodes for similarity-based conflicts.

    Fetches up to `limit` PERSISTENT nodes and checks each for high-similarity
    neighbours.  New pairs are written as 'pending_llm_review' — run
    run_llm_conflict_review afterwards to confirm.

    Supports cursor-based pagination: pass `next_cursor` from the previous
    result to continue scanning where the last batch left off.

    Args:
        limit: Max nodes to scan per batch (default 150).
        cursor: Resume token from previous scan. Empty string starts from the beginning.
        namespace: Optional silo to scope this operation to. Empty = every silo
            (existing behavior). A pinned client has this forced to its own silo.

    Returns:
        Counts of nodes scanned and new conflicts, plus next_cursor and done flag.
    """

    return await ScanConflictsTool().execute(
        limit=limit, cursor=cursor, namespace=namespace
    )


class ScanConflictsTool(BaseJsonTool):
    name = "scan_for_conflicts"
    # NAMESPACED: the scan WRITES conflict_group_id onto tenant nodes. Pairing was already
    # per-node namespace-scoped, so this bounds which nodes are scanned at all.
    scope = ToolScope.NAMESPACED
    required_tier = "operator"
    title = "Scan For Conflicts"
    oauth_scopes = ("menhir:admin",)
    # The scan WRITES conflict_group_id onto tenant nodes, so it is not read-only.
    read_only_hint = False
    destructive_hint = False
    open_world_hint = False
    description = "Scan persistent entity nodes for similarity-based conflicts."

    async def endpoint(
        self, limit: int = 150, cursor: str = "", namespace: str = ""
    ) -> str:
        backend = self.get_backend()
        # `namespace` is forwarded only when set. An unpinned caller therefore produces a
        # byte-identical backend call to the one made before this parameter existed --
        # not merely an equivalent query. That is the stronger property, and it is what
        # keeps every pre-existing backend stub and protocol implementation valid.
        scope = {"namespace": namespace} if namespace else {}
        counts = await backend.scan_for_conflicts(
            limit=limit, cursor=cursor or None, **scope
        )
        return self.render_json(counts)
