"""MCP tool: scan_for_conflicts."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseJsonTool


async def scan_for_conflicts(limit: int = 150, cursor: str = "") -> str:
    """Scan persistent entity nodes for similarity-based conflicts.

    Fetches up to `limit` PERSISTENT nodes and checks each for high-similarity
    neighbours.  New pairs are written as 'pending_llm_review' — run
    run_llm_conflict_review afterwards to confirm.

    Supports cursor-based pagination: pass `next_cursor` from the previous
    result to continue scanning where the last batch left off.

    Args:
        limit: Max nodes to scan per batch (default 150).
        cursor: Resume token from previous scan. Empty string starts from the beginning.

    Returns:
        Counts of nodes scanned and new conflicts, plus next_cursor and done flag.
    """

    return await ScanConflictsTool().execute(limit=limit, cursor=cursor)


class ScanConflictsTool(BaseJsonTool):
    name = "scan_for_conflicts"
    required_tier = "operator"
    description = "Scan persistent entity nodes for similarity-based conflicts."

    async def endpoint(self, limit: int = 150, cursor: str = "") -> str:
        backend = self.get_backend()
        counts = await backend.scan_for_conflicts(limit=limit, cursor=cursor or None)
        return self.render_json(counts)
