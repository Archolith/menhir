"""Git-grounded structural staleness (R3 belief-layer Rung 2 / Chronostratum moat).

The belief currentness policy (belief.py) suppresses *superseded* beliefs, but its
supersession signal normally comes from extraction (IS_EXPIRED / LATER_CONTRADICTED),
which is noisy. The Chronostratum thesis: Git is recorded ground truth, so staleness of a
belief ABOUT CODE can be derived deterministically, with zero extraction reliance —

    a belief anchored to a file/symbol that was CHANGED by a commit AFTER the belief was
    formed is, by construction, a later contradiction of that belief.

This module is the deterministic PRODUCER of that signal. Given a belief's structural
anchors + when it was believed (created_at, from the bitemporal substrate, temporal.py) +
a git change log, it emits BeliefEvidence the scorer/currentness policy already understand:
a LATER_CONTRADICTED (so is_superseded_belief picks it up) plus the specific
FILE_CHANGED / SYMBOL_CHANGED / DEPENDENCY_CHANGED for provenance.

Pure functions over ISO timestamps; no git/graph access (the caller supplies the change
log, e.g. from `git log --name-status` reconciled to :File/:Symbol identities).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from menhir.domain.belief import BeliefEvidence, EvidencePolarity, EvidenceSignal
from menhir.domain.temporal import parse_iso8601


#: Tolerant Neo4j-timestamp parse (fail-safe: None when absent/unparseable, never raises).
#: Single definition in `domain.temporal` -- do not re-inline it.
_parse = parse_iso8601


# Change kind -> the structural evidence signal it grounds.
_KIND_SIGNAL: dict[str, EvidenceSignal] = {
    "file": EvidenceSignal.FILE_CHANGED,
    "symbol": EvidenceSignal.SYMBOL_CHANGED,
    "dependency": EvidenceSignal.DEPENDENCY_CHANGED,
}


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


@dataclass(frozen=True)
class BeliefCommitContext:
    """The git world a belief was formed against.

    A belief formed on a commit carries ``commit`` (+ ``branch``); one formed against
    uncommitted/stashed work carries ``worktree_hash`` (the anchor content hash at belief
    time) and usually no commit. This is what makes staleness branch- and stash-correct
    instead of a naive global timestamp compare.
    """

    commit: str | None = None
    branch: str | None = None
    worktree_hash: str | None = None  # set for uncommitted/stash-based beliefs


class StalenessBasis(str, Enum):
    """How a staleness verdict was reached — grounded vs heuristic is the honesty axis."""

    ANCESTRY = "ancestry"              # grounded: change reachable as a descendant of the belief commit
    WORKTREE = "worktree"             # grounded: working-tree content hash diverged
    DATE_HEURISTIC = "date_heuristic"  # UNGROUNDED fallback: timestamp only, no commit context
    NONE = "none"


@dataclass(frozen=True)
class StalenessVerdict:
    """Whether a belief is git-stale, how it was decided, and the derived evidence."""

    is_stale: bool
    basis: StalenessBasis = StalenessBasis.NONE
    grounded: bool = False  # True only for ANCESTRY / WORKTREE (extraction-independent ground truth)
    stale_anchors: tuple[str, ...] = ()
    grounding_changes: tuple[GitChange, ...] = ()
    wrong_branch_changes: tuple[GitChange, ...] = ()  # anchor changes off the belief's lineage (skipped)
    evidence: tuple[BeliefEvidence, ...] = ()


def _build_verdict(basis: StalenessBasis, grounded: bool, grounding: list[GitChange],
                   wrong_branch: list[GitChange]) -> StalenessVerdict:
    if not grounding:
        return StalenessVerdict(is_stale=False, basis=StalenessBasis.NONE, grounded=False,
                                wrong_branch_changes=tuple(wrong_branch))
    stale_anchors = tuple(sorted({c.target for c in grounding}))
    note_kind = "git" if grounded else "git(heuristic)"
    evidence: list[BeliefEvidence] = [
        BeliefEvidence(
            EvidenceSignal.LATER_CONTRADICTED,
            EvidencePolarity.CONTRADICTS,
            note=f"{note_kind}: {len(grounding)} change(s) to {', '.join(stale_anchors)} after the belief",
        )
    ]
    for change in grounding:
        signal = _KIND_SIGNAL.get(change.kind, EvidenceSignal.FILE_CHANGED)
        ref = f"{change.target}@{change.commit}" if change.commit else change.target
        evidence.append(
            BeliefEvidence(signal, EvidencePolarity.CONTRADICTS, note=f"git change: {ref} ({change.changed_at})")
        )
    return StalenessVerdict(
        is_stale=True, basis=basis, grounded=grounded, stale_anchors=stale_anchors,
        grounding_changes=tuple(grounding), wrong_branch_changes=tuple(wrong_branch), evidence=tuple(evidence),
    )


def derive_structural_staleness(
    *,
    anchors: list[str],
    believed_at: str | None = None,
    changes: list[GitChange],
    belief_context: BeliefCommitContext | None = None,
) -> StalenessVerdict:
    """Decide whether a code belief is stale because an anchor changed after it was formed.

    Three modes, in precedence order:

    1. **Worktree/stash** (``belief_context.worktree_hash`` set): the belief has no commit;
       it is stale iff a change's post-change ``worktree_hash`` for an anchor differs from
       the belief's snapshot hash. Branch/date are irrelevant. (basis=WORKTREE, grounded.)
    2. **Commit ancestry** (``belief_context.commit`` set): a change invalidates the belief
       ONLY if the belief's commit is in the change's ``descends_from`` set — i.e. the change
       is in the belief's line of history. A change on a divergent branch (belief commit not
       an ancestor) is recorded as ``wrong_branch_changes`` and does NOT mark the belief
       stale. This is the cross-branch correctness fix — dates are not used. (basis=ANCESTRY.)
    3. **Date heuristic** (no ``belief_context``): fall back to "anchor changed strictly after
       ``believed_at``". UNGROUNDED (grounded=False) — a global timestamp compare cannot tell
       branches apart; use only when commit context is unavailable. (basis=DATE_HEURISTIC.)

    Emits LATER_CONTRADICTED (CONTRADICTS) so the currentness policy treats the belief as
    superseded, plus a structural signal per grounding change for provenance.
    """
    anchor_set = set(anchors)
    if not anchor_set:
        return StalenessVerdict(is_stale=False)
    anchor_changes = [c for c in changes if c.matches(anchor_set)]  # rename-following

    # Mode 1: working-tree / stash belief — compare content hashes, ignore branch/date.
    if belief_context is not None and belief_context.worktree_hash is not None:
        grounding = [
            c for c in anchor_changes
            if c.worktree_hash is not None and c.worktree_hash != belief_context.worktree_hash
        ]
        return _build_verdict(StalenessBasis.WORKTREE, True, grounding, [])

    # Mode 2: commit belief — ancestry/reachability, NOT dates.
    if belief_context is not None and belief_context.commit is not None:
        commit = belief_context.commit
        grounding: list[GitChange] = []
        wrong_branch: list[GitChange] = []
        for c in anchor_changes:
            if commit in c.descends_from:
                grounding.append(c)            # the change is in the belief's future on this line
            else:
                wrong_branch.append(c)          # divergent branch / unreachable -> not invalidating
        return _build_verdict(StalenessBasis.ANCESTRY, True, grounding, wrong_branch)

    # Mode 3: no commit context — ungrounded date heuristic (fail-safe without a belief time).
    pivot = _parse(believed_at)
    if pivot is None:
        return StalenessVerdict(is_stale=False)
    grounding = [c for c in anchor_changes if (_parse(c.changed_at) or pivot) > pivot]
    return _build_verdict(StalenessBasis.DATE_HEURISTIC, False, grounding, [])
