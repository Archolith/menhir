"""MCP tool: add_candidate."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def add_candidate(
    content: str,
    source: str,
    cluster_id: str,
    label: str,
    kind: str = "memory",
    candidate_type: str = "other",
    type: str = "SEMANTIC",
    evidence_strength: str = "REPEATED",
    distinct_sessions: int = 0,
    first_seen: str | None = None,
    last_seen: str | None = None,
    notes: list[str] | None = None,
    source_confidence: float = 0.5,
    namespace: str = "",
) -> str:
    """Stage a low-trust memory/friction candidate for human review (not recalled until approved).

    Args:
        content: The candidate content to stage for review.
        source: Source label for where this candidate originated.
        cluster_id: Cluster identifier for grouping related candidates.
        label: Human-readable label for this candidate.
        kind: Type of candidate (default: memory).
        candidate_type: Candidate classification (default: other).
        type: Memory type (default: SEMANTIC).
        evidence_strength: Strength of supporting evidence (default: REPEATED).
        distinct_sessions: Number of distinct sessions providing evidence (default: 0).
        first_seen: ISO date when candidate was first observed (optional).
        last_seen: ISO date when candidate was last observed (optional).
        notes: List of supporting notes or context (optional).
        source_confidence: Confidence level from 0.0 to 1.0 (default: 0.5).

    Returns:
        Confirmation with the candidate uuid, cluster_id, and review status.
    """

    return await AddCandidateTool().execute(
        content=content,
        source=source,
        cluster_id=cluster_id,
        label=label,
        kind=kind,
        candidate_type=candidate_type,
        type=type,
        evidence_strength=evidence_strength,
        distinct_sessions=distinct_sessions,
        first_seen=first_seen,
        last_seen=last_seen,
        notes=notes,
        source_confidence=source_confidence,
        namespace=namespace,
    )


class AddCandidateTool(BaseTextTool):
    name = "add_candidate"
    scope = ToolScope.NAMESPACED
    description = "Stage a low-trust memory/friction candidate for human review (not recalled until approved)."
    title = "Add Candidate"
    oauth_scopes = ("menhir:write",)
    read_only_hint = False
    destructive_hint = False
    open_world_hint = False

    async def endpoint(
        self,
        content: str,
        source: str,
        cluster_id: str,
        label: str,
        kind: str = "memory",
        candidate_type: str = "other",
        type: str = "SEMANTIC",
        evidence_strength: str = "REPEATED",
        distinct_sessions: int = 0,
        first_seen: str | None = None,
        last_seen: str | None = None,
        notes: list[str] | None = None,
        source_confidence: float = 0.5,
        namespace: str = "",
    ) -> str:
        """Stage a low-trust memory/friction candidate for human review (not recalled until approved).

        Args:
            content: The candidate content to stage for review.
            source: Source label for where this candidate originated.
            cluster_id: Cluster identifier for grouping related candidates.
            label: Human-readable label for this candidate.
            kind: Type of candidate (default: memory).
            candidate_type: Candidate classification (default: other).
            type: Memory type (default: SEMANTIC).
            evidence_strength: Strength of supporting evidence (default: REPEATED).
            distinct_sessions: Number of distinct sessions providing evidence (default: 0).
            first_seen: ISO date when candidate was first observed (optional).
            last_seen: ISO date when candidate was last observed (optional).
            notes: List of supporting notes or context (optional).
            source_confidence: Confidence level from 0.0 to 1.0 (default: 0.5).

        Returns:
            Confirmation with the candidate uuid, cluster_id, and review status.
        """
        backend = self.get_backend()

        # CF-33 step 4 resolved this as a MISDECLARATION, not a missing guard: `cluster_id`
        # is an emitter-side grouping label on a NEW write, not the address of an existing
        # object, so OBJECT never described this tool. It stages tenant memory -- an approved
        # candidate becomes recalled memory -- and did so with a hardcoded default group_id.
        scope = {"namespace": namespace} if namespace else {}
        result = await backend.create_candidate(
            content=content,
            source=source,
            cluster_id=cluster_id,
            label=label,
            kind=kind,
            candidate_type=candidate_type,
            type=type,
            evidence_strength=evidence_strength,
            distinct_sessions=distinct_sessions,
            first_seen=first_seen,
            last_seen=last_seen,
            notes=notes,
            source_confidence=source_confidence,
            **scope,
        )

        return (
            f"{'Created' if result.get('created') else 'Refreshed'} CANDIDATE uuid={result.get('uuid')}, "
            f"cluster_id={result.get('cluster_id')}, evidence_strength={result.get('evidence_strength')}, "
            f"distinct_sessions={result.get('distinct_sessions')} (held for review; not recalled until approved)"
        )
