"""Git change records (the ``GitChange`` value type shared by the structure-temporal modules).

Pure value types over ISO timestamps; no git/graph access (the caller supplies the change
log, e.g. from `git log --name-status` reconciled to :File/:Symbol identities).
"""

from __future__ import annotations

from dataclasses import dataclass

from menhir.domain.temporal import parse_iso8601


#: Tolerant Neo4j-timestamp parse (fail-safe: None when absent/unparseable, never raises).
#: Single definition in `domain.temporal` -- do not re-inline it.
_parse = parse_iso8601


@dataclass(frozen=True)
class GitChange:
    """One recorded change to a structural target (file / symbol / dependency).

    ``descends_from`` is the set of commits this change's commit is a descendant of (its
    ancestry, from parent topology) — the load-bearing field for correct cross-branch
    reasoning: a change invalidates a belief only if the belief's commit is in this set.
    ``worktree_hash`` is the anchor's content hash after the change (for working-tree/stash
    beliefs that have no commit).
    """

    target: str        # path or symbol id; matched against a belief's anchors
    changed_at: str    # ISO commit/author date
    kind: str = "file"  # file | symbol | dependency
    commit: str | None = None
    branch: str | None = None
    descends_from: frozenset[str] = frozenset()  # commits this change descends from (reachability)
    worktree_hash: str | None = None             # anchor content hash after this change
    renamed_from: str | None = None              # prior path/id if this change moved the code (git R / -M)

    def matches(self, anchor_set: set[str]) -> bool:
        """A change matches if it touches an anchor directly OR moved code from one (rename).

        Without rename-following, a change to renamed code is invisible to an anchor keyed
        to the old path — the durable-identity problem (chronostratum plan Rev 5)."""
        return self.target in anchor_set or (self.renamed_from is not None and self.renamed_from in anchor_set)
