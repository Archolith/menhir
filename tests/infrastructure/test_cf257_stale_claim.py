"""CF-257 -- a settled identity can go stale between settlement and the write.

The counterexample, exactly as the fourth-pass review reproduced it:

    1. transfer X succeeds
    2. transfer Y supersedes X
    3. X's delayed scan resumes
    4. the shared writer accepts it, because X's ``project_id`` is non-null

A populated field is not an authorisation. The scan in hand describes a directory that changed
hands while it was being produced, and writing it lands another project's files in X's silo --
with the per-project stale prune deleting whatever X does not have.

So a settled scan carries a CLAIM -- identity, directory, generation -- and the shared write
boundary re-validates all three inside the statement that admits the writer. The mirror-image
protection is in ``_transfer``: a transfer is refused while a writer is registered against any
identity it would invalidate, so the race cannot be won from the other side either.

The offline lane pins the protocol and the zero-write outcome. The lock that makes it hold under
real concurrency is Neo4j's, and is pinned in ``test_cf257_identity_binding_online.py``.
"""

from __future__ import annotations

import pytest

from menhir.infrastructure.project_identity_binding import (
    IdentityRootContested,
    bind_project_identity,
    root_key_for,
)
from menhir.infrastructure.structure_write_fence import (
    IdentityClaim,
    StaleIdentityClaim,
    admit_structure_writer,
    release_structure_writer,
)

ROOT = "/srv/proj"
HOST = "h1"


@pytest.fixture
def graph(fake_identity_graph, monkeypatch):
    monkeypatch.setattr(
        "menhir.infrastructure.project_identity_binding._host", lambda: HOST
    )
    return fake_identity_graph


def _claim(binding, root=ROOT):
    return IdentityClaim(
        project_id=binding.project_id,
        root_key=root_key_for(root),
        generation=binding.claim_generation,
        host=HOST,
    )


class _Structure:
    """Stands in for the structure repository, and counts what actually reached it."""

    def __init__(self):
        self.writes: list[str] = []

    def write_project(self, scan, session_id, user_id):
        self.writes.append(getattr(scan, "name", ""))
        return {"entities": 1, "edges": 0}


class _Scan:
    def __init__(self, project_id, generation, root=ROOT, name="proj"):
        self.name = name
        self.root_path = root
        self.project_id = project_id
        self.identity_generation = generation


def _adapter(graph, structure):
    """A real MemoryGraphAdapter with its collaborators stubbed.

    The adapter itself is NOT stubbed: the guard under test lives in its
    ``write_project_structure``, and a test that reimplemented that method would be asserting
    against its own copy of the logic.
    """
    from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

    adapter = MemoryGraphAdapter.__new__(MemoryGraphAdapter)
    adapter.neo4j = graph
    adapter._structure = structure
    return adapter


# ---------------------------------------------------------------------------
# The counterexample
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_scan_settled_before_a_transfer_writes_nothing_after_it(graph):
    """Pause X before its write, complete Y, resume X: X is refused and writes zero rows."""
    x = bind_project_identity(graph, project_id="id-x", root_path=ROOT)
    scan = _Scan(x.project_id, x.claim_generation)  # X settles, then "scans for minutes"

    bind_project_identity(graph, project_id="id-y", root_path=ROOT, rebind=True)  # Y supersedes X

    structure = _Structure()
    adapter = _adapter(graph, structure)

    with pytest.raises(StaleIdentityClaim, match="no longer the active binding"):
        adapter.write_project_structure(scan, "s", "u")  # X resumes

    assert structure.writes == [], "a superseded identity reached the structure writer"


@pytest.mark.unit
def test_the_same_scan_writes_when_its_claim_is_still_current(graph):
    """The negative control. Without it the test above passes on a writer that refuses always."""
    x = bind_project_identity(graph, project_id="id-x", root_path=ROOT)
    scan = _Scan(x.project_id, x.claim_generation)

    structure = _Structure()
    _adapter(graph, structure).write_project_structure(scan, "s", "u")

    assert structure.writes == ["proj"]


@pytest.mark.unit
def test_a_non_null_id_alone_does_not_authorise_a_write(graph):
    """What the old check accepted. ``project_id`` is populated and the identity is genuinely the
    active binding again; only the generation records that the directory changed hands twice in
    between, and that the scan in hand predates both."""
    x = bind_project_identity(graph, project_id="id-x", root_path=ROOT)
    stale_generation = x.claim_generation
    bind_project_identity(graph, project_id="id-y", root_path=ROOT, rebind=True)
    bind_project_identity(graph, project_id="id-x", root_path=ROOT, rebind=True)  # back to X

    structure = _Structure()
    scan = _Scan("id-x", stale_generation)
    assert graph.nodes["id-x"]["state"] == "bound", "id-x IS the active binding again"

    with pytest.raises(StaleIdentityClaim):
        _adapter(graph, structure).write_project_structure(scan, "s", "u")
    assert structure.writes == []


@pytest.mark.unit
def test_a_claim_for_a_different_directory_is_refused(graph):
    """The claim must authorise the directory the SCAN describes, not merely exist."""
    x = bind_project_identity(graph, project_id="id-x", root_path=ROOT)
    structure = _Structure()
    scan = _Scan(x.project_id, x.claim_generation, root="/srv/somewhere-else")

    with pytest.raises(StaleIdentityClaim):
        _adapter(graph, structure).write_project_structure(scan, "s", "u")
    assert structure.writes == []


@pytest.mark.unit
def test_an_id_less_scan_is_still_refused_before_any_claim_is_built(graph):
    structure = _Structure()
    with pytest.raises(ValueError, match="no structure_project_id"):
        _adapter(graph, structure).write_project_structure(_Scan(None, 0), "s", "u")
    assert structure.writes == []


# ---------------------------------------------------------------------------
# The mirror image: the transfer cannot win the race either
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_a_transfer_is_refused_while_a_writer_holds_the_directory(graph):
    x = bind_project_identity(graph, project_id="id-x", root_path=ROOT)
    handle = admit_structure_writer(graph, label="proj", claim=_claim(x))

    with pytest.raises(IdentityRootContested, match="structure writer is registered"):
        bind_project_identity(graph, project_id="id-y", root_path=ROOT, rebind=True)

    assert graph.nodes["id-x"]["state"] == "bound", "the incumbent was disturbed by a refusal"
    assert "id-y" not in graph.nodes, "the refused transfer created its target anyway"

    release_structure_writer(graph, handle)
    bind_project_identity(graph, project_id="id-y", root_path=ROOT, rebind=True)
    assert graph.nodes["id-x"]["state"] == "superseded"


@pytest.mark.unit
def test_a_writer_also_blocks_moving_its_own_identity_elsewhere(graph):
    """The writer is registered against the identity, so the identity cannot be walked to a
    different directory underneath it either -- not only superseded in place."""
    x = bind_project_identity(graph, project_id="id-x", root_path=ROOT)
    handle = admit_structure_writer(graph, label="proj", claim=_claim(x))

    with pytest.raises(IdentityRootContested):
        bind_project_identity(graph, project_id="id-x", root_path="/srv/moved", rebind=True)

    release_structure_writer(graph, handle)


@pytest.mark.unit
def test_releasing_clears_the_slot_on_both_the_identity_and_the_fence(graph):
    """Half a release is a slow outage: an entry left on the identity blocks every future
    transfer of that directory, and one left on the fence blocks the migration drain."""
    x = bind_project_identity(graph, project_id="id-x", root_path=ROOT)
    handle = admit_structure_writer(graph, label="proj", claim=_claim(x))
    assert graph.nodes["id-x"]["active_writers"] and graph.fence_writers

    release_structure_writer(graph, handle)
    assert graph.nodes["id-x"]["active_writers"] == []
    assert graph.fence_writers == []


@pytest.mark.unit
def test_a_writer_must_present_a_claim_at_all(graph):
    with pytest.raises(StaleIdentityClaim, match="must present an identity claim"):
        admit_structure_writer(graph, label="proj")
