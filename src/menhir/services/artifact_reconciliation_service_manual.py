"""Manual repair and source-preparation operations for artifact reconciliation.

Moved verbatim from ``artifact_reconciliation_service`` as a mixin combined by the facade
class; the facade module re-exports every name so existing import sites keep working
unchanged. ``self._require_repository`` resolves through the concrete
``ArtifactReconciliationService`` at call time.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from menhir.domain.artifact_reconciliation import route_for_path


class ArtifactReconciliationManualOps:
    """Operator escape hatches and the ordered source backfill/activation pass."""

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
