"""Domain-neutral projection lifecycle contracts.

Lifecycle state is deliberately separate from projection semantics and View persistence. A work
token is an optimistic capability for one exact semantic generation; a freshness certificate proves
that one exact generation of one exact semantic-definition version produced the exact persisted
projection hash named by the certificate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from menhir.domain.projection import ProjectionTarget

__all__ = [
    "ProjectionDefinitionNotPublishedError",
    "ProjectionDefinitionPublication",
    "ProjectionFreshnessAssessment",
    "ProjectionFreshnessCertificate",
    "ProjectionFreshnessState",
    "ProjectionLifecycleCorruptionError",
    "ProjectionLifecycleError",
    "ProjectionWorkAlreadyCompletedError",
    "ProjectionWorkToken",
    "StaleProjectionDefinitionError",
    "StaleProjectionWorkError",
]


class ProjectionLifecycleError(RuntimeError):
    """Base class for projection lifecycle contract failures."""


class ProjectionDefinitionNotPublishedError(ProjectionLifecycleError):
    """Raised when work is attempted before its semantic definition is published."""


class StaleProjectionDefinitionError(ProjectionLifecycleError):
    """Raised when an old process attempts work under a superseded definition version."""


class StaleProjectionWorkError(ProjectionLifecycleError):
    """Raised when a worker token no longer names the current target generation."""


class ProjectionWorkAlreadyCompletedError(ProjectionLifecycleError):
    """Raised when another worker already completed the exact token generation."""


class ProjectionLifecycleCorruptionError(ProjectionLifecycleError):
    """Raised when persisted lifecycle identity disagrees with the requested target/definition."""


def _require_nonblank(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")


def _require_version(name: str, value: object, *, allow_zero: bool = False) -> None:
    minimum = 0 if allow_zero else 1
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


@dataclass(frozen=True)
class ProjectionWorkToken:
    """Snapshot of one target's semantic work generation."""

    work_key: str
    definition_id: str
    definition_version: int
    target: ProjectionTarget
    generation: int
    target_present: bool
    reason: str

    def __post_init__(self) -> None:
        for name in ("work_key", "definition_id", "reason"):
            _require_nonblank(f"ProjectionWorkToken.{name}", getattr(self, name))
        _require_version("ProjectionWorkToken.definition_version", self.definition_version)
        _require_version("ProjectionWorkToken.generation", self.generation)
        if not isinstance(self.target, ProjectionTarget):
            raise TypeError("ProjectionWorkToken.target must be a ProjectionTarget")
        if not isinstance(self.target_present, bool):
            raise TypeError("ProjectionWorkToken.target_present must be a bool")


@dataclass(frozen=True)
class ProjectionDefinitionPublication:
    """Result of publishing one shared-current semantic definition."""

    definition_id: str
    version: int
    semantic_hash: str
    changed: bool
    invalidated_targets: int

    def __post_init__(self) -> None:
        _require_nonblank("ProjectionDefinitionPublication.definition_id", self.definition_id)
        _require_version("ProjectionDefinitionPublication.version", self.version)
        _require_nonblank("ProjectionDefinitionPublication.semantic_hash", self.semantic_hash)
        if not isinstance(self.changed, bool):
            raise TypeError("ProjectionDefinitionPublication.changed must be a bool")
        _require_version(
            "ProjectionDefinitionPublication.invalidated_targets",
            self.invalidated_targets,
            allow_zero=True,
        )


@dataclass(frozen=True)
class ProjectionFreshnessCertificate:
    """Proof that one exact persisted projection state completed one exact work generation."""

    definition_id: str
    definition_version: int
    target: ProjectionTarget
    generation: int
    target_present: bool
    projection_hash: str
    derivation_id: str

    def __post_init__(self) -> None:
        _require_nonblank("ProjectionFreshnessCertificate.definition_id", self.definition_id)
        _require_version(
            "ProjectionFreshnessCertificate.definition_version",
            self.definition_version,
        )
        if not isinstance(self.target, ProjectionTarget):
            raise TypeError("ProjectionFreshnessCertificate.target must be a ProjectionTarget")
        _require_version("ProjectionFreshnessCertificate.generation", self.generation)
        if not isinstance(self.target_present, bool):
            raise TypeError("ProjectionFreshnessCertificate.target_present must be a bool")
        _require_nonblank("ProjectionFreshnessCertificate.projection_hash", self.projection_hash)
        _require_nonblank("ProjectionFreshnessCertificate.derivation_id", self.derivation_id)


ProjectionFreshnessState = Literal["fresh", "stale", "unavailable"]


@dataclass(frozen=True)
class ProjectionFreshnessAssessment:
    """Read-side lifecycle state for one target and the adapter-reported current projection hash."""

    state: ProjectionFreshnessState
    reason: str
    definition_id: str
    target: ProjectionTarget
    current_definition_version: int | None
    target_present: bool | None
    work_generation: int | None
    applied_generation: int | None
    certified_definition_version: int | None
    certified_generation: int | None
    certified_projection_hash: str | None
    current_projection_hash: str | None
    derivation_id: str | None

    def __post_init__(self) -> None:
        if self.state not in {"fresh", "stale", "unavailable"}:
            raise ValueError("ProjectionFreshnessAssessment.state is invalid")
        _require_nonblank("ProjectionFreshnessAssessment.reason", self.reason)
        _require_nonblank("ProjectionFreshnessAssessment.definition_id", self.definition_id)
        if not isinstance(self.target, ProjectionTarget):
            raise TypeError("ProjectionFreshnessAssessment.target must be a ProjectionTarget")
        if self.target_present is not None and not isinstance(self.target_present, bool):
            raise TypeError("ProjectionFreshnessAssessment.target_present must be a bool or None")
        for name in (
            "current_definition_version",
            "work_generation",
            "applied_generation",
            "certified_definition_version",
            "certified_generation",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_version(
                    f"ProjectionFreshnessAssessment.{name}",
                    value,
                    allow_zero=name == "applied_generation",
                )

    @property
    def fresh(self) -> bool:
        return self.state == "fresh"

    @property
    def stale(self) -> bool:
        return self.state == "stale"
