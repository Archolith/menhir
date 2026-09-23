"""CF-257 phase 1 -- the legacy identity file reader, the binding, and the resolution protocol.

Three separable guarantees:

* **the legacy file** is only read (Menhir never writes one); an unusable file is reported, never
  treated as an identity;
* **the binding** refuses one id presented from two directories, and refuses it for BOTH roots;
* **resolution** never mints silently, and hands a one-shot caller something it can act on.
"""

from __future__ import annotations

import json

import pytest

from menhir.domain.project_id_file import (
    MalformedIdentityFile,
    identity_path,
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


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_malformed_file_is_reported_not_read_as_an_identity(tmp_path):
    """A corrupt file may be the only record of an id whose project holds thousands of entities."""
    identity_path(tmp_path).parent.mkdir(parents=True)
    path = identity_path(tmp_path)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(MalformedIdentityFile, match="could not be read as JSON"):
        read_identity(tmp_path)


@pytest.mark.unit
def test_a_file_without_a_project_id_is_malformed(tmp_path):
    identity_path(tmp_path).parent.mkdir(parents=True)
    identity_path(tmp_path).write_text(json.dumps({"schema": 1}), encoding="utf-8")
    with pytest.raises(MalformedIdentityFile):
        read_identity(tmp_path)


@pytest.mark.unit
def test_a_missing_file_is_not_an_error(tmp_path):
    assert read_identity(tmp_path) is None


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
def test_a_verified_binding_resolves_directly():
    r = resolve_identity(root_path="C:/repos/proj", verified_project_id="id-9", candidates=[])
    assert r.resolved and r.project_id == "id-9"


@pytest.mark.unit
def test_an_unverified_directory_with_a_candidate_needs_a_decision():
    """NEVER an automatic mint: minting silently would orphan the project's entire silo."""
    r = resolve_identity(root_path="C:/repos/proj", verified_project_id=None,
                         candidates=[_candidate()])
    assert r.status is ResolutionStatus.NEEDS_DECISION
    assert r.reason == "directory_not_bound"
    assert r.candidates[0].entity_count == 15636


@pytest.mark.unit
def test_an_unverified_directory_with_no_candidate_still_needs_a_decision():
    """A moved repo, a replacement machine and a fresh clone all land here. Revision 1 claimed
    recovery worked by root_path equality, which only ever covered deletion IN PLACE."""
    r = resolve_identity(root_path="/srv/new/proj", verified_project_id=None, candidates=[])
    assert r.status is ResolutionStatus.NEEDS_DECISION
    assert r.reason == "directory_not_bound_no_candidate"
    assert r.candidates == []


@pytest.mark.unit
def test_the_payload_tells_a_one_shot_caller_how_to_retry():
    """MCP and HTTP callers have no interactive channel, so the answer has to be in the value."""
    payload = resolve_identity(
        root_path="C:/repos/proj", verified_project_id=None, candidates=[_candidate()]
    ).as_dict()
    assert payload["status"] == "needs_decision"
    assert payload["retry_with"]["identity_action"] == "adopt|new"
    assert "adopt_project_id" in payload["retry_with"]
    cand = payload["candidates"][0]
    assert cand["entity_count"] == 15636 and cand["last_scan"]


@pytest.mark.unit
def test_adopt_resolves_to_the_named_id():
    r = resolve_identity(root_path="C:/repos/proj", verified_project_id=None,
                         candidates=[_candidate()], action=IdentityAction.ADOPT,
                         adopt_project_id="id-1")
    assert r.resolved and r.project_id == "id-1"


@pytest.mark.unit
def test_adopt_without_an_id_is_not_a_silent_mint():
    r = resolve_identity(root_path="C:/repos/proj", verified_project_id=None,
                         candidates=[_candidate()], action=IdentityAction.ADOPT)
    assert r.status is ResolutionStatus.NEEDS_DECISION
    assert r.reason == "adopt_requires_project_id"


@pytest.mark.unit
def test_new_resolves_to_a_fresh_mint():
    """The intended outcome for a genuinely new working copy, including a deliberate second
    checkout on another machine -- which the gitignored design makes a separate project."""
    r = resolve_identity(root_path="/srv/new/proj", verified_project_id=None,
                         candidates=[_candidate()], action=IdentityAction.NEW)
    assert r.resolved and r.project_id is None  # None = mint one
