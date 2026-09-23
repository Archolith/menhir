"""Deciding which identity a scan writes under, and what to do when that cannot be decided.

CF-257. Identity lives only in the graph; Menhir writes nothing into a checkout. Resolution has
three outcomes and only one of them is a decision the machine may take on its own:

    directory verified         -> RESOLVED, use the bound id
    not verified               -> NEEDS DECISION (adopt an existing id, or mint a new one)
    caller supplied an action  -> execute it

"Verified" is established by the caller: this host has an active binding for the directory AND
the checkout is the one that binding recorded (same repository origin; for a legacy binding that
recorded none, a legacy ``.agent/project-id`` naming the same id).

**Why unverified is never an automatic mint or reuse.** A fresh clone, a new machine, a moved repo
or a different repository cloned into a bound directory all land here. Minting silently orphans the
project's whole silo (15,636 entities for menhir, 5,708 for archolith-bench, unreachable with no
error); reusing silently writes one repository's structure into another's silo. The decision is
cheap; the silent version is not recoverable without noticing first.

**Why this is a typed result rather than a prompt.** The callers are one-shot MCP and HTTP
requests with no interactive channel, and the structure watcher is fully unattended. A "prompt"
would mean blocking a request that cannot answer, or a background job inventing an answer. So the
undecidable case returns a value the caller can act on, and the watcher treats it as skip-and-report.

**Why the candidate carries entity_count and last_scan.** An operator answering ``new`` where they
meant ``adopt`` does exactly the damage a silent mint would. Those two numbers are what makes the
choice informed, so they are part of the contract rather than a nicety.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "IdentityAction",
    "IdentityCandidate",
    "IdentityResolution",
    "ResolutionStatus",
    "resolve_identity",
]


class ResolutionStatus(Enum):
    RESOLVED = "resolved"
    NEEDS_DECISION = "needs_decision"


class IdentityAction(Enum):
    ADOPT = "adopt"
    NEW = "new"


@dataclass(frozen=True)
class IdentityCandidate:
    """An existing identity this directory might be the continuation of."""

    project_id: str
    display_name: str
    entity_count: int
    last_scan: str
    recorded_root_path: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "display_name": self.display_name,
            "entity_count": self.entity_count,
            "last_scan": self.last_scan,
            "recorded_root_path": self.recorded_root_path,
        }


@dataclass(frozen=True)
class IdentityResolution:
    status: ResolutionStatus
    project_id: str | None = None
    reason: str = ""
    directory: str = ""
    candidates: list[IdentityCandidate] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.status is ResolutionStatus.RESOLVED

    def as_dict(self) -> dict[str, Any]:
        """The payload a one-shot caller receives, including how to retry."""
        if self.resolved:
            return {"status": "resolved", "project_id": self.project_id}
        return {
            "status": "needs_decision",
            "reason": self.reason,
            "directory": self.directory,
            "candidates": [c.as_dict() for c in self.candidates],
            "retry_with": {
                "identity_action": "adopt|new",
                "adopt_project_id": "<project_id from candidates, required for adopt>",
            },
        }


def resolve_identity(
    *,
    root_path: str,
    verified_project_id: str | None,
    candidates: list[IdentityCandidate],
    action: IdentityAction | None = None,
    adopt_project_id: str | None = None,
    reason: str = "",
) -> IdentityResolution:
    """Decide the identity for a scan of *root_path*. Pure: no I/O, no minting.

    Verifying the directory, querying for candidates and binding the chosen id all happen at the
    call site. *reason* names why an unverified directory needs a decision. Keeping the decision separate is what lets every branch -- including the ones that only
    occur on a fresh clone or a replaced machine -- be exercised without a filesystem or a graph.
    """
    # An EXPLICIT action outranks the binding: re-pointing a checkout at a different identity, and
    # forcing a fresh one after a bad adopt, are the two cases an operator most needs.
    if verified_project_id and action is None:
        return IdentityResolution(
            status=ResolutionStatus.RESOLVED, project_id=verified_project_id
        )

    if action is IdentityAction.ADOPT:
        chosen = (adopt_project_id or "").strip()
        if not chosen:
            return IdentityResolution(
                status=ResolutionStatus.NEEDS_DECISION,
                reason="adopt_requires_project_id",
                directory=root_path,
                candidates=list(candidates),
            )
        return IdentityResolution(status=ResolutionStatus.RESOLVED, project_id=chosen)

    if action is IdentityAction.NEW:
        # The intended outcome for a genuinely new working copy -- including a deliberate second
        # checkout on another machine, which the gitignored design makes a separate project.
        return IdentityResolution(status=ResolutionStatus.RESOLVED, project_id=None)

    # Unverified and no instruction. A candidate makes this recoverable; its absence does not make
    # it automatic -- each case needs a person to say whether this directory continues a project.
    return IdentityResolution(
        status=ResolutionStatus.NEEDS_DECISION,
        reason=reason or ("directory_not_bound" if candidates else "directory_not_bound_no_candidate"),
        directory=root_path,
        candidates=list(candidates),
    )
