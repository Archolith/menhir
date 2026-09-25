"""Digest-gated apply path for artifact reconciliation plans.

Moved verbatim from ``artifact_reconciliation_service`` as a mixin combined by the facade
class; the facade module re-exports every name so existing import sites keep working
unchanged. ``self.audit`` and the shared static helpers resolve through the concrete
``ArtifactReconciliationService`` at call time.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from menhir.domain.artifact_reconciliation import (
    ActionKind,
    ConflictKind,
    ReconciliationAction,
    SAFE_ACTION_KINDS,
    observation_from_action,
)
from menhir.services.artifact_reconciliation_service_contracts import ApplyResult


class ArtifactReconciliationApplyOps:
    """Write operations: re-derive the plan, apply safe actions, advance the cursor."""

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
                status_unresolved_reason=action.status_unresolved_reason,
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
