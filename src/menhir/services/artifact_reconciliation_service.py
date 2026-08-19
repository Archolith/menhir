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
    ConflictKind,
    CorpusEntry,
    CorpusLane,
    GitRename,
    ReconciliationAction,
    ReconciliationReport,
    SAFE_ACTION_KINDS,
    observation_from_action,
    plan_reconciliation,
    route_for_path,
    WorkArtifactIdentitySnapshot,
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

    Audit only calls the read methods. Cursor advancement remains an
    explicit apply-only operation.
    """

    def list_artifact_source_snapshots(
        self, *, repository: str | None = None
    ) -> list[ArtifactSourceSnapshot]: ...

    def get_artifact_reconciliation_cursor(self, *, repository: str) -> str | None: ...

    def list_work_artifact_identities(
        self, *, artifact_uuids: Sequence[str]
    ) -> list[WorkArtifactIdentitySnapshot]: ...

    def list_unscoped_artifact_source_snapshots(
        self, *, paths: Sequence[str], artifact_uuids: Sequence[str]
    ) -> list[ArtifactSourceSnapshot]: ...

    def advance_artifact_reconciliation_cursor(
        self,
        *,
        repository: str,
        expected_commit: str | None,
        observed_commit: str,
        observed_at: str,
    ) -> dict[str, Any]: ...

    def artifact_reconciliation_preflight(self) -> dict[str, int]: ...

    def activate_artifact_reconciliation_schema(self) -> dict[str, Any]: ...


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
    cursor_advanced: bool = False
    cursor_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.refused_reason is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "plan_digest": self.plan_digest,
            "ok": self.ok,
            "refused_reason": self.refused_reason,
            "cursor_advanced": self.cursor_advanced,
            "cursor_reason": self.cursor_reason,
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
        root = Path(repo_root).resolve()
        name = self._require_repository(repository)
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

        current_cursor = self._repo.get_artifact_reconciliation_cursor(
            repository=report.repository
        )
        if current_cursor != report.cursor_commit:
            result.refused_reason = "reconciliation_cursor_changed"
            result.cursor_reason = "stale"
            return result

        if not report.evidence_base_valid:
            result.refused_reason = "git_evidence_base_unavailable"
            result.cursor_reason = "invalid_evidence_base"
            return result

        registrations = [
            action
            for action in report.actions
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

        blocking_conflicts = [
            action
            for action in report.actions
            if action.kind == ActionKind.CONFLICT
            and not self._conflict_allows_cursor_advance(action)
        ]
        acknowledged_conflicts = bool(result.conflicted) and not blocking_conflicts

        if blocking_conflicts:
            result.cursor_reason = "conflicts_present"
        elif result.skipped:
            result.cursor_reason = "writes_skipped"
        elif not report.observed_commit:
            result.cursor_reason = "observed_commit_unavailable"
        elif report.cursor_commit == report.observed_commit:
            result.cursor_advanced = True
            result.cursor_reason = (
                "already_current_with_acknowledged_conflicts"
                if acknowledged_conflicts
                else "already_current"
            )
        else:
            advanced = self._repo.advance_artifact_reconciliation_cursor(
                repository=report.repository,
                expected_commit=report.cursor_commit,
                observed_commit=report.observed_commit,
                observed_at=now,
            )
            result.cursor_advanced = bool(advanced.get("advanced"))
            if not result.cursor_advanced:
                result.refused_reason = "reconciliation_cursor_update_failed"
                result.cursor_reason = "compare_and_set_failed"
            elif acknowledged_conflicts:
                result.cursor_reason = "advanced_with_acknowledged_conflicts"
        return result

    @staticmethod
    def _conflict_allows_cursor_advance(action: ReconciliationAction) -> bool:
        """Return True only for a conflict that cannot hide source identity.

        An unclassified corpus entry has no graph identity to relocate or
        overwrite. Advancing past it preserves Git evidence for every existing
        source while the full scanner keeps reporting the entry on every audit.
        Any identity-bearing conflict remains a hard cursor barrier.
        """
        return (
            action.conflict_kind == ConflictKind.UNCLASSIFIED_NEW_SOURCE
            and action.source_uuid is None
            and action.source_identity is None
            and action.artifact_uuid is None
            and action.old_path is None
        )

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
        if action.kind == ActionKind.ADOPT_SOURCE_REPOSITORY:
            if not action.source_uuid:
                return {"applied": False, "reason": "source_uuid_not_backfilled"}
            return self._repo.relocate_artifact_source(
                source_uuid=action.source_uuid,
                old_locator={
                    "repository": "",
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
        if action.kind == ActionKind.ATTACH_SOURCE:
            return self._repo.attach_artifact_source(
                artifact_uuid=action.artifact_uuid or "",
                expected_artifact_type=action.artifact_type or "",
                repository=action.repository,
                path=action.path or "",
                medium=action.medium,
                observation=observation,
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

    def adopt_source_repository_manually(
        self,
        *,
        source_uuid: str,
        repository: str,
        medium: str,
        old_path: str,
        new_path: str,
        repo_root: str | Path = ".",
        expected_old_integrity: str | None = None,
    ) -> dict[str, Any]:
        """Assign an unscoped legacy source after explicit operator review."""
        from menhir.domain.artifact_reconciliation import (
            MatchBasis,
            SourceObservation,
            sha256_bytes,
        )

        name = self._require_repository(repository)
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
            lane=(route_for_path(new_path).lane if route_for_path(new_path) else None),
            observed_at=datetime.now(timezone.utc).isoformat(),
            basis=MatchBasis.NONE,
        )
        return self._repo.relocate_artifact_source(
            source_uuid=source_uuid,
            old_locator={"repository": "", "path": old_path, "medium": medium},
            new_locator={"repository": name, "path": new_path, "medium": medium},
            observation=observation,
            expected_integrity=expected_old_integrity,
        )

    def source_preflight(self) -> dict[str, int]:
        """Return the graph-wide preparation surface without writing."""
        return self._repo.artifact_reconciliation_preflight()

    def prepare_sources(self, *, expected_source_count: int) -> dict[str, Any]:
        """Backfill source UUIDs and locator keys, then activate constraints.

        Ordered deliberately: UUIDs first, then keys, then the schema pass that
        creates the uniqueness constraints. A constraint created over unstamped
        sources would fail on the nulls, and a constraint created over duplicate
        locator keys would fail on the very defect the audit is meant to report.
        """
        preflight = self.source_preflight()
        actual = int(preflight.get("sources", 0))
        if expected_source_count < 0 or actual != expected_source_count:
            raise ValueError(
                "source count changed: "
                f"expected {expected_source_count}, observed {actual}; nothing written"
            )
        blocker_keys = (
            "duplicate_artifact_uuids",
            "duplicate_source_uuids",
            "duplicate_raw_locators",
            "duplicate_locator_keys",
            "duplicate_cursor_repositories",
        )
        blockers = {key: preflight.get(key, 0) for key in blocker_keys if preflight.get(key, 0)}
        if blockers:
            raise ValueError(f"artifact reconciliation preparation blocked: {blockers}")

        stamped = {
            "source_uuids": self._repo.backfill_source_uuids(),
            "locator_keys": self._repo.backfill_current_locator_keys(),
        }
        schema = self._repo.activate_artifact_reconciliation_schema()
        if not schema.get("ready"):
            raise RuntimeError(
                "artifact reconciliation constraints are not ONLINE: "
                f"{schema.get('constraints_missing', [])}"
            )
        after = self.source_preflight()
        if after.get("missing_source_uuids") or after.get("missing_locator_keys"):
            raise RuntimeError(f"artifact reconciliation preparation incomplete: {after}")
        return {
            "preflight": preflight,
            "stamped": stamped,
            "schema": schema,
            "after": after,
        }
