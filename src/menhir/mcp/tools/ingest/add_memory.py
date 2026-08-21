"""MCP tool: add_memory."""

from __future__ import annotations

from menhir.mcp.formatters import _queue_summary
from menhir.mcp.service_access import get_mcp_session
from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def add_memory(
    text: str,
    source: str = "claude-code",
    diff: str | None = None,
    type: str = "SEMANTIC",
    valid_at: str | None = None,
    namespace: str = "",
    flagged: bool = False,
    bootstrap_scope: str = "",
    turn_evidence_uuid: str | None = None,
) -> str:
    """Queue a memory for enrichment. Use this to remember facts, preferences, decisions, or anything worth keeping.

    Args:
        text: The memory content to store. Be specific and self-contained.
        source: Where this memory came from (default: claude-code).
        diff: Optional git diff to attach as context for what changed alongside this memory.
        type: Memory type. Use "TEMPORAL" with valid_at to create a time-bound reminder.
        valid_at: ISO date for TEMPORAL memories (YYYY-MM-DD), e.g. "2027-02-16".
                  Required when type="TEMPORAL". Ignored for other types.
                  TEMPORAL + valid_at bypasses the enrichment queue and writes directly:
                  the memory persists immediately but receives no graph enrichment.
        namespace: Optional silo to scope this operation to. Empty = default/global behavior.
        flagged: If True, mark this memory as permanently retained (exempt from decay).
        bootstrap_scope: Optional startup pin: general or workspace:<registered-key>.
        turn_evidence_uuid: Optional UUID of the :TurnEvidence node for the turn this memory was written in the context of. For source='user'/'manual' it also grounds the user-tier claim (an ungrounded claim is downgraded). For every other source it draws the provenance edge only, which is what lets a typed-scalar assertion reach the entities extracted from this memory -- pass it whenever you know it.
                           Required for the user tier; omit to use agent_inference tier.

    Returns:
        Confirmation with the episode ID and counts of nodes/edges created.
    """

    return await AddMemoryTool().execute(text=text, source=source, diff=diff, type=type, valid_at=valid_at, namespace=namespace, flagged=flagged, bootstrap_scope=bootstrap_scope, turn_evidence_uuid=turn_evidence_uuid)


class AddMemoryTool(BaseTextTool):
    name = "add_memory"
    scope = ToolScope.NAMESPACED
    description = "Queue a memory for enrichment (TEMPORAL + valid_at writes directly instead)."

    async def endpoint(
        self,
        text: str,
        source: str = "claude-code",
        diff: str | None = None,
        type: str = "SEMANTIC",
        valid_at: str | None = None,
        namespace: str = "",
        flagged: bool = False,
        bootstrap_scope: str = "",
        turn_evidence_uuid: str | None = None,
    ) -> str:
        """Queue a memory for enrichment. Use this to remember facts, preferences, decisions, or anything worth keeping.

        Args:
            text: The memory content to store. Be specific and self-contained.
            source: Where this memory came from (default: claude-code).
            diff: Optional git diff to attach as context for what changed alongside this memory.
            type: Memory type. Use "TEMPORAL" with valid_at to create a time-bound reminder.
            valid_at: ISO date for TEMPORAL memories (YYYY-MM-DD), e.g. "2027-02-16".
                      Required when type="TEMPORAL". Ignored for other types.
                      TEMPORAL + valid_at bypasses the enrichment queue and writes directly:
                      the memory persists immediately but receives no graph enrichment.
            namespace: Optional silo to scope this operation to. Empty = default/global behavior.
            flagged: If True, mark this memory as permanently retained (exempt from decay).
            bootstrap_scope: Optional startup pin: general or workspace:<registered-key>.
            turn_evidence_uuid: Optional UUID of the :TurnEvidence node for the turn this memory was written in the context of. For source='user'/'manual' it also grounds the user-tier claim (an ungrounded claim is downgraded). For every other source it draws the provenance edge only, which is what lets a typed-scalar assertion reach the entities extracted from this memory -- pass it whenever you know it.

        Returns:
            Confirmation with the episode ID and counts of nodes/edges created.
        """
        backend = self.get_backend()
        from menhir.domain.bootstrap_scope import normalize_bootstrap_scope
        from menhir.domain.namespace import namespace_group_id_error

        namespace_error = namespace_group_id_error(namespace)
        if namespace_error is not None:
            return f"Cannot store memory: {namespace_error}"

        normalized_bootstrap_scope = normalize_bootstrap_scope(bootstrap_scope)
        if normalized_bootstrap_scope is not None and not flagged:
            return "Cannot store memory: bootstrap_scope requires flagged=true."

        # TEMPORAL direct-write path — bypasses Graphiti enrichment queue
        if type.upper() == "TEMPORAL" and valid_at:
            scope_kwargs: dict[str, object] = {}
            if normalized_bootstrap_scope is not None:
                scope_kwargs["bootstrap_scope"] = normalized_bootstrap_scope
            result = await backend.create_temporal(
                content=text,
                target_date=valid_at,
                source=source,
                flagged=flagged,
                namespace=namespace or None,
                turn_evidence_uuid=turn_evidence_uuid,
                **scope_kwargs,
            )
            flag_note = ", flagged=true" if flagged else ""
            if normalized_bootstrap_scope is not None:
                flag_note += f", bootstrap_scope={normalized_bootstrap_scope}"
            return (
                f"Created TEMPORAL memory uuid={result.get('uuid')}, "
                f"target_date={result.get('target_date')}, status=open{flag_note}"
            )

        session = get_mcp_session()
        scope_kwargs = {}
        if normalized_bootstrap_scope is not None:
            scope_kwargs["bootstrap_scope"] = normalized_bootstrap_scope
        result = await backend.queue_episode(
            text,
            user_id=session.user_id,
            session_id=session.session_id,
            source=source,
            diff=diff,
            namespace=namespace or None,
            flagged=flagged,
            turn_evidence_uuid=turn_evidence_uuid,
            **scope_kwargs,
        )
        if str(result.get("status") or "") == "failed":
            return "Failed to store memory."
        flag_note = ", flagged=true" if flagged else ""
        if normalized_bootstrap_scope is not None:
            flag_note += f", bootstrap_scope={normalized_bootstrap_scope}"
        return (
            f"Queued. episode_id={result.get('episode_id')}, "
            f"state=PENDING, nodes={int(result.get('nodes_touched') or 0)}, edges={int(result.get('edges_touched') or 0)}, "
            f"{await _queue_summary(backend)}{flag_note}"
        )
