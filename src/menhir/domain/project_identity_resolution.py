"""Deciding which identity a scan writes under, and what to do when that cannot be decided.

CF-257 phase 1. Resolution has three outcomes and only one of them is a decision the machine may
take on its own:

    file present, valid        -> RESOLVED, use its id
    file absent                -> NEEDS DECISION (adopt an existing id, or mint a new one)
    caller supplied an action  -> execute it

**Why absent is never an automatic mint.** The identity file is gitignored -- deliberately, so a
fork does not inherit its parent's identity -- which means a fresh clone, a new machine or a
``git clean`` removes it. Minting silently there orphans the project's whole silo: 15,636 entities
for menhir, 5,708 for archolith-bench, still in the graph and unreachable, with no error anywhere.
The decision is cheap; the silent version is not recoverable without noticing first.

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
    existing_file_id: str | None,
    candidate: IdentityCandidate | None,
    action: IdentityAction | None = None,
    adopt_project_id: str | None = None,
) -> IdentityResolution:
    """Decide the identity for a scan of *root_path*. Pure: no I/O, no minting.

    Reading the file, querying for a candidate and writing the chosen id all happen at the call
    site. Keeping the decision separate is what lets every branch -- including the ones that only
    occur on a fresh clone or a replaced machine -- be exercised without a filesystem or a graph.
    """
    # An EXPLICIT action outranks the file. Checking the file first made `identity_action` a no-op
    # wherever one already existed, so the two cases an operator most needs -- re-pointing a
    # checkout at a different identity, and forcing a fresh one after a bad adopt -- were
    # unreachable, silently, with the call reporting success.
    if existing_file_id and action is None:
        return IdentityResolution(
            status=ResolutionStatus.RESOLVED, project_id=existing_file_id
        )

    if action is IdentityAction.ADOPT:
        chosen = (adopt_project_id or "").strip()
        if not chosen:
            return IdentityResolution(
                status=ResolutionStatus.NEEDS_DECISION,
                reason="adopt_requires_project_id",
                directory=root_path,
                candidates=[candidate] if candidate else [],
            )
        return IdentityResolution(status=ResolutionStatus.RESOLVED, project_id=chosen)

    if action is IdentityAction.NEW:
        # The intended outcome for a genuinely new working copy -- including a deliberate second
        # checkout on another machine, which the gitignored design makes a separate project.
        return IdentityResolution(status=ResolutionStatus.RESOLVED, project_id=None)

    # No file and no instruction. A candidate makes this recoverable; its absence does not make it
    # automatic -- a moved repo, a replacement machine and a fresh clone all land here, and each
    # needs a person to say whether this directory continues an existing project.
    return IdentityResolution(
        status=ResolutionStatus.NEEDS_DECISION,
        reason="identity_file_missing" if candidate else "identity_file_missing_no_candidate",
        directory=root_path,
        candidates=[candidate] if candidate else [],
    )
