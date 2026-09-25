"""Shared schema, errors, and state types for :mod:`menhir.infrastructure.project_identity_binding`.

The constraint statements, the two binding errors, the binding dataclasses, and the constraint
bootstrap, moved verbatim from the facade module by the file-size refactor. The facade re-exports
every public name defined here, so existing import sites are unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "PROJECT_IDENTITY_CONSTRAINT",
    "PROJECT_IDENTITY_ROOT_CONSTRAINT",
    "PROJECT_IDENTITY_CONSTRAINTS",
    "IdentityBindingConflict",
    "IdentityRootContested",
    "BindingState",
    "PendingIdentityPublication",
    "ensure_binding_constraint",
]

PROJECT_IDENTITY_CONSTRAINT = (
    "CREATE CONSTRAINT project_identity_id_unique IF NOT EXISTS "
    "FOR (p:ProjectIdentity) REQUIRE p.project_id IS UNIQUE"
)

#: One ACTIVE binding per (host, normalized root). Retired bindings null `root_key` and so fall
#: outside it -- see the module docstring.
PROJECT_IDENTITY_ROOT_CONSTRAINT = (
    "CREATE CONSTRAINT project_identity_root_unique IF NOT EXISTS "
    "FOR (p:ProjectIdentity) REQUIRE (p.bound_host, p.root_key) IS UNIQUE"
)

PROJECT_IDENTITY_CONSTRAINTS = (
    PROJECT_IDENTITY_CONSTRAINT,
    PROJECT_IDENTITY_ROOT_CONSTRAINT,
)


class IdentityBindingConflict(RuntimeError):
    """One project id was presented from two different directories."""


class IdentityRootContested(RuntimeError):
    """One directory was claimed by two different project ids on the same host."""


@dataclass(frozen=True)
class BindingState:
    project_id: str
    canonical_root_path: str
    state: str  # "bound" | "conflicted" | "superseded"
    #: Bumped every time this identity CLAIMS a directory. A scan settles under one generation and
    #: must still hold it when it writes; see :class:`~menhir.infrastructure.structure_write_fence.
    #: IdentityClaim`. Without it, transferring a root away and back would let a scan settled
    #: before the round trip write as though nothing had happened -- the state and root checks both
    #: pass, and only the generation records that the directory changed hands in between.
    claim_generation: int = 0


@dataclass(frozen=True)
class PendingIdentityPublication:
    """A graph-committed binding whose checkout file still needs publication."""

    project_id: str
    canonical_root_path: str
    claim_generation: int


def ensure_binding_constraint(neo4j: Any) -> None:
    """Create both uniqueness constraints if absent.

    Separate from :func:`bind_project_identity` so a migration can assert them up front and fail
    before writing anything, rather than discovering mid-run that the guarantee is missing. Also
    invoked from the phase-1 schema bootstrap, because a constraint that only a migration script
    creates is one a fresh deployment does not have.
    """
    for statement in PROJECT_IDENTITY_CONSTRAINTS:
        neo4j.execute(statement, {})
