"""Live reconciliation coordinator for lifecycle-owned typed-scalar projection slots.

A scalar assertion write and projection materialization cannot share one transaction today because
assertion persistence owns its existing transaction boundary. This coordinator therefore uses the
T5 durable work generation as the crash-recovery boundary:

1. dirty the exact affected scalar slot(s);
2. fence/materialize/certify each generation through an injected lifecycle/materializer seam;
3. leave any uncommitted generation pending if materialization raises or the process exits.

The legacy entity-wide rebuild remains available to callers during staged cutover. This module owns
only lifecycle reconciliation and never clears assertion-level ``projection_pending`` markers; the
caller may do that only after the returned token is certified.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from menhir.domain.projection import ProjectionDefinition, ProjectionTarget
from menhir.domain.projection_lifecycle import ProjectionWorkToken
from menhir.services.projection_reconciliation import (
    ProjectionCommitQueue,
    ProjectionMaterializer,
    ProjectionReconciliation,
    ProjectionReconciler,
)
from menhir.services.scalar_projection_definition import (
    SCALAR_STATE_PROJECTION,
    scalar_projection_target,
)

__all__ = [
    "ScalarProjectionReconciliation",
    "ScalarProjectionReconciler",
]

_TARGET_FIELDS = (
    "subject_uuid",
    "namespace",
    "attribute",
    "scope",
    "value_kind",
    "unit",
    "operation",
)


class _ProjectionLifecycle(ProjectionCommitQueue, Protocol):
    """Minimal lifecycle capability required by scalar reconciliation."""

    def dirty_targets(
        self,
        definition: ProjectionDefinition,
        targets: Sequence[ProjectionTarget],
        *,
        reason: str,
    ) -> tuple[ProjectionWorkToken, ...]: ...

def _target_row(assertion: object) -> dict[str, Any]:
    """Expose only the projection-identity fields from a row or live TypedAssertion object."""

    if isinstance(assertion, Mapping):
        return dict(assertion)
    row = {field: getattr(assertion, field, None) for field in _TARGET_FIELDS}
    if not row["subject_uuid"]:
        raise TypeError("assertions must be mappings or expose typed-scalar identity fields")
    return row


def _unique_targets(assertions: Iterable[object]) -> tuple[ProjectionTarget, ...]:
    targets: dict[tuple[int, str, str, tuple[str, ...]], ProjectionTarget] = {}
    for assertion in assertions:
        target = scalar_projection_target(_target_row(assertion))
        targets[target.sort_key] = target
    return tuple(targets[key] for key in sorted(targets))


@dataclass(frozen=True)
class ScalarProjectionReconciliation(ProjectionReconciliation):
    """One exact scalar generation successfully materialized and certified."""


class ScalarProjectionReconciler:
    """Dirty and reconcile exact ``typed_scalar.current_state`` lifecycle targets.

    ``reconcile_assertions`` is the live mutation seam. It advances generations before attempting
    materialization, so a crash or exception after dirtying cannot falsely look fresh. ``drain_pending``
    is the recovery seam and commits existing generations without dirtying them again.
    """

    def __init__(
        self,
        lifecycle: _ProjectionLifecycle,
        *,
        materializer: ProjectionMaterializer,
    ) -> None:
        self._lifecycle = lifecycle
        self._reconciler = ProjectionReconciler(
            lifecycle,
            SCALAR_STATE_PROJECTION,
            materializer=materializer,
            derivation_prefix="scalar-reconcile-v1",
        )

    def dirty_assertions(
        self,
        assertions: Iterable[object],
        *,
        reason: str = "typed_scalar_mutation",
    ) -> tuple[ProjectionWorkToken, ...]:
        """Advance work generations for the unique scalar slots touched by assertions."""

        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-blank string")
        targets = _unique_targets(assertions)
        if not targets:
            return ()
        return self._lifecycle.dirty_targets(
            SCALAR_STATE_PROJECTION,
            targets,
            reason=reason.strip(),
        )

    def commit_tokens(
        self,
        tokens: Iterable[ProjectionWorkToken],
        *,
        operation_id: str,
    ) -> tuple[ScalarProjectionReconciliation, ...]:
        """Fence/materialize/certify existing generations without advancing them again."""

        committed = self._reconciler.commit_tokens(tokens, operation_id=operation_id)
        return tuple(ScalarProjectionReconciliation(result.token, result.certificate) for result in committed)

    def reconcile_assertions(
        self,
        assertions: Iterable[object],
        *,
        operation_id: str,
        reason: str = "typed_scalar_mutation",
    ) -> tuple[ScalarProjectionReconciliation, ...]:
        """Dirty exact affected slots, then synchronously reconcile their new generations.

        If a commit raises, every not-yet-certified generation remains durable and discoverable via
        the injected lifecycle repository's pending-work seam. The caller must not clear its own
        projection recovery marker for the failed assertion batch.
        """

        tokens = self.dirty_assertions(assertions, reason=reason)
        return self.commit_tokens(tokens, operation_id=operation_id)

    def drain_pending(
        self,
        *,
        operation_id: str,
        limit: int = 100,
    ) -> tuple[ScalarProjectionReconciliation, ...]:
        """Recover already-dirty scalar generations without incrementing their generation."""

        committed = self._reconciler.drain_pending(operation_id=operation_id, limit=limit)
        return tuple(ScalarProjectionReconciliation(result.token, result.certificate) for result in committed)
