"""MCP tool: recall_memories."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace

from menhir.mcp.formatters import _compact_scored_item
from menhir.mcp.service_access import get_request_session
from menhir.mcp.tools.base import BaseJsonTool
from menhir.mcp.contracts import ToolScope


def _resolve_compact(explicit: bool | None) -> bool:
    """Resolve compact mode: explicit arg wins, else env default, else False."""
    if explicit is not None:
        return explicit
    return os.environ.get("MENHIR_RECALL_COMPACT", "").lower() in ("1", "true", "yes")


_COMPACT_SOURCE_MEMORY_LIMIT = 5
_COMPACT_SOURCE_MEMORY_MAX_CHARS = 1000


def _deployment_source_memory_limit() -> int | None:
    """The serving runtime's default source-memory count, or None when this process has none.

    The stdio bridge builds no runtime, and its own environment says nothing about the server's
    setting, so it must not turn a guess into an explicit limit the server would obey.
    """
    try:
        from menhir.core.runtime import _state

        settings = getattr(_state.built, "settings", None)
        value = getattr(settings, "frontier_source_memory_limit", None)
    except Exception:
        return None
    return value if isinstance(value, int) else None


def _bound_source_memories(entries: list) -> tuple[list, bool]:
    """Cut over-long compact-mode source memories; never mutates the backend's entries."""
    kept = list(entries)
    cut = False
    for i, entry in enumerate(kept):
        content = entry.get("content") if isinstance(entry, dict) else None
        if isinstance(content, str) and len(content) > _COMPACT_SOURCE_MEMORY_MAX_CHARS:
            kept[i] = {
                **entry,
                "content": content[: _COMPACT_SOURCE_MEMORY_MAX_CHARS - 1] + "…",
                "truncated": True,
            }
            cut = True
    return kept, cut


async def recall_memories(
    query: str,
    preset: str = "knowledge",
    limit: int = 5,
    file_context: str = "",
    file_context_project: str = "",
    namespace: str = "",
    include_invalidated: bool = False,
    compact: bool | None = None,
    trace: bool = False,
    source_memory_limit: int | None = None,
    include_warden_status: bool = False,
) -> str:
    """Search memories by semantic similarity. Returns ranked results with relevance scores.

    Args:
        query: What to search for. Natural language works best.
        preset: Ranking strategy — "knowledge" (default), "recent", "connected". Also accepts "emotional" and "conflict" (partial — weight balance only, no domain signals yet).
        limit: Max results to return (default: 5).
        file_context: Optional file path — boosts memories linked to this file and its structural neighbors. Accepts absolute or relative paths.
        file_context_project: Optional project name for file_context disambiguation. Auto-detected if omitted.
        namespace: Optional silo to scope this operation to. Empty = default/global behavior.
        include_invalidated: When True, also return superseded/historical beliefs (expired facts). Default False = current beliefs only.
        compact: True to drop per-item explainability (breakdown sub-scores, type) and candidates_evaluated, keeping only the decision-relevant fields (name, scope, score, relevance, summary, uuid), and ask for at most 5 source memories (never more than the deployment default) of 1000 chars each; an explicit source_memory_limit keeps its count. None defers to the MENHIR_RECALL_COMPACT env default (off).
        include_warden_status: Include detailed Warden execution diagnostics (default false). Brief incomplete-check notices remain visible.
        source_memory_limit: Per-call size of the additive source-memory section (raw episode memories with their time, kept NEXT TO the ranked results, never fused into them). None = deployment default (on); 0 = off for this call; 1..50 = that many entries for this call. The section carries SESSION-scoped raw episodes, so it is off whenever include_session is false (promoted knowledge only); this tool always recalls with include_session=true.

    Returns:
        Ranked memory results with scores and explainability breakdown.
    """

    return await RecallMemoriesTool().execute(
        query=query, preset=preset, limit=limit,
        file_context=file_context, file_context_project=file_context_project,
        namespace=namespace,
        include_invalidated=include_invalidated,
        _compact=compact,
        include_warden_status=include_warden_status,
        trace=trace,
        source_memory_limit=source_memory_limit,
    )


class RecallMemoriesTool(BaseJsonTool):
    name = "recall_memories"
    scope = ToolScope.NAMESPACED
    required_tier = "readonly"
    title = "Recall Memories"
    oauth_scopes = ("menhir:read",)
    read_only_hint = True
    destructive_hint = False
    open_world_hint = False
    description = (
        "Find recorded decisions, reasons, incidents, preferences and constraints using "
        "vector/full-text retrieval and contextual ranking."
    )

    async def endpoint(
        self,
        query: str,
        preset: str = "knowledge",
        limit: int = 5,
        file_context: str = "",
        file_context_project: str = "",
        namespace: str = "",
        include_invalidated: bool = False,
        compact: bool | None = None,
        trace: bool = False,
        source_memory_limit: int | None = None,
        include_warden_status: bool = False,
    ) -> str:
        """Name the component and decision in query. Start with one focused query;
        rephrase if needed. limit defaults to 5. preset defaults to knowledge;
        recent and connected change ranking. emotional and conflict are also
        accepted ranking presets, not dedicated search modes.

        file_context adds file-linked context; supply file_context_project to
        disambiguate the structural project. namespace scopes the operation,
        subject to the client's configured pin.

        Returns ranked memory summaries, UUIDs, available temporal facts and
        warnings, plus a separate source_memories section of raw source episodes
        by default. Use get_provenance(node_uuid=...) on an entity/View result
        to inspect linked source excerpts.

        include_invalidated=true retains superseded fact edges attached to
        returned results; it does not search all historical episodes.
        compact=true omits per-item type/breakdown and candidates_evaluated, and
        asks for at most 5 source memories (never more than the server default)
        and cuts each to 1000 chars, ending cut entries with "…" and
        truncated=true; an explicit source_memory_limit keeps its count.
        Omitting compact uses the server default. trace=true requests retrieval
        diagnostics when available; it does not retrieve source text.
        include_warden_status=true shows configured versus applied guards and coverage gaps.

        Scores indicate retrieval relevance, not truth. Check dates, conflicts,
        stale-anchor warnings and response notes. Empty results do not establish
        that no relevant memory exists.
        """
        backend = self.get_backend()
        compact = _resolve_compact(compact)
        count_reduced = False
        backend_limit = source_memory_limit
        trim_locally = False
        if compact and source_memory_limit is None:
            deployment_limit = _deployment_source_memory_limit()
            if deployment_limit is None:
                # Server default unknown here: let the server apply its own, then trim to 5.
                trim_locally = True
            else:
                backend_limit = min(_COMPACT_SOURCE_MEMORY_LIMIT, deployment_limit)
                count_reduced = backend_limit < deployment_limit
        try:
            result = await backend.recall(
                query,
                preset=preset,
                limit=limit,
                include_session=True,
                wait_for_pending=True,
                file_context=file_context or None,
                file_context_project=file_context_project or None,
                namespace=namespace or None,
                include_invalidated=include_invalidated,
                **({"include_warden_status": True} if include_warden_status else {}),
                trace=trace,
                source_memory_limit=backend_limit,
            )
        except ValueError:
            return self.render_json(
                {
                    "ok": False,
                    "tool": self.operation,
                    "error": {
                        "message": (
                            f"Invalid preset '{preset}'. Use: knowledge, recent, emotional, connected, conflict."
                        )
                    },
                }
            )
        now = datetime.now(timezone.utc)
        retrieved_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        elapsed_since_last_access: str | None = None

        session = get_request_session()
        if session is not None and session.session_id:
            try:
                from menhir.mcp.telemetry import telemetry_store
                last_accessed = telemetry_store.get_session_last_accessed(session.session_id)
                if last_accessed:
                    last_dt = datetime.fromisoformat(last_accessed)
                    delta = now - last_dt
                    hours = delta.total_seconds() / 3600
                    if hours < 1:
                        elapsed_since_last_access = f"{int(delta.total_seconds() / 60)}m"
                    elif hours < 48:
                        elapsed_since_last_access = f"{hours:.1f}h"
                    else:
                        elapsed_since_last_access = f"{delta.days}d"
            except Exception:
                pass

        payload: dict = {
            "retrieved_at": retrieved_at,
            "query": query,
            "preset": result.get("preset"),
            "count": len(result.get("results", [])),
            "items": [
                _compact_scored_item(SimpleNamespace(**scored), compact=compact)
                for scored in result.get("results", [])
            ],
        }
        if not compact:
            payload["candidates_evaluated"] = result.get("candidates_evaluated", 0)
        if result.get("note"):
            payload["note"] = result.get("note")
        if result.get("query_hint"):
            payload["query_hint"] = result.get("query_hint")
        if result.get("authority_layer"):
            # Decision-relevant and already bounded by recall (7.J), so compact mode retains it.
            payload["authority_layer"] = result.get("authority_layer")
        if result.get("source_memories"):
            # Decision-relevant (raw source text + its time), so compact mode retains it,
            # but asks the backend for fewer entries (unless the caller set a limit) and cuts
            # each entry's length.
            source_memories = result.get("source_memories")
            if compact:
                if trim_locally and len(source_memories) > _COMPACT_SOURCE_MEMORY_LIMIT:
                    # The server sends them oldest-first, not by match: keep the closest five,
                    # in the server's order.
                    entries = list(source_memories)
                    closest = sorted(
                        range(len(entries)),
                        key=lambda i: -float((entries[i] or {}).get("cosine") or 0.0),
                    )[:_COMPACT_SOURCE_MEMORY_LIMIT]
                    source_memories = [entries[i] for i in sorted(closest)]
                    backend_limit = _COMPACT_SOURCE_MEMORY_LIMIT
                    count_reduced = True
                source_memories, cut = _bound_source_memories(source_memories)
                if count_reduced or cut:
                    applied = []
                    if count_reduced:
                        applied.append(f"at most {backend_limit} source memories")
                    if cut:
                        applied.append(
                            f"entries cut to {_COMPACT_SOURCE_MEMORY_MAX_CHARS} chars"
                        )
                    bounds = " and ".join(applied) if not (count_reduced and cut) else (
                        f"at most {backend_limit} source memories, each cut to "
                        f"{_COMPACT_SOURCE_MEMORY_MAX_CHARS} chars"
                    )
                    more = (
                        "more entries and longer text"
                        if count_reduced and cut
                        else "more entries" if count_reduced else "longer text"
                    )
                    payload["source_memories_note"] = (
                        f"Compact mode returned {bounds}. Call recall_memories with "
                        f"compact=false for {more} (up to the server's per-entry limit)."
                    )
            payload["source_memories"] = source_memories
        if include_warden_status and result.get("warden_status") is not None:
            payload["warden_status"] = result["warden_status"]
        if result.get("warden_notice"):
            payload["warden_notice"] = result["warden_notice"]
        if trace and result.get("trace") is not None:
            payload["trace"] = result.get("trace")
        if elapsed_since_last_access is not None:
            payload["elapsed_since_last_access"] = elapsed_since_last_access
        return self.render_recall_json(payload)
