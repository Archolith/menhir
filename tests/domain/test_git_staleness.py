"""Tests for git-grounded structural staleness (R3 Rung 2 / Chronostratum moat).

Pins: a belief is stale iff an anchor changed AFTER it was formed; the derived evidence
bridges into the currentness policy (is_superseded_belief fires); fail-safe without a
belief time; change kind maps to the right structural signal."""

from __future__ import annotations

from menhir.domain.belief import EvidenceSignal, is_superseded_belief
from menhir.domain.git_staleness import (
    BeliefCommitContext,
    GitChange,
    StalenessBasis,
    derive_structural_staleness,
)


def test_anchor_changed_after_belief_is_stale() -> None:
    v = derive_structural_staleness(
        anchors=["LocalDataSourceService.java"],
        believed_at="2026-01-01",
        changes=[GitChange("LocalDataSourceService.java", "2026-02-01", kind="file", commit="c9940ae")],
    )
    assert v.is_stale is True
    assert v.stale_anchors == ("LocalDataSourceService.java",)
    assert len(v.grounding_changes) == 1


def test_change_before_belief_is_not_stale() -> None:
    v = derive_structural_staleness(
        anchors=["x.py"],
        believed_at="2026-03-01",
        changes=[GitChange("x.py", "2026-01-01")],
    )
    assert v.is_stale is False
    assert v.evidence == ()


def test_change_to_other_anchor_is_ignored() -> None:
    v = derive_structural_staleness(
        anchors=["a.py"],
        believed_at="2026-01-01",
        changes=[GitChange("b.py", "2026-02-01")],
    )
    assert v.is_stale is False


def test_no_belief_time_is_fail_safe_not_stale() -> None:
    v = derive_structural_staleness(
        anchors=["a.py"], believed_at=None,
        changes=[GitChange("a.py", "2026-02-01")],
    )
    assert v.is_stale is False


def test_derived_evidence_bridges_into_currentness_policy() -> None:
    """The whole point: git staleness must make the belief layer treat it as superseded."""
    v = derive_structural_staleness(
        anchors=["mapping"], believed_at="2026-01-01",
        changes=[GitChange("mapping", "2026-02-01", kind="symbol")],
    )
    assert is_superseded_belief(list(v.evidence)) is True   # LATER_CONTRADICTED present
    signals = {e.signal for e in v.evidence}
    assert EvidenceSignal.LATER_CONTRADICTED in signals
    assert EvidenceSignal.SYMBOL_CHANGED in signals          # provenance signal for a symbol change


def test_date_path_is_marked_ungrounded() -> None:
    """The no-context date heuristic must own up to being a guess."""
    v = derive_structural_staleness(
        anchors=["a.py"], believed_at="2026-01-01",
        changes=[GitChange("a.py", "2026-02-01")],
    )
    assert v.is_stale is True
    assert v.basis is StalenessBasis.DATE_HEURISTIC
    assert v.grounded is False


# --- cross-branch correctness (the date-only soundness fix) ----------------

def test_change_on_belief_lineage_marks_stale_via_ancestry() -> None:
    """A change whose commit descends from the belief's commit invalidates it — and the
    decision is ancestry, not date."""
    v = derive_structural_staleness(
        anchors=["mapping.py"],
        belief_context=BeliefCommitContext(commit="C0", branch="main"),
        changes=[GitChange("mapping.py", "2026-02-01", commit="C2", branch="main", descends_from=frozenset({"C0", "C1"}))],
    )
    assert v.is_stale is True
    assert v.basis is StalenessBasis.ANCESTRY and v.grounded is True


def test_later_change_on_divergent_branch_does_NOT_mark_stale() -> None:
    """The bug fix: a change with a LATER date on a different branch is not in the belief's
    lineage (belief commit not in its ancestry), so it must NOT invalidate the belief."""
    v = derive_structural_staleness(
        anchors=["mapping.py"],
        belief_context=BeliefCommitContext(commit="C0", branch="feature/x"),
        changes=[GitChange("mapping.py", "2999-01-01", commit="M9", branch="main", descends_from=frozenset({"A1", "A2"}))],
    )
    assert v.is_stale is False                       # date is later, but off-lineage
    assert len(v.wrong_branch_changes) == 1          # recorded as wrong-branch, not applied
    assert v.evidence == ()


def test_stash_uncommitted_belief_uses_content_hash() -> None:
    """A belief formed against uncommitted/stashed work has no commit; staleness is a
    working-tree content-hash divergence, independent of branch/date."""
    ctx = BeliefCommitContext(worktree_hash="hashA")
    # same content hash -> not stale even with a 'change' present
    same = derive_structural_staleness(
        anchors=["wt.py"], belief_context=ctx,
        changes=[GitChange("wt.py", "2026-02-01", worktree_hash="hashA")],
    )
    assert same.is_stale is False and same.basis.value == "none"
    # diverged content hash -> stale, grounded by worktree
    diff = derive_structural_staleness(
        anchors=["wt.py"], belief_context=ctx,
        changes=[GitChange("wt.py", "2026-02-01", worktree_hash="hashB")],
    )
    assert diff.is_stale is True and diff.basis is StalenessBasis.WORKTREE and diff.grounded is True


def test_rename_following_change_to_moved_code_still_matches_old_anchor() -> None:
    """Durable-identity: a belief anchored to the OLD path is still invalidated by a change
    that renamed/moved that code (otherwise the renamed code is hidden from the anchor)."""
    v = derive_structural_staleness(
        anchors=["old/path/Service.java"],
        belief_context=BeliefCommitContext(commit="C0"),
        changes=[GitChange("new/path/Service.java", "2026-02-01", commit="C1",
                           descends_from=frozenset({"C0"}), renamed_from="old/path/Service.java")],
    )
    assert v.is_stale is True
    assert v.basis is StalenessBasis.ANCESTRY


def test_kind_maps_to_signal() -> None:
    for kind, signal in (("file", EvidenceSignal.FILE_CHANGED),
                         ("symbol", EvidenceSignal.SYMBOL_CHANGED),
                         ("dependency", EvidenceSignal.DEPENDENCY_CHANGED)):
        v = derive_structural_staleness(
            anchors=["t"], believed_at="2026-01-01",
            changes=[GitChange("t", "2026-02-01", kind=kind)],
        )
        assert signal in {e.signal for e in v.evidence}
