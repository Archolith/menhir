"""Orchestration for corpus audit, validation, and digest-gated apply.

Three operations sit here because they share one collector. The failure mode
this replaces was two collectors -- a migration script that scanned one
directory level and a graph that had been populated by something else -- which
is how a corpus ends up with 24 records nobody can find.

Audit is read-only and provably so: it never touches a write method. Apply
re-derives the whole plan and refuses if the digest moved, so an approved ledger
can only ever be applied to the state it was approved against.

Facade module: the shared contracts, the apply path, and the manual
repair/preparation methods live in the ``artifact_reconciliation_service_contracts``,
``artifact_reconciliation_service_apply``, and ``artifact_reconciliation_service_manual``
sibling modules, moved verbatim and re-exported here so existing import sites keep
working unchanged. Audit and validate stay in this module because tests patch
``collect_git_evidence`` on this module's namespace.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from menhir.domain.artifact_reconciliation import (
    CorpusEntry,
    CorpusLane,
    GitRename,
    ReconciliationReport,
    plan_reconciliation,
    route_for_path,
)
from menhir.domain.work_artifact import ArtifactMedium
from menhir.infrastructure.artifact_corpus_scanner import (
    GitEvidence,
    collect_git_evidence,
    read_index_links,
    scan_corpus,
)
from menhir.services.artifact_reconciliation_service_apply import (
    ArtifactReconciliationApplyOps,
)
from menhir.services.artifact_reconciliation_service_contracts import (
    ApplyResult,
    CorpusRootUnavailableError,
    SourceRepository,
    ValidationFinding,
    ValidationReport,
    _require_readable_corpus_root,
)
from menhir.services.artifact_reconciliation_service_manual import (
    ArtifactReconciliationManualOps,
)


class ArtifactReconciliationService(
    ArtifactReconciliationApplyOps,
    ArtifactReconciliationManualOps,
):
    def __init__(self, repository: Any) -> None:
        self._repo = repository

    # ------------------------------------------------------------------
    # Read-only
    # ------------------------------------------------------------------

    def audit(
        self,
        repo_root: str | Path,
        *,
        repository: str | None = None,
        from_commit: str | None = None,
        git: GitEvidence | None = None,
        extra_renames: Sequence[GitRename] = (),
        namespace: str | None = None,
    ) -> ReconciliationReport:
        """Compare the tree against the graph. Zero writes, by construction.

        Graph locators are repository-scoped identities. Inferring that identity
        from a worktree directory name can make an existing repository appear
        empty, so every graph-backed caller must supply it explicitly.

        ``namespace`` is a separate axis and is opt-in. A repository is not a tenancy
        boundary; `WorkArtifact.namespace` is. All three graph reads below carry the filter --
        omitting it from any one of them would leave the audit reporting another silo's
        artifacts as conflicts or contradictions in the caller's own corpus.
        """
        name = self._require_repository(repository)
        root = _require_readable_corpus_root(repo_root)
        cursor_commit = self._repo.get_artifact_reconciliation_cursor(repository=name)
        evidence_from_commit = from_commit or cursor_commit
        evidence = (
            git
            if git is not None
            else collect_git_evidence(root, from_commit=evidence_from_commit)
        )
        entries = scan_corpus(root, repository=name, git=evidence)
        snapshots = self._repo.list_artifact_source_snapshots(
            repository=name, namespace=namespace
        )
        declared_uuids = sorted(
            {entry.declared_uuid for entry in entries if entry.declared_uuid}
        )
        unscoped_snapshots = self._repo.list_unscoped_artifact_source_snapshots(
            paths=sorted({entry.path for entry in entries}),
            artifact_uuids=declared_uuids,
            namespace=namespace,
        )
        identities = (
            self._repo.list_work_artifact_identities(
                artifact_uuids=declared_uuids, namespace=namespace
            )
            if declared_uuids
            else []
        )
        renames = tuple(evidence.renames) + tuple(extra_renames)
        evidence_base_valid = (
            evidence_from_commit is None or evidence.rename_evidence_available
        )
        return plan_reconciliation(
            repository=name,
            entries=entries,
            snapshots=snapshots,
            unscoped_snapshots=unscoped_snapshots,
            identities=identities,
            renames=renames,
            observed_commit=evidence.observed_commit,
            cursor_commit=cursor_commit,
            evidence_from_commit=evidence_from_commit,
            evidence_base_valid=evidence_base_valid,
        )

    def validate(
        self, repo_root: str | Path, *, repository: str | None = None
    ) -> ValidationReport:
        """Check the corpus against the authoring contract. No graph access.

        Runs for authors before a commit and for the auditor as a precondition,
        which is why it takes no repository connection: a document is either
        well-formed or it is not, and that answer must not depend on whether a
        database happens to be up.
        """
        root = Path(repo_root).resolve()
        name = repository or root.name
        entries = scan_corpus(root, repository=name)
        findings: list[ValidationFinding] = []

        by_uuid: dict[str, list[CorpusEntry]] = {}
        for entry in entries:
            for code in entry.metadata_errors:
                base, _, detail = code.partition(":")
                findings.append(ValidationFinding(entry.path, base, detail))
            if entry.declared_uuid:
                by_uuid.setdefault(entry.declared_uuid, []).append(entry)
            if not entry.title_from_h1 and entry.medium != ArtifactMedium.PDF:
                findings.append(ValidationFinding(entry.path, "missing_h1_title"))
            if entry.requires_declared_type and not entry.declared_type:
                findings.append(
                    ValidationFinding(
                        entry.path, "reference_record_without_declared_type"
                    )
                )

        for declared_uuid, group in sorted(by_uuid.items()):
            if len(group) > 1:
                for entry in group:
                    findings.append(
                        ValidationFinding(
                            entry.path,
                            "duplicate_artifact_uuid",
                            ", ".join(sorted(e.path for e in group)),
                        )
                    )

        findings.extend(self._index_membership_findings(root, entries))
        return ValidationReport(
            repository=name,
            checked=len(entries),
            findings=tuple(sorted(findings, key=lambda f: (f.path, f.code))),
        )

    @staticmethod
    def _index_membership_findings(
        root: Path, entries: Sequence[CorpusEntry]
    ) -> list[ValidationFinding]:
        """Every routed document should be reachable from its directory index.

        Archived records are exempt: an archive index that had to list ninety
        documents would be a directory listing with extra steps, and nobody
        navigates to an archived plan through it.
        """
        findings: list[ValidationFinding] = []
        cache: dict[str, frozenset[str]] = {}
        for entry in entries:
            if entry.lane == CorpusLane.ARCHIVE:
                continue
            route = route_for_path(entry.path)
            if route is None:
                continue
            links = cache.get(route.directory)
            if links is None:
                links = read_index_links(root, route.directory)
                cache[route.directory] = links
            if not links:
                continue  # no index to be missing from
            if Path(entry.path).name not in links:
                findings.append(
                    ValidationFinding(
                        entry.path, "not_listed_in_corpus_index", route.directory
                    )
                )
        return findings

    @staticmethod
    def _require_repository(repository: str | None) -> str:
        name = (repository or "").strip()
        if not name:
            raise ValueError(
                "repository is required for graph-backed artifact reconciliation"
            )
        return name
