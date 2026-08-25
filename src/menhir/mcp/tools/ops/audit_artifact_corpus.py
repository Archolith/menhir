"""MCP tool: audit_artifact_corpus."""

from __future__ import annotations

from menhir.mcp.tools.base import BaseTextTool
from menhir.mcp.contracts import ToolScope


async def audit_artifact_corpus(
    repo_path: str,
    repository: str,
    from_commit: str = "",
    namespace: str = "",
) -> str:
    """Report whether a repository's work-artifact corpus matches the graph.

    Read-only. Compares every document in the configured corpus directories
    against the recorded artifact sources and summarizes what would change:
    sources that moved, sources whose content changed, documents with no
    artifact yet, and sources that can no longer be found.

    Conflicts are listed but bounded. For the full action ledger and to apply
    anything, use the operator CLI: ``menhir artifacts audit --repository <name>
    --json`` and ``menhir artifacts reconcile --repository <name> --apply
    --plan-digest <digest>``.

    Args:
        repo_path: Absolute path to the repository working tree to audit.
        repository: Repository name recorded on sources. Required because a
                    worktree directory name is not repository identity.
        from_commit: Optional override for the persisted reconciliation cursor.
                     It changes only the Git evidence interval.
        namespace: Optional silo to scope this operation to. Empty = every silo
                   (existing behavior). Distinct from `repository`: a repository is a
                   locator identity, not a tenancy boundary.

    Returns:
        A parity summary, the plan digest, and any conflicts or lane/lifecycle
        contradictions found.
    """
    return await AuditArtifactCorpusTool().execute(
        repo_path=repo_path, repository=repository, from_commit=from_commit,
        namespace=namespace,
    )


class AuditArtifactCorpusTool(BaseTextTool):
    name = "audit_artifact_corpus"
    # NAMESPACED, not OBJECT: `repository` reads like an object address but is a locator
    # identity, and `WorkArtifact.namespace` is the tenancy boundary. Two silos holding
    # artifacts for the same repository saw each other's titles, statuses and paths -- and
    # each other's artifacts reported as conflicts in their own corpus.
    scope = ToolScope.NAMESPACED
    title = "Audit Artifact Corpus"
    oauth_scopes = ("menhir:write",)
    read_only_hint = True
    destructive_hint = False
    open_world_hint = False
    description = (
        "Read-only parity report between a repository's work-artifact corpus and the graph."
    )

    async def endpoint(
        self, repo_path: str, repository: str, from_commit: str = "",
        namespace: str = "",
    ) -> str:
        backend = self.get_backend()
        result = await backend.fetch_artifact_corpus_audit(
            repo_path=repo_path,
            repository=repository,
            from_commit=from_commit or None,
            # Forwarded only when set: byte-identical call when unpinned.
            **({"namespace": namespace} if namespace else {}),
        )

        counts = result.get("counts") or {}
        lines = [
            f"corpus audit: {result.get('repository')} @ "
            f"{str(result.get('observed_commit') or 'unknown')[:12]}",
            f"  entries {counts.get('entries', 0)} / sources {counts.get('sources', 0)}",
            f"  actions {counts.get('by_kind', {})}",
            f"  by lane {counts.get('entries_by_lane', {})}",
            f"  stored cursor {str(result.get('cursor_commit') or 'none')[:12]}",
            f"  evidence base {str(result.get('evidence_from_commit') or 'full audit')[:12]}",
            f"  evidence valid {result.get('evidence_base_valid')}",
            f"  plan digest {result.get('plan_digest')}",
        ]

        conflicts = result.get("conflicts") or []
        if conflicts:
            lines.append(f"  conflicts ({len(conflicts)} shown, no mutation proposed):")
            for item in conflicts:
                lines.append(
                    f"    {item.get('conflict_kind')}  {item.get('path') or item.get('old_path')}"
                )
            truncated = int(result.get("conflicts_truncated") or 0)
            if truncated:
                lines.append(f"    ... {truncated} more (use the CLI for the full ledger)")

        contradictions = result.get("contradictions") or []
        if contradictions:
            lines.append("  lane/lifecycle contradictions (owner disposition required):")
            for item in contradictions:
                lines.append(
                    f"    {item.get('reason')}  {item.get('status')}  {item.get('path')}"
                )

        if not conflicts and not contradictions:
            lines.append("  no conflicts or contradictions")
        return "\n".join(lines)
