"""Orchestration for corpus audit, validation, and digest-gated apply.

Three operations sit here because they share one collector. The failure mode
this replaces was two collectors -- a migration script that scanned one
directory level and a graph that had been populated by something else -- which
is how a corpus ends up with 24 records nobody can find.

Audit is read-only and provably so: it never touches a write method. Apply
re-derives the whole plan and refuses if the digest moved, so an approved ledger
can only ever be applied to the state it was approved against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, Sequence
from uuid import uuid4

from menhir.domain.artifact_reconciliation import (
    ActionKind,
    ArtifactSourceSnapshot,
    CorpusEntry,
    CorpusLane,
    GitRename,
    ReconciliationAction,
    ReconciliationReport,
    SAFE_ACTION_KINDS,
    observation_from_action,
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


class SourceRepository(Protocol):
    """The repository surface reconciliation needs. Kept narrow on purpose.

    Audit only ever sees ``list_artifact_source_snapshots``; a service that
    cannot reach a write method cannot accidentally perform one.
    """

    def list_artifact_source_snapshots(
        self, *, repository: str | None = None
    ) -> list[ArtifactSourceSnapshot]: ...


@dataclass(frozen=True)
class ValidationFinding:
    path: str
    code: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class ValidationReport:
    repository: str
    checked: int
    findings: tuple[ValidationFinding, ...]

    @property
    def ok(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "checked": self.checked,
            "ok": self.ok,
            "findings": [f.as_dict() for f in self.findings],
        }


@dataclass
class ApplyResult:
    """What apply actually did, split three ways rather than summed.

    Applied, skipped, and conflicted are separate lists because one conflict
    must not block unrelated safe work, and a caller reading a single total
    cannot tell a clean run from a half-refused one.
    """

    run_id: str
    plan_digest: str
    applied: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    conflicted: list[dict[str, Any]] = field(default_factory=list)
    refused_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.refused_reason is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "plan_digest": self.plan_digest,
            "ok": self.ok,
            "refused_reason": self.refused_reason,
            "counts": {
                "applied": len(self.applied),
                "skipped": len(self.skipped),
                "conflicted": len(self.conflicted),
            },
            "applied": self.applied,
            "skipped": self.skipped,
            "conflicted": self.conflicted,
        }


class ArtifactReconciliationService:
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
    ) -> ReconciliationReport:
        """Compare the tree against the graph. Zero writes, by construction.

        Graph locators are repository-scoped identities. Inferring that identity
        from a worktree directory name can make an existing repository appear
        empty, so every graph-backed caller must supply it explicitly.
        """
        root = Path(repo_root).resolve()
        name = self._require_repository(repository)
        evidence = git if git is not None else collect_git_evidence(root, from_commit=from_commit)
        entries = scan_corpus(root, repository=name, git=evidence)
        snapshots = self._repo.list_artifact_source_snapshots(repository=name)
        renames = tuple(evidence.renames) + tuple(extra_renames)
        return plan_reconciliation(
            repository=name,
            entries=entries,
            snapshots=snapshots,
            renames=renames,
            observed_commit=evidence.observed_commit,
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
                    ValidationFinding(entry.path, "reference_record_without_declared_type")
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

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def apply(
        self,
        repo_root: str | Path,
        *,
        expected_digest: str,
        repository: str | None = None,
        from_commit: str | None = None,
        allow_new_repository: bool = False,
    ) -> ApplyResult:
        """Re-derive the plan and apply only its safe actions.

        The premises are re-read rather than carried over from the audit. An
        approved ledger is approval for a state of the world, not a blank
        cheque, so if anything moved in between the digest no longer matches and
        nothing is written.
        """
        report = self.audit(repo_root, repository=repository, from_commit=from_commit)
        run_id = str(uuid4())
        result = ApplyResult(run_id=run_id, plan_digest=report.plan_digest)

        if report.plan_digest != expected_digest:
            result.refused_reason = "plan_digest_mismatch"
            return result

        registrations = [
            action for action in report.actions
            if action.kind == ActionKind.REGISTER_ARTIFACT
        ]
        if (
            registrations
            and int(report.counts.get("sources", 0)) == 0
            and not allow_new_repository
        ):
            result.refused_reason = "new_repository_requires_explicit_allow"
            return result

        now = datetime.now(timezone.utc).isoformat()
        for action in report.actions:
            if action.kind == ActionKind.CONFLICT:
                result.conflicted.append(action.as_dict())
                continue
            if action.kind not in SAFE_ACTION_KINDS:
                continue  # NOOP: nothing to do and nothing to report
            outcome = self._apply_one(
                action, observed_commit=report.observed_commit, now=now, run_id=run_id
            )
            record = {**action.as_dict(), "outcome": outcome}
            if outcome.get("applied"):
                result.applied.append(record)
            else:
                result.skipped.append(record)
        return result

    @staticmethod
    def _require_repository(repository: str | None) -> str:
        name = (repository or "").strip()
        if not name:
            raise ValueError(
                "repository is required for graph-backed artifact reconciliation"
            )
        return name

    def _apply_one(
        self,
        action: ReconciliationAction,
        *,
        observed_commit: str | None,
        now: str,
        run_id: str,
    ) -> dict[str, Any]:
        observation = observation_from_action(
            action, observed_commit=observed_commit, observed_at=now, run_id=run_id
        )
        if action.kind == ActionKind.REFRESH_SOURCE:
            if action.source_uuid:
                return self._repo.refresh_artifact_source(
                    source_uuid=action.source_uuid,
                    observation=observation,
                    expected_integrity=action.expected_integrity,
                )
            return self._repo.refresh_artifact_source_by_locator(
                repository=action.repository,
                medium=action.medium,
                path=action.path or "",
                observation=observation,
            )
        if action.kind == ActionKind.RELOCATE_SOURCE:
            if action.source_uuid:
                return self._repo.relocate_artifact_source(
                    source_uuid=action.source_uuid,
                    old_locator={
                        "repository": action.repository,
                        "path": action.old_path,
                        "medium": action.medium,
                    },
                    new_locator={
                        "repository": action.repository,
                        "path": action.path,
                        "medium": action.medium,
                    },
                    observation=observation,
                    expected_integrity=action.expected_integrity,
                )
            return self._repo.relocate_artifact_source_by_locator(
                repository=action.repository,
                medium=action.medium,
                old_path=action.old_path or "",
                new_path=action.path or "",
                observation=observation,
            )
        if action.kind == ActionKind.REGISTER_ARTIFACT:
            return self._repo.register_work_artifact(
                artifact_type=action.artifact_type or "",
                title=action.title or Path(action.path or "").stem,
                repository=action.repository,
                path=action.path or "",
                medium=action.medium,
                observation=observation,
                status=action.status,
                status_raw=action.raw_status_header,
                artifact_uuid=action.artifact_uuid,
                structure_project=action.repository,
            )
        if action.kind == ActionKind.MARK_SOURCE_UNRESOLVED:
            if not action.source_uuid:
                return {"applied": False, "reason": "source_uuid_not_backfilled"}
            return self._repo.mark_artifact_source_unresolved(
                source_uuid=action.source_uuid,
                reason=action.reason or "source_not_observed",
                observed_commit=observed_commit,
            )
        return {"applied": False, "reason": f"unsupported_action:{action.kind}"}

    def relocate_manually(
        self,
        *,
        repository: str,
        medium: str,
        old_path: str,
        new_path: str,
        repo_root: str | Path = ".",
        expected_old_integrity: str | None = None,
    ) -> dict[str, Any]:
        """Repair one move the detectors could not see.

        Hashes the destination if it is readable, so a manual repair records the
        same evidence an automatic one would. It runs the identical collision
        checks: the escape hatch must not be the weaker path, or it becomes the
        way every ambiguity gets resolved.
        """
        from menhir.domain.artifact_reconciliation import (
            MatchBasis,
            SourceObservation,
            sha256_bytes,
        )

        destination = Path(repo_root) / new_path
        integrity: str | None = None
        size_bytes: int | None = None
        if destination.is_file():
            payload = destination.read_bytes()
            integrity = sha256_bytes(payload)
            size_bytes = len(payload)

        observation = SourceObservation(
            integrity=integrity,
            size_bytes=size_bytes,
            observed_at=datetime.now(timezone.utc).isoformat(),
            basis=MatchBasis.NONE,
        )
        source_uuid, reason = self._repo._source_uuid_at_locator(  # noqa: SLF001
            repository, medium, old_path
        )
        if source_uuid is None:
            return {"applied": False, "reason": reason}
        return self._repo.relocate_artifact_source(
            source_uuid=source_uuid,
            old_locator={"repository": repository, "path": old_path, "medium": medium},
            new_locator={"repository": repository, "path": new_path, "medium": medium},
            observation=observation,
            expected_integrity=expected_old_integrity,
        )

    def prepare_sources(self) -> dict[str, int]:
        """Backfill source UUIDs and locator keys before constraints activate.

        Ordered deliberately: UUIDs first, then keys, then the schema pass that
        creates the uniqueness constraints. A constraint created over unstamped
        sources would fail on the nulls, and a constraint created over duplicate
        locator keys would fail on the very defect the audit is meant to report.
        """
        return {
            "source_uuids": self._repo.backfill_source_uuids(),
            "locator_keys": self._repo.backfill_current_locator_keys(),
        }
