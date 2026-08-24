"""CF-257 phase 0 -- a scan root may not write under another checkout's identity.

Project identity is a directory basename (`project_name = name or root.name`), so two directories
sharing one basename are one project: the writers are `MERGE ... SET` and the fingerprint is looked
up by the same name, so a scan from the wrong copy overwrites the canonical rows AND runs the
per-project stale prune, deleting the files that copy does not have.

Two refusals, and neither covers the other:

* a WORKTREE or SUBMODULE is refused on filesystem shape alone -- catches a first-ever scan;
* a FORK is an independent clone that passes every git check, and is caught only by the project
  already recording a different `root_path` -- needs graph state, cannot catch a first scan.

**These tests build real directory shapes rather than mocking, because the predicate is a
filesystem fact.** They must also never shell out to git: on this workspace `git rev-parse` fails
two different ways -- `dubious ownership` on live worktrees under a different user, and `not a git
repository` on the 7-of-81 worktrees whose gitdir no longer exists. A test that invoked git would
be asserting the local git configuration, not the code.
"""

from __future__ import annotations

import pytest

from menhir.domain.project_identity import (
    ProjectIdentityRefused,
    ensure_scan_root_owns_identity,
)
from menhir.infrastructure.repo_topology import RootKind, classify_root


# ---------------------------------------------------------------------------
# Real directory shapes
# ---------------------------------------------------------------------------

def _clone(tmp_path, name="canonical"):
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    return root


def _worktree(tmp_path, name="feature-branch", primary_name="canonical"):
    """A worktree: `.git` is a FILE pointing at <primary>/.git/worktrees/<name>."""
    primary = _clone(tmp_path, primary_name)
    gitdir = primary / ".git" / "worktrees" / name
    gitdir.mkdir(parents=True)
    # `commondir` is relative to the gitdir. `../..` is what this workspace's worktrees carry.
    (gitdir / "commondir").write_text("../..", encoding="utf-8")
    root = tmp_path / name
    root.mkdir()
    (root / ".git").write_text(f"gitdir: {gitdir}", encoding="utf-8")
    return root, primary


def _submodule(tmp_path, name="vendored"):
    """A submodule: `.git` is a FILE pointing into <super>/.git/modules/<name>."""
    super_repo = _clone(tmp_path, "superproject")
    gitdir = super_repo / ".git" / "modules" / name
    gitdir.mkdir(parents=True)
    root = tmp_path / name
    root.mkdir()
    (root / ".git").write_text(f"gitdir: {gitdir}", encoding="utf-8")
    return root


def _plain(tmp_path, name="just-a-directory"):
    root = tmp_path / name
    root.mkdir()
    return root


# ---------------------------------------------------------------------------
# classify_root
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_an_independent_clone_may_scan(tmp_path):
    t = classify_root(_clone(tmp_path))
    assert t.kind is RootKind.CLONE
    assert t.may_scan


@pytest.mark.unit
def test_a_plain_directory_may_scan(tmp_path):
    """THE REGRESSION the reviewer named.

    `_is_independent_clone` is `isdir(".git")`, which is false for a non-git directory too, so
    reusing it on the scan root would refuse ordinary project directories. Many scan targets are
    not repositories.
    """
    t = classify_root(_plain(tmp_path))
    assert t.kind is RootKind.PLAIN
    assert t.may_scan


@pytest.mark.unit
def test_a_worktree_is_refused_and_names_its_primary(tmp_path):
    root, primary = _worktree(tmp_path)
    t = classify_root(root)
    assert t.kind is RootKind.WORKTREE
    assert not t.may_scan
    assert t.primary_worktree == primary.resolve()


@pytest.mark.unit
def test_a_submodule_is_refused_and_names_no_primary(tmp_path):
    """A submodule has no alternate primary checkout, so naming one would be a fabrication."""
    t = classify_root(_submodule(tmp_path))
    assert t.kind is RootKind.SUBMODULE
    assert not t.may_scan
    assert t.primary_worktree is None


@pytest.mark.unit
def test_a_stale_gitdir_fails_closed(tmp_path):
    """7 of 81 worktrees on this workspace are in this state -- the checkout outlived its repo.

    `git` itself reports `not a git repository` here, which is why resolution reads files.
    """
    root, primary = _worktree(tmp_path)
    import shutil
    shutil.rmtree(primary / ".git" / "worktrees")
    t = classify_root(root)
    assert t.kind is RootKind.UNRESOLVABLE
    assert not t.may_scan


@pytest.mark.unit
def test_a_stale_gitdir_says_so_rather_than_blaming_commondir(tmp_path):
    """Pins WHY the stale case is refused, not just that it is.

    Mutation M7 (deleting the `gitdir.is_dir()` check) left the previous test green: a missing
    gitdir also has no readable `commondir`, so the worktree branch reached UNRESOLVABLE by a
    different route. Same verdict, different reason, and the reason is what the operator acts on
    -- "gitdir does not exist" means the repository is gone, while "no resolvable commondir" sends
    them looking at a file that was never the problem.
    """
    root, primary = _worktree(tmp_path)
    import shutil
    shutil.rmtree(primary / ".git" / "worktrees")
    t = classify_root(root)
    assert t.kind is RootKind.UNRESOLVABLE
    assert "gitdir does not exist" in t.detail
    assert t.gitdir is not None


@pytest.mark.unit
def test_a_submodule_pointer_with_a_missing_gitdir_is_unresolvable_not_submodule(tmp_path):
    """The stale-gitdir check's load-bearing case, which the worktree path masked.

    On the submodule branch there is no `commondir` to fail on, so without the existence check a
    dangling pointer is classified SUBMODULE with confident detail while nothing about it was
    actually verified. Both outcomes refuse the scan, but only one of them is honest about how
    much is known -- and the override CAN admit a submodule while it must never admit an
    unidentifiable root.
    """
    root = _submodule(tmp_path)
    import shutil
    shutil.rmtree(tmp_path / "superproject" / ".git" / "modules")
    t = classify_root(root)
    assert t.kind is RootKind.UNRESOLVABLE
    with pytest.raises(ProjectIdentityRefused, match="not overridable"):
        _guard(t, recorded=None, tier="operator", force=True)


@pytest.mark.unit
def test_a_gitdir_without_commondir_fails_closed(tmp_path):
    root, primary = _worktree(tmp_path)
    (primary / ".git" / "worktrees" / "feature-branch" / "commondir").unlink()
    assert classify_root(root).kind is RootKind.UNRESOLVABLE


@pytest.mark.unit
def test_an_unrecognised_gitdir_layout_fails_closed(tmp_path):
    """Refusing an unknown shape is the point: assuming 'clone' is the destructive guess."""
    other = tmp_path / "somewhere" / "else"
    other.mkdir(parents=True)
    root = tmp_path / "odd"
    root.mkdir()
    (root / ".git").write_text(f"gitdir: {other}", encoding="utf-8")
    assert classify_root(root).kind is RootKind.UNRESOLVABLE


@pytest.mark.unit
def test_a_git_file_with_no_pointer_fails_closed(tmp_path):
    root = tmp_path / "odd"
    root.mkdir()
    (root / ".git").write_text("this is not a gitdir pointer", encoding="utf-8")
    assert classify_root(root).kind is RootKind.UNRESOLVABLE


# ---------------------------------------------------------------------------
# ensure_scan_root_owns_identity
# ---------------------------------------------------------------------------

def _guard(topology, *, recorded=None, tier="agent", force=False, project="canonical"):
    ensure_scan_root_owns_identity(
        topology=topology, project_name=project, recorded_root_path=recorded,
        tier=tier, force=force,
    )


@pytest.mark.unit
def test_a_clone_claiming_an_unknown_project_is_allowed(tmp_path):
    """The first claimant is not the problem, so a name nothing has claimed must pass."""
    _guard(classify_root(_clone(tmp_path)), recorded=None)


@pytest.mark.unit
def test_a_clone_rescanning_its_own_recorded_path_is_allowed(tmp_path):
    root = _clone(tmp_path)
    _guard(classify_root(root), recorded=str(root))


@pytest.mark.unit
def test_path_comparison_tolerates_separator_and_case_differences(tmp_path):
    """A FALSE mismatch refuses a legitimate re-scan -- the failure this guard must not have.

    The graph holds Windows paths written by several clients, so `C:\\x\\y` and `C:/x/y` are the
    same directory and must not read as a collision.
    """
    root = _clone(tmp_path)
    _guard(classify_root(root), recorded=str(root).replace("/", "\\").upper())


@pytest.mark.unit
def test_a_fork_is_refused_because_the_name_is_already_recorded_elsewhere(tmp_path):
    """THE FORK CASE. An independent clone passes every git check.

    `projects/forked/yawn.frontend` against `projects/yawn/yawn.frontend`: same basename, real
    clone, different code. Only the recorded root_path can tell them apart.
    """
    fork = _clone(tmp_path / "forked", "yawn.frontend")
    with pytest.raises(ProjectIdentityRefused, match="already recorded at"):
        _guard(classify_root(fork), recorded=str(tmp_path / "yawn" / "yawn.frontend"),
               project="yawn.frontend")


@pytest.mark.unit
def test_a_worktree_is_refused_even_when_the_project_is_unknown(tmp_path):
    """Shape alone must refuse, or a first-ever scan from a worktree claims the name outright."""
    root, _ = _worktree(tmp_path)
    with pytest.raises(ProjectIdentityRefused, match="worktree"):
        _guard(classify_root(root), recorded=None)


@pytest.mark.unit
def test_the_operator_override_admits_a_worktree(tmp_path):
    root, _ = _worktree(tmp_path)
    _guard(classify_root(root), recorded=None, tier="operator", force=True)


@pytest.mark.unit
def test_the_operator_override_admits_a_recorded_path_mismatch(tmp_path):
    """This is the 'the project moved' path, which must remain possible."""
    root = _clone(tmp_path)
    _guard(classify_root(root), recorded=str(tmp_path / "somewhere-else"),
           tier="operator", force=True)


@pytest.mark.unit
def test_an_agent_tier_override_is_refused(tmp_path):
    """Writing across an identity boundary is not an agent-tier decision."""
    root, _ = _worktree(tmp_path)
    with pytest.raises(ProjectIdentityRefused, match="requires operator tier"):
        _guard(classify_root(root), recorded=None, tier="agent", force=True)


@pytest.mark.unit
def test_an_unconfigured_tier_may_override(tmp_path):
    """`None` tier means no API keys are configured (local dev); the auth model treats that as
    unrestricted everywhere else, and this guard must not be the one place it does not."""
    root, _ = _worktree(tmp_path)
    _guard(classify_root(root), recorded=None, tier=None, force=True)


@pytest.mark.unit
def test_the_override_cannot_admit_an_unresolvable_root(tmp_path):
    """'I know this is a second checkout and want it anyway' is coherent.

    'I know this pointer is broken and want it anyway' is not -- nothing downstream knows what
    identity was intended, so there is nothing to consent to.
    """
    root, primary = _worktree(tmp_path)
    import shutil
    shutil.rmtree(primary / ".git" / "worktrees")
    with pytest.raises(ProjectIdentityRefused, match="not overridable"):
        _guard(classify_root(root), recorded=None, tier="operator", force=True)


@pytest.mark.unit
def test_a_submodule_refusal_says_submodule_not_worktree(tmp_path):
    """The message drives the operator's next action, and they differ: a submodule is ingested
    directly as its own project, a worktree is scanned from its primary."""
    with pytest.raises(ProjectIdentityRefused, match="submodule"):
        _guard(classify_root(_submodule(tmp_path)), recorded=None)


# ---------------------------------------------------------------------------
# Review round 3: the two write-safety defects
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_posix_paths_differing_only_in_case_are_different_directories(tmp_path):
    """P1.2. `/srv/Foo` and `/srv/foo` are two directories on Linux.

    The first version case-folded every path, so the second compared equal to the first and was
    ADMITTED under its identity. That is the false-ALLOW direction: a false refusal annoys
    someone, a false allow prunes a project's files.

    Uses paths that do not exist locally on purpose -- this pins the textual fallback, which is
    what runs when the recorded root_path came from another host.
    """
    from menhir.domain.project_identity import _same_path
    assert not _same_path("/srv/Foo", "/srv/foo")
    assert _same_path("/srv/foo", "/srv/foo/")


@pytest.mark.unit
def test_windows_paths_still_compare_case_and_separator_insensitively():
    """The leniency that must survive the fix: one directory spelled two ways in the graph."""
    from menhir.domain.project_identity import _same_path
    assert _same_path(r"C:\Users\thron\proj", "c:/users/thron/proj")
    assert _same_path(r"C:\Users\thron\proj\\", "C:/Users/thron/proj")


@pytest.mark.unit
def test_two_spellings_of_one_real_directory_are_the_same_path(tmp_path):
    """`samefile` answers 'the same directory', not 'the same spelling', when both exist."""
    from menhir.domain.project_identity import _same_path
    real = _clone(tmp_path, "proj")
    assert _same_path(str(real), str(tmp_path / "." / "proj"))


@pytest.mark.unit
def test_a_root_absent_from_this_host_is_missing_not_plain(tmp_path):
    """P1.1 support. Reporting an unobservable root as 'not a git repository' would be a claim
    this process cannot make -- it would let a remotely-scanned worktree read as an ordinary
    directory."""
    t = classify_root(tmp_path / "not-here")
    assert t.kind is RootKind.MISSING
    assert not t.may_scan


@pytest.mark.unit
def test_an_unobservable_root_is_refused_by_default(tmp_path):
    """Silence is not approval: the default answer for a root we cannot see is no."""
    with pytest.raises(ProjectIdentityRefused, match="cannot see"):
        _guard(classify_root(tmp_path / "not-here"), recorded=None)


@pytest.mark.unit
def test_the_compatibility_path_may_accept_an_unobservable_root(tmp_path):
    """A remote client legitimately scans on its own machine and ships the result."""
    ensure_scan_root_owns_identity(
        topology=classify_root(tmp_path / "not-here"), project_name="remote-proj",
        recorded_root_path=None, tier="agent", allow_unobservable_root=True,
    )


@pytest.mark.unit
def test_the_compatibility_path_still_refuses_a_fork(tmp_path):
    """The concession is narrow: shape cannot be checked, IDENTITY still can.

    This is what stops the caller-supplied write from being a bypass -- an agent submitting a
    fork's structure under a canonical project's name is refused on the recorded root_path even
    though nothing about the directory is observable here.
    """
    with pytest.raises(ProjectIdentityRefused, match="already recorded at"):
        ensure_scan_root_owns_identity(
            topology=classify_root(tmp_path / "elsewhere" / "yawn.frontend"),
            project_name="yawn.frontend",
            recorded_root_path="/srv/projects/yawn/yawn.frontend",
            tier="agent", allow_unobservable_root=True,
        )


@pytest.mark.unit
def test_the_compatibility_path_still_refuses_an_observable_worktree(tmp_path):
    """`allow_unobservable_root` must relax ONLY the unobservable case.

    If the submitted root does exist here and is a worktree, the shape refusal is available and
    must still fire -- otherwise the flag would be a general bypass rather than a narrow one.
    """
    root, _ = _worktree(tmp_path)
    with pytest.raises(ProjectIdentityRefused, match="worktree"):
        ensure_scan_root_owns_identity(
            topology=classify_root(root), project_name="canonical",
            recorded_root_path=None, tier="agent", allow_unobservable_root=True,
        )
