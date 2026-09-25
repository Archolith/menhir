"""Record and provenance types for the remote snapshot wire contract.

Split out of :mod:`menhir.snapshot.protocol` (the wire-contract facade), which re-exports every
public name defined here; import these from the facade, not from this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "PROVENANCE_SELF_REPORTED",
    "PROVENANCE_TRUSTED_AUTOMATION",
    "FileRecord",
    "GitProvenance",
    "Omission",
    "OmissionReason",
]

#: Provenance quality. The client may only ever assert the first: it is describing its own
#: machine, and nothing it says about a commit can be checked from inside the bundle (the bundle
#: has no `.git`). The server assigns the second from trusted forge or CI evidence -- never from
#: anything a caller sent. A remote URL, repository name, branch or commit string is a claim, and
#: a claim is never authorization (plan: "Source classes and project identity").
PROVENANCE_SELF_REPORTED = "self_reported"
PROVENANCE_TRUSTED_AUTOMATION = "trusted_automation"


@dataclass(frozen=True)
class GitProvenance:
    """Where the bundled bytes came from, as a CLAIM about the source checkout.

    Replaces the single `source_head` field, which could not answer the question that matters for
    grounding a code memory: *were these the bytes at that commit, or bytes someone was still
    editing?* A commit id alone reads as the former and is frequently the latter.

    What each field is for:

    ``base_commit``
        The commit HEAD pointed at. A label, not an anchor -- plan invariant 13 forbids a
        caller-reported commit from being the sole historical anchor of anything.
    ``commit_tree``
        The tree OID of that commit. Present so a server that ever gains the commit's objects can
        compare them against what arrived; `base_commit` alone cannot be checked against content.
    ``branch``
        The branch label, or None when detached. Display metadata: durable isolation uses
        `view_id`, so renames, detached heads and duplicate branch names do not collide.
    ``dirty``
        True when tracked working-tree bytes differ from ``base_commit``. This describes the
        SOURCE, not the bundle. Untracked files are excluded from the comparison because they
        never enter the bundle, so they cannot make it differ from the commit.
    ``quality``
        Always :data:`PROVENANCE_SELF_REPORTED` from a client.

    **`dirty=False` does not mean the bundle equals the commit.** The selection policy drops
    scanner-skipped directories, oversized files and refused paths, and tracked-but-deleted files
    appear as deletions. Those are declared separately (`omissions`, `deleted_count`) and a reader
    must consult them; only a clean tree with neither would claim to be the commit's tracked
    content, and even then no one inside this system can verify it. `tree_digest` is the
    authoritative statement about the uploaded bytes; everything here is provenance.
    """

    base_commit: str | None = None
    commit_tree: str | None = None
    branch: str | None = None
    dirty: bool = False
    quality: str = PROVENANCE_SELF_REPORTED

    def as_json(self) -> dict[str, Any]:
        return {
            "base_commit": self.base_commit,
            "commit_tree": self.commit_tree,
            "branch": self.branch,
            "dirty": self.dirty,
            "quality": self.quality,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> GitProvenance:
        """Parse untrusted provenance. Unparseable values become absent, never invented.

        `quality` is forced back to self-reported: a client claiming to be trusted automation is
        exactly the claim this field exists to refuse. Only the server may raise it.
        """
        if not isinstance(raw, Mapping):
            return cls()
        return cls(
            base_commit=_optional_str(raw.get("base_commit")),
            commit_tree=_optional_str(raw.get("commit_tree")),
            branch=_optional_str(raw.get("branch")),
            dirty=bool(raw.get("dirty", False)),
            quality=PROVENANCE_SELF_REPORTED,
        )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class OmissionReason:
    """Why a tracked path is absent from the bundle on purpose.

    The distinction that matters to the server: an omission is NOT a deletion. The extracted tree
    cannot tell them apart by itself, so a snapshot that omits a submodule would otherwise look
    like a snapshot in which those files were removed -- and structure writes prune.
    """

    SUBMODULE = "submodule"
    SYMLINK = "symlink"
    OVERSIZE = "oversize"
    EXCLUDED_DIR = "excluded_dir"
    RECEIPT = "local_receipt"
    SECRET_RISK = "secret_risk"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class FileRecord:
    """One regular file in the bundle."""

    path: str
    size: int
    sha256: str
    executable: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "executable": self.executable,
        }


@dataclass(frozen=True)
class Omission:
    """A declared, deliberate absence."""

    path: str
    reason: str

    def as_json(self) -> dict[str, Any]:
        return {"path": self.path, "reason": self.reason}
