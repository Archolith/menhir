"""CF-257 phase 1 -- the identity file, its binding, and the resolution protocol.

Three separable guarantees:

* **the file** cannot be minted into a trackable location, cannot be clobbered by a racing
  scanner, and is never repaired by overwriting;
* **the binding** refuses one id presented from two directories, and refuses it for BOTH roots;
* **resolution** never mints silently, and hands a one-shot caller something it can act on.
"""

from __future__ import annotations

import json
import multiprocessing
import os

import pytest

from menhir.domain.project_id_file import (
    IdentityFileNotIgnored,
    MalformedIdentityFile,
    ProjectIdFileError,
    ensure_ignore_rule,
    identity_path,
    is_ignore_rule_present,
    mint_identity,
    read_identity,
)
from menhir.domain.project_identity_resolution import (
    IdentityAction,
    IdentityCandidate,
    ResolutionStatus,
    resolve_identity,
)
from menhir.infrastructure.project_identity_binding import (
    IdentityBindingConflict,
    IdentityRootContested,
    bind_project_identity,
    clear_conflict,
    read_binding,
)


def _hold_publication_lock(root: str, entered, release) -> None:
    from menhir.domain.project_id_file import identity_publication_lock

    with identity_publication_lock(root):
        entered.set()
        if not release.wait(10):
            raise TimeoutError("publication-lock test was never released")


def _enter_publication_lock(root: str, attempting, entered) -> None:
    from menhir.domain.project_id_file import identity_publication_lock

    attempting.set()
    with identity_publication_lock(root):
        entered.set()


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_minting_refuses_until_the_ignore_rule_exists(tmp_path):
    """THE PRECONDITION. 0 of 138 repos here would ignore this file today.

    An untracked identity file eventually gets committed, and a committed id is inherited by every
    clone and fork -- which is precisely the case the gitignored design exists to separate.
    """
    with pytest.raises(IdentityFileNotIgnored, match="Refusing to mint"):
        mint_identity(tmp_path)
    assert not identity_path(tmp_path).exists()


@pytest.mark.unit
def test_the_ignore_rule_is_created_and_is_idempotent(tmp_path):
    assert ensure_ignore_rule(tmp_path) is True
    assert is_ignore_rule_present(tmp_path)
    assert ensure_ignore_rule(tmp_path) is False  # second call changes nothing


@pytest.mark.unit
def test_the_ignore_rule_appends_and_preserves_existing_entries(tmp_path):
    """`.agent/.gitignore` is the repo's file, not menhir's -- this one already carries
    test_tmp/, mcp_telemetry.db and *.log."""
    gi = tmp_path / ".agent" / ".gitignore"
    gi.parent.mkdir(parents=True)
    gi.write_text("test_tmp/\n*.log\n", encoding="utf-8")
    ensure_ignore_rule(tmp_path)
    body = gi.read_text(encoding="utf-8")
    assert "test_tmp/" in body and "*.log" in body and "project-id" in body


@pytest.mark.unit
def test_minting_writes_a_readable_identity(tmp_path):
    ensure_ignore_rule(tmp_path)
    minted = mint_identity(tmp_path, display_name="proj")
    again = read_identity(tmp_path)
    assert again is not None
    assert again.project_id == minted.project_id
    assert again.display_name == "proj"


@pytest.mark.unit
def test_a_second_mint_cannot_clobber_the_first(tmp_path):
    """THE RACE. `os.replace` over a published file SUCCEEDS on this platform, so temp+rename
    would let two concurrent scanners each publish and the last one win -- two ids for one
    directory, no error. O_EXCL on the destination makes the loser fail loudly."""
    ensure_ignore_rule(tmp_path)
    first = mint_identity(tmp_path)
    with pytest.raises(FileExistsError):
        mint_identity(tmp_path)
    assert read_identity(tmp_path).project_id == first.project_id


@pytest.mark.unit
def test_a_malformed_file_refuses_rather_than_re_minting(tmp_path):
    """A corrupt file may be the only record of an id whose project holds thousands of entities."""
    ensure_ignore_rule(tmp_path)
    path = identity_path(tmp_path)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(MalformedIdentityFile, match="Refusing to overwrite"):
        read_identity(tmp_path)


@pytest.mark.unit
def test_a_file_without_a_project_id_is_malformed(tmp_path):
    ensure_ignore_rule(tmp_path)
    identity_path(tmp_path).write_text(json.dumps({"schema": 1}), encoding="utf-8")
    with pytest.raises(MalformedIdentityFile):
        read_identity(tmp_path)


@pytest.mark.unit
def test_a_missing_file_is_not_an_error(tmp_path):
    assert read_identity(tmp_path) is None


@pytest.mark.unit
def test_adopting_writes_the_supplied_id(tmp_path):
    ensure_ignore_rule(tmp_path)
    minted = mint_identity(tmp_path, project_id="adopted-id-123")
    assert minted.project_id == "adopted-id-123"
    assert read_identity(tmp_path).project_id == "adopted-id-123"


@pytest.mark.unit
def test_an_unwritable_location_reports_rather_than_half_minting(tmp_path, monkeypatch):
    ensure_ignore_rule(tmp_path)

    def _deny(*a, **k):
        raise PermissionError("read-only checkout")

    monkeypatch.setattr(os, "open", _deny)
    with pytest.raises(ProjectIdFileError, match="read-only"):
        mint_identity(tmp_path)


@pytest.mark.unit
def test_publication_can_recheck_ignore_rule_while_holding_the_windows_lock(tmp_path):
    from menhir.domain.project_id_file import identity_publication_lock
    from menhir.services.project_identity_service import _publish_identity_file

    with identity_publication_lock(tmp_path):
        assert ensure_ignore_rule(tmp_path) is False
        _publish_identity_file(
            str(tmp_path),
            project_id="locked-publication",
            display_name="proj",
            existing=None,
        )

    assert read_identity(tmp_path).project_id == "locked-publication"


@pytest.mark.unit
def test_publication_lock_serializes_separate_processes(tmp_path):
    """The thread mutex alone cannot prevent a delayed process from publishing stale state."""
    ensure_ignore_rule(tmp_path)
    context = multiprocessing.get_context("spawn")
    first_entered = context.Event()
    release_first = context.Event()
    second_attempting = context.Event()
    second_entered = context.Event()
    first = context.Process(
        target=_hold_publication_lock,
        args=(str(tmp_path), first_entered, release_first),
    )
    second = context.Process(
        target=_enter_publication_lock,
        args=(str(tmp_path), second_attempting, second_entered),
    )

    first.start()
    try:
        assert first_entered.wait(5), "first process never acquired the publication lock"
        second.start()
        assert second_attempting.wait(5), "second process never attempted the publication lock"
        assert not second_entered.wait(0.5), "second process bypassed the publication lock"
        release_first.set()
        assert second_entered.wait(5), "second process did not acquire after release"
    finally:
        release_first.set()
        first.join(5)
        if second.pid is not None:
            second.join(5)
        if first.is_alive():
            first.terminate()
        if second.pid is not None and second.is_alive():
            second.terminate()

    assert first.exitcode == 0
    assert second.exitcode == 0


# ---------------------------------------------------------------------------
# The binding
# ---------------------------------------------------------------------------

# The binding protocol is exercised against `fake_identity_graph` (tests/infrastructure/
# conftest.py), which enforces the `(bound_host, root_key)` uniqueness constraint rather than
# merely storing rows. The authority for the real constraint semantics is
# `test_cf257_identity_binding_online.py`.

@pytest.fixture
def neo4j(fake_identity_graph):
    return fake_identity_graph


@pytest.mark.unit
def test_a_first_binding_is_accepted(neo4j):
    state = bind_project_identity(neo4j, project_id="id-1", root_path="C:/repos/proj")
    assert state.state == "bound"


@pytest.mark.unit
def test_rebinding_the_same_root_is_accepted(neo4j):
    bind_project_identity(neo4j, project_id="id-1", root_path="C:/repos/proj")
    bind_project_identity(neo4j, project_id="id-1", root_path="C:/repos/proj/")  # trailing sep
    bind_project_identity(neo4j, project_id="id-1", root_path=r"C:\repos\proj")  # separators
    assert read_binding(neo4j, "id-1").state == "bound"


@pytest.mark.unit
def test_one_id_from_two_directories_is_refused(neo4j):
    """The copied-tree case the composite key constraint cannot see: identical paths simply MERGE
    onto the same nodes, so the graph looks consistent while two roots share one silo."""
    bind_project_identity(neo4j, project_id="id-1", root_path="C:/repos/proj")
    with pytest.raises(IdentityBindingConflict, match="bound to"):
        bind_project_identity(neo4j, project_id="id-1", root_path="C:/copies/proj")


@pytest.mark.unit
def test_a_conflict_disables_the_identity_for_the_INCUMBENT_too(neo4j):
    """The load-bearing half. Refusing only the newcomer leaves the already-bound directory
    writing into a silo now known to be ambiguous, with no signal until something else breaks."""
    bind_project_identity(neo4j, project_id="id-1", root_path="C:/repos/proj")
    with pytest.raises(IdentityBindingConflict):
        bind_project_identity(neo4j, project_id="id-1", root_path="C:/copies/proj")

    with pytest.raises(IdentityBindingConflict, match="CONFLICTED"):
        bind_project_identity(neo4j, project_id="id-1", root_path="C:/repos/proj")


@pytest.mark.unit
def test_an_operator_resolves_a_conflict_by_naming_the_root_to_keep(neo4j):
    """No 'just clear it': the conflicted state exists because the system cannot tell which
    directory is real, so resolution has to say."""
    bind_project_identity(neo4j, project_id="id-1", root_path="C:/repos/proj")
    with pytest.raises(IdentityBindingConflict):
        bind_project_identity(neo4j, project_id="id-1", root_path="C:/copies/proj")
    clear_conflict(neo4j, project_id="id-1", keep_root_path="C:/repos/proj")
    assert bind_project_identity(
        neo4j, project_id="id-1", root_path="C:/repos/proj"
    ).state == "bound"


@pytest.mark.unit
def test_an_ordinary_transfer_cannot_clear_a_conflict(neo4j):
    bind_project_identity(neo4j, project_id="id-1", root_path="C:/repos/proj")
    with pytest.raises(IdentityBindingConflict):
        bind_project_identity(neo4j, project_id="id-1", root_path="C:/copies/proj")

    with pytest.raises(IdentityBindingConflict, match="CONFLICTED"):
        bind_project_identity(
            neo4j,
            project_id="id-1",
            root_path="C:/repos/proj",
            rebind=True,
        )
    assert read_binding(neo4j, "id-1").state == "conflicted"


@pytest.mark.unit
def test_operator_adopt_can_atomically_claim_the_kept_conflicted_root(neo4j):
    bind_project_identity(neo4j, project_id="id-1", root_path="C:/repos/proj")
    with pytest.raises(IdentityBindingConflict):
        bind_project_identity(neo4j, project_id="id-1", root_path="C:/copies/proj")

    state = bind_project_identity(
        neo4j,
        project_id="id-1",
        root_path="C:/repos/proj",
        rebind=True,
        resolve_conflict=True,
    )
    assert state.state == "bound"
    assert read_binding(neo4j, "id-1").state == "bound"


@pytest.mark.unit
def test_a_failed_conflict_resolution_leaves_the_identity_conflicted(neo4j):
    bind_project_identity(neo4j, project_id="id-1", root_path="C:/repos/proj")
    with pytest.raises(IdentityBindingConflict):
        bind_project_identity(neo4j, project_id="id-1", root_path="C:/copies/proj")
    neo4j.nodes["id-1"]["active_writers"] = ["writer"]

    with pytest.raises(IdentityRootContested, match="Nothing was changed"):
        clear_conflict(neo4j, project_id="id-1", keep_root_path="C:/repos/proj")

    assert read_binding(neo4j, "id-1").state == "conflicted"


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def _candidate(**kw):
    base = dict(project_id="id-1", display_name="proj", entity_count=15636,
                last_scan="2026-08-19T10:00:00Z", recorded_root_path="C:/repos/proj")
    base.update(kw)
    return IdentityCandidate(**base)


@pytest.mark.unit
def test_an_existing_file_resolves_directly():
    r = resolve_identity(root_path="C:/repos/proj", existing_file_id="id-9", candidate=None)
    assert r.resolved and r.project_id == "id-9"


@pytest.mark.unit
def test_a_missing_file_with_a_candidate_needs_a_decision():
    """NEVER an automatic mint: the file is gitignored, so a fresh clone or a git clean removes
    it, and minting silently would orphan the project's entire silo."""
    r = resolve_identity(root_path="C:/repos/proj", existing_file_id=None, candidate=_candidate())
    assert r.status is ResolutionStatus.NEEDS_DECISION
    assert r.reason == "identity_file_missing"
    assert r.candidates[0].entity_count == 15636


@pytest.mark.unit
def test_a_missing_file_with_no_candidate_still_needs_a_decision():
    """A moved repo, a replacement machine and a fresh clone all land here. Revision 1 claimed
    recovery worked by root_path equality, which only ever covered deletion IN PLACE."""
    r = resolve_identity(root_path="/srv/new/proj", existing_file_id=None, candidate=None)
    assert r.status is ResolutionStatus.NEEDS_DECISION
    assert r.reason == "identity_file_missing_no_candidate"
    assert r.candidates == []


@pytest.mark.unit
def test_the_payload_tells_a_one_shot_caller_how_to_retry():
    """MCP and HTTP callers have no interactive channel, so the answer has to be in the value."""
    payload = resolve_identity(
        root_path="C:/repos/proj", existing_file_id=None, candidate=_candidate()
    ).as_dict()
    assert payload["status"] == "needs_decision"
    assert payload["retry_with"]["identity_action"] == "adopt|new"
    assert "adopt_project_id" in payload["retry_with"]
    cand = payload["candidates"][0]
    assert cand["entity_count"] == 15636 and cand["last_scan"]


@pytest.mark.unit
def test_adopt_resolves_to_the_named_id():
    r = resolve_identity(root_path="C:/repos/proj", existing_file_id=None,
                         candidate=_candidate(), action=IdentityAction.ADOPT,
                         adopt_project_id="id-1")
    assert r.resolved and r.project_id == "id-1"


@pytest.mark.unit
def test_adopt_without_an_id_is_not_a_silent_mint():
    r = resolve_identity(root_path="C:/repos/proj", existing_file_id=None,
                         candidate=_candidate(), action=IdentityAction.ADOPT)
    assert r.status is ResolutionStatus.NEEDS_DECISION
    assert r.reason == "adopt_requires_project_id"


@pytest.mark.unit
def test_new_resolves_to_a_fresh_mint():
    """The intended outcome for a genuinely new working copy, including a deliberate second
    checkout on another machine -- which the gitignored design makes a separate project."""
    r = resolve_identity(root_path="/srv/new/proj", existing_file_id=None,
                         candidate=_candidate(), action=IdentityAction.NEW)
    assert r.resolved and r.project_id is None  # None = mint one
