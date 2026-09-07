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

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from menhir.domain.projection import ProjectionDefinition, ProjectionTarget
from menhir.domain.projection_lifecycle import (
    ProjectionFreshnessCertificate,
    ProjectionWorkToken,
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


class _ScalarMaterializer(Protocol):
    """Transaction-scoped callback used by lifecycle certification."""

    def __call__(self, tx: Any, token: ProjectionWorkToken) -> str: ...


class _ProjectionLifecycle(Protocol):
    """Minimal lifecycle capability required by scalar reconciliation."""

    def dirty_targets(
        self,
        definition: ProjectionDefinition,
        targets: Sequence[ProjectionTarget],
        *,
        reason: str,
    ) -> tuple[ProjectionWorkToken, ...]: ...

    def commit(
        self,
        token: ProjectionWorkToken,
        *,
        derivation_id: str,
        materialize: _ScalarMaterializer,
    ) -> ProjectionFreshnessCertificate: ...

    def pending(self, *, limit: int = 100) -> tuple[ProjectionWorkToken, ...]: ...


def _require_token(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")
    return value.strip()


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


def _derivation_id(operation_id: str, token: ProjectionWorkToken) -> str:
    material = (
        f"scalar-reconcile-v1\0{operation_id}\0{token.work_key}\0{token.generation}"
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True)
class ScalarProjectionReconciliation:
    """One exact lifecycle generation successfully materialized and certified."""

    token: ProjectionWorkToken
    certificate: ProjectionFreshnessCertificate

    def __post_init__(self) -> None:
        if self.certificate.definition_id != self.token.definition_id:
            raise ValueError("certificate definition does not match work token")
        if self.certificate.definition_version != self.token.definition_version:
            raise ValueError("certificate definition version does not match work token")
        if self.certificate.target != self.token.target:
            raise ValueError("certificate target does not match work token")
        if self.certificate.generation != self.token.generation:
            raise ValueError("certificate generation does not match work token")
        if self.certificate.target_present != self.token.target_present:
            raise ValueError("certificate membership does not match work token")


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
        materializer: _ScalarMaterializer,
    ) -> None:
        self._lifecycle = lifecycle
        self._materializer = materializer

    def dirty_assertions(
        self,
        assertions: Iterable[object],
        *,
        reason: str = "typed_scalar_mutation",
    ) -> tuple[ProjectionWorkToken, ...]:
        """Advance work generations for the unique scalar slots touched by assertions."""

        reason = _require_token("reason", reason)
        targets = _unique_targets(assertions)
        if not targets:
            return ()
        return self._lifecycle.dirty_targets(
            SCALAR_STATE_PROJECTION,
            targets,
            reason=reason,
        )

    def commit_tokens(
        self,
        tokens: Iterable[ProjectionWorkToken],
        *,
        operation_id: str,
    ) -> tuple[ScalarProjectionReconciliation, ...]:
        """Fence/materialize/certify existing generations without advancing them again."""

        operation_id = _require_token("operation_id", operation_id)
        reconciled: list[ScalarProjectionReconciliation] = []
        for token in tokens:
            if not isinstance(token, ProjectionWorkToken):
                raise TypeError("tokens must contain ProjectionWorkToken values")
            if token.definition_id != SCALAR_STATE_PROJECTION.definition_id:
                raise ValueError("scalar reconciler received work for another projection definition")
            if token.definition_version != SCALAR_STATE_PROJECTION.version:
                raise ValueError("scalar reconciler received work for another projection version")
            certificate = self._lifecycle.commit(
                token,
                derivation_id=_derivation_id(operation_id, token),
                materialize=self._materializer,
            )
            reconciled.append(ScalarProjectionReconciliation(token, certificate))
        return tuple(reconciled)

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

        pending = tuple(
            token
            for token in self._lifecycle.pending(limit=limit)
            if token.definition_id == SCALAR_STATE_PROJECTION.definition_id
        )
        return self.commit_tokens(pending, operation_id=operation_id)
