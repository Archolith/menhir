"""Internal execution lane for one host-registered projection definition."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from menhir.domain.projection import ProjectionDefinition
from menhir.domain.projection_lifecycle import ProjectionFreshnessCertificate, ProjectionWorkToken

ProjectionMaterializer = Callable[[Any, ProjectionWorkToken], str]


@dataclass(frozen=True)
class ProjectionReconciliation:
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


class ProjectionCommitQueue(Protocol):
    def pending(
        self, *, definition_id: str, definition_version: int, limit: int,
    ) -> tuple[ProjectionWorkToken, ...]: ...

    def commit(
        self,
        token: ProjectionWorkToken,
        *,
        derivation_id: str,
        materialize: ProjectionMaterializer,
    ) -> ProjectionFreshnessCertificate: ...


class ProjectionReconciler:
    """Bind one trusted materializer to an exact definition and version.

    Construction is local to the host. The lane reads existing work and uses
    the repository's transactional fence for every materialization.
    """

    def __init__(
        self,
        queue: ProjectionCommitQueue,
        definition: ProjectionDefinition,
        *,
        materializer: ProjectionMaterializer,
        derivation_prefix: str = "projection-reconcile-v1",
    ) -> None:
        self._queue = queue
        self._definition = definition
        self._materializer = materializer
        self._derivation_prefix = derivation_prefix

    def commit_tokens(
        self, tokens: Iterable[ProjectionWorkToken], *, operation_id: str,
    ) -> tuple[ProjectionReconciliation, ...]:
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise ValueError("operation_id must be a non-blank string")
        operation_id = operation_id.strip()
        reconciled: list[ProjectionReconciliation] = []
        for token in tokens:
            if not isinstance(token, ProjectionWorkToken):
                raise TypeError("tokens must contain ProjectionWorkToken values")
            if token.definition_id != self._definition.definition_id:
                raise ValueError("reconciler received work for another projection definition")
            if token.definition_version != self._definition.version:
                raise ValueError("reconciler received work for another projection version")
            material = (
                f"{self._derivation_prefix}\0{operation_id}\0{token.work_key}\0{token.generation}"
            ).encode("utf-8")
            certificate = self._queue.commit(
                token,
                derivation_id=hashlib.sha256(material).hexdigest(),
                materialize=self._materializer,
            )
            reconciled.append(ProjectionReconciliation(token, certificate))
        return tuple(reconciled)

    def drain_pending(self, *, operation_id: str, limit: int = 100) -> tuple[ProjectionReconciliation, ...]:
        tokens = self._queue.pending(
            definition_id=self._definition.definition_id,
            definition_version=self._definition.version,
            limit=limit,
        )
        return self.commit_tokens(tokens, operation_id=operation_id)
