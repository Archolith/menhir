"""Shared contracts for artifact reconciliation: repository protocol, root guard, results.

Moved verbatim from ``artifact_reconciliation_service``; the facade module re-exports every
name so existing import sites keep working unchanged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence

from menhir.domain.artifact_reconciliation import (
    ArtifactSourceSnapshot,
    WorkArtifactIdentitySnapshot,
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


class CorpusRootUnavailableError(ValueError):
    """The requested corpus root could not be safely observed."""


def _require_readable_corpus_root(repo_root: str | Path) -> Path:
    try:
        root = Path(repo_root).resolve(strict=True)
    except OSError as exc:
        raise CorpusRootUnavailableError(
            f"corpus root is unavailable: {repo_root}"
        ) from exc
    if not root.is_dir():
        raise CorpusRootUnavailableError(f"corpus root is not a directory: {root}")
    try:
        with os.scandir(root) as entries:
            next(entries, None)
    except OSError as exc:
        raise CorpusRootUnavailableError(
            f"corpus root is unreadable: {root}"
        ) from exc
    return root


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
