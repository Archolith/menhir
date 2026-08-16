"""Domain-neutral projection lifecycle tokens and errors.

The work token is an optimistic capability for one exact semantic generation. It does not grant
permission to write by itself: the infrastructure committer must re-check it while holding the
same transaction lock that guards the materialization write.
"""

from __future__ import annotations

from dataclasses import dataclass

from menhir.domain.projection import ProjectionTarget

__all__ = [
    "ProjectionDefinitionNotPublishedError",
    "ProjectionLifecycleCorruptionError",
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
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"ProjectionWorkToken.{name} must be non-blank")
        if not isinstance(self.definition_version, int) or self.definition_version < 1:
            raise ValueError("ProjectionWorkToken.definition_version must be >= 1")
        if not isinstance(self.generation, int) or self.generation < 1:
            raise ValueError("ProjectionWorkToken.generation must be >= 1")
        if not isinstance(self.target, ProjectionTarget):
            raise TypeError("ProjectionWorkToken.target must be a ProjectionTarget")
