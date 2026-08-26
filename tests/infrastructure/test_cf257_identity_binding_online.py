"""CF-257 -- the identity binding against a REAL Neo4j, with the real constraints.

These are the authority for three things the offline lane provably cannot decide:

1. **The constraint is the enforcement.** Offline, the fake re-states the uniqueness rule in
   Python. That proves the calling code respects a rule; it does not prove the database has one.
   Here the constraint is created from the same DDL the schema bootstrap ships.

2. **The Cypher means what it says.** The offline fake dispatches on a statement PREFIX, so it is
   blind to the predicates inside the query -- a mutation restoring
   ``coalesce(p.bound_host, $host) = $host`` (the bug that made host-scoping inert on all 60
   production rows) passes every offline test. It has to fail here or it is not tested at all.

3. **Concurrency.** Two transfers racing for one directory is not simulable against a dict.

Run with ``pytest --run-online``; conftest forces the test instance (:7688) and refuses to run
if that resolves to production.
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest

from menhir.infrastructure.project_identity_binding import (
    PROJECT_IDENTITY_CONSTRAINTS,
    IdentityBindingConflict,
    IdentityRootContested,
    bind_project_identity,
    binding_for_root,
    clear_conflict,
    root_key_for,
)
from menhir.infrastructure.structure_write_fence import (
    IdentityClaim,
    StaleIdentityClaim,
    admit_structure_writer,
    release_structure_writer,
)

pytestmark = [pytest.mark.online]

#: Iterations for the admission/transfer race. High enough that the rarer ordering is observed
#: on this instance (measured ~1 in 100), because a lost lock only shows up on that side.
_RACE_ITERATIONS = 150


class _Repo:
    """Minimal `execute` over a driver session, matching Neo4jRepository's surface."""

    def __init__(self, driver):
        self._driver = driver

    def execute(self, query, params=None, **_kw):
        with self._driver.session() as session:
            return [dict(r) for r in session.run(query, params or {})]


@pytest.fixture
def repo():
    import os

    from neo4j import GraphDatabase

    uri = os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://localhost:7688")
    user = os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j")
    password = os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    r = _Repo(driver)
    # The real DDL, not a copy of it: a test that creates its own constraint proves nothing about
    # the one the bootstrap ships.
    for statement in PROJECT_IDENTITY_CONSTRAINTS:
        r.execute(statement, {})
    try:
        yield r
    finally:
        r.execute(
            "MATCH (p:ProjectIdentity) WHERE p.project_id STARTS WITH 'cf257-test-' DETACH DELETE p",
            {},
        )
        driver.close()


@pytest.fixture
def pid():
    """Test ids are prefixed so cleanup can never touch a binding it did not create."""
    return lambda n: f"cf257-test-{n}-{uuid.uuid4().hex[:8]}"


def _active_for(repo, host, root_key):
    return sorted(
        row["id"]
        for row in repo.execute(
            "MATCH (p:ProjectIdentity) WHERE p.bound_host = $host AND p.root_key = $rk "
            "AND coalesce(p.state,'bound') = 'bound' RETURN p.project_id AS id",
            {"host": host, "rk": root_key},
        )
    )


@pytest.fixture
def on_host(monkeypatch):
    def _set(host):
        monkeypatch.setattr(
            "menhir.infrastructure.project_identity_binding._host", lambda: host
        )

    return _set


@pytest.mark.online
def test_the_constraint_itself_rejects_a_second_active_claim(repo, pid, on_host):
    """The property, asserted at the database rather than at the caller."""
    host = f"cf257-host-{uuid.uuid4().hex[:6]}"
    root = f"/srv/{uuid.uuid4().hex[:8]}"
    a, b = pid("a"), pid("b")
    on_host(host)

    bind_project_identity(repo, project_id=a, root_path=root)
    with pytest.raises(IdentityRootContested):
        bind_project_identity(repo, project_id=b, root_path=root)

    assert _active_for(repo, host, root) == [a]


@pytest.mark.online
def test_a_transfer_leaves_exactly_one_active_binding(repo, pid, on_host):
    host = f"cf257-host-{uuid.uuid4().hex[:6]}"
    root = f"/srv/{uuid.uuid4().hex[:8]}"
    old, new = pid("old"), pid("new")
    on_host(host)

    bind_project_identity(repo, project_id=old, root_path=root)
    bind_project_identity(repo, project_id=new, root_path=root, rebind=True)

    assert _active_for(repo, host, root) == [new]
    row = repo.execute(
        "MATCH (p:ProjectIdentity {project_id:$id}) RETURN p.state AS state, "
        "p.root_key AS rk, p.previous_root_key AS prev",
        {"id": old},
    )[0]
    assert row["state"] == "superseded"
    assert row["rk"] is None, "a retired binding must leave the constraint's key space"
    assert row["prev"], "the retired key is recorded, not just discarded"


@pytest.mark.online
def test_the_same_path_on_another_host_is_never_superseded(repo, pid, on_host):
    """Host scoping in the CYPHER. The offline fake cannot see this predicate at all."""
    root = f"/srv/{uuid.uuid4().hex[:8]}"
    h1, h2 = f"cf257-h1-{uuid.uuid4().hex[:6]}", f"cf257-h2-{uuid.uuid4().hex[:6]}"
    here, there, replacement = pid("here"), pid("there"), pid("replacement")

    on_host(h1)
    bind_project_identity(repo, project_id=here, root_path=root)
    on_host(h2)
    bind_project_identity(repo, project_id=there, root_path=root)
    on_host(h1)
    bind_project_identity(repo, project_id=replacement, root_path=root, rebind=True)

    assert _active_for(repo, h1, root) == [replacement]
    assert _active_for(repo, h2, root) == [there], "another host's binding was superseded"


@pytest.mark.online
def test_the_same_id_and_root_text_from_another_host_is_marked_conflicted(
    repo, pid, on_host
):
    root = f"/srv/{uuid.uuid4().hex[:8]}"
    h1, h2 = f"cf257-h1-{uuid.uuid4().hex[:6]}", f"cf257-h2-{uuid.uuid4().hex[:6]}"
    copied = pid("copied")

    on_host(h1)
    bind_project_identity(repo, project_id=copied, root_path=root)
    on_host(h2)
    with pytest.raises(IdentityBindingConflict):
        bind_project_identity(repo, project_id=copied, root_path=root)

    row = repo.execute(
        "MATCH (p:ProjectIdentity {project_id:$id}) RETURN p.state AS state, "
        "p.bound_host AS host, p.conflicting_bound_host AS conflicting_host",
        {"id": copied},
    )[0]
    assert row == {"state": "conflicted", "host": h1, "conflicting_host": h2}


@pytest.mark.online
def test_a_binding_with_no_bound_host_matches_no_host(repo, pid, on_host):
    """`coalesce(p.bound_host, $host) = $host` made an unstamped binding match EVERY host, which
    is how the host scoping stayed inert across all 60 production rows while reading as correct.

    A row that records no host is not a row about this host. This is the canary the review asked
    for, and it is the mutation the offline lane cannot catch.
    """
    root = f"/srv/{uuid.uuid4().hex[:8]}"
    orphan = pid("orphan")
    repo.execute(
        "CREATE (p:ProjectIdentity {project_id:$id, canonical_root_path:$root, state:'bound'})",
        {"id": orphan, "root": root},
    )
    for host in (f"cf257-any-{uuid.uuid4().hex[:6]}", f"cf257-other-{uuid.uuid4().hex[:6]}"):
        on_host(host)
        assert binding_for_root(repo, root) is None


@pytest.mark.online
def test_a_host_less_binding_blocks_no_host_either(repo, pid, on_host):
    """The other half of the same rule, and the one the first canary missed.

    `binding_for_root` and the rival scan must agree about what a host-less row means, or the two
    halves of one decision disagree: the lookup says "this directory is unbound, mint one" and the
    rival scan then refuses the mint it just asked for, leaving the directory permanently
    unusable with an error naming a row nobody can attribute to a machine.

    A row recording no host is not a row about ANY host -- so it neither resolves a lookup nor
    blocks a claim. Restoring `coalesce(p.bound_host, $host) = $host` in the rival scan alone
    passes the first canary and fails here.
    """
    root = f"/srv/{uuid.uuid4().hex[:8]}"
    orphan, claimant = pid("orphan"), pid("claimant")
    repo.execute(
        "CREATE (p:ProjectIdentity {project_id:$id, canonical_root_path:$root, state:'bound'})",
        {"id": orphan, "root": root},
    )
    host = f"cf257-host-{uuid.uuid4().hex[:6]}"
    on_host(host)

    bind_project_identity(repo, project_id=claimant, root_path=root)
    assert _active_for(repo, host, root) == [claimant]


@pytest.mark.online
def test_concurrent_transfers_cannot_both_hold_the_directory(repo, pid, on_host):
    """Two operators transferring one directory at the same instant.

    The claim is narrow and exact: **at most one identity holds (host, root) once both have
    returned, and no other row is left inside the constraint's key space.**

    It is NOT "the loser always fails". Both callers are operator-authorised, so if they
    serialise cleanly they form a legitimate chain -- incumbent superseded by Y, then Y superseded
    by X -- and both commit. That is last-writer-wins among authorised transfers, which is a
    property of issuing two transfers, not a bypass. If instead they interleave, the loser's
    entire statement rolls back, retirement included.

    Both shapes satisfy the invariant asserted below, and asserting the stronger "the incumbent
    was retired by the final holder" fails on the legitimate chain -- as it did when first
    written here.
    """
    host = f"cf257-host-{uuid.uuid4().hex[:6]}"
    root = f"/srv/{uuid.uuid4().hex[:8]}"
    incumbent, x, y = pid("inc"), pid("x"), pid("y")
    on_host(host)
    bind_project_identity(repo, project_id=incumbent, root_path=root)

    outcomes: dict[str, str] = {}
    gate = threading.Barrier(2)

    def claim(who):
        try:
            gate.wait(timeout=10)
            bind_project_identity(repo, project_id=who, root_path=root, rebind=True)
            outcomes[who] = "committed"
        except Exception as exc:  # noqa: BLE001 - the class is the assertion below
            outcomes[who] = type(exc).__name__

    threads = [threading.Thread(target=claim, args=(w,)) for w in (x, y)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    holders = _active_for(repo, host, root)
    assert len(holders) == 1, f"two active holders after a race: {holders} ({outcomes})"
    assert holders[0] in (x, y)
    assert "committed" in outcomes.values(), f"neither transfer succeeded: {outcomes}"

    # The rollback half, from observable state. Every id that does not hold the directory must be
    # both `superseded` AND out of the constraint's key space (root_key null). A half-applied
    # transfer -- retirement without claim, or a retired row that kept its key -- shows up here:
    # the first as a `bound` row with no key, the second as a second row inside the key space that
    # only the constraint's NULL exemption is hiding.
    rows = repo.execute(
        "MATCH (p:ProjectIdentity) WHERE p.project_id IN $ids "
        "RETURN p.project_id AS id, coalesce(p.state,'bound') AS state, p.root_key AS rk",
        {"ids": [incumbent, x, y]},
    )
    for row in rows:
        if row["id"] == holders[0]:
            assert row["state"] == "bound" and row["rk"], f"holder is not cleanly bound: {row}"
        else:
            assert row["state"] == "superseded", f"non-holder left active: {row} ({outcomes})"
            assert row["rk"] is None, f"retired row still occupies the key space: {row}"


@pytest.mark.online
def test_a_vacated_root_keeps_no_binding(repo, pid, on_host):
    """Transferring an identity to a new directory must release the one it came from, or the old
    root keeps an active claim and the next checkout there is refused as contested."""
    host = f"cf257-host-{uuid.uuid4().hex[:6]}"
    old_root = f"/srv/{uuid.uuid4().hex[:8]}"
    new_root = f"/srv/{uuid.uuid4().hex[:8]}"
    moved = pid("moved")
    on_host(host)

    bind_project_identity(repo, project_id=moved, root_path=old_root)
    bind_project_identity(repo, project_id=moved, root_path=new_root, rebind=True)

    assert _active_for(repo, host, new_root) == [moved]
    assert _active_for(repo, host, old_root) == [], "the vacated root still claims a binding"
    assert binding_for_root(repo, old_root) is None


# ---------------------------------------------------------------------------
# The stale-claim race, against the real lock
# ---------------------------------------------------------------------------

def _claim_for(binding, root, host):
    return IdentityClaim(
        project_id=binding.project_id,
        root_key=root_key_for(root),
        generation=binding.claim_generation,
        host=host,
    )


@pytest.mark.online
def test_a_writer_admitted_and_a_transfer_committed_are_mutually_exclusive(repo, pid, on_host):
    """The fourth-pass counterexample, run against the real lock rather than a model.

    The previous concurrency test stopped at "one binding remains" and never resumed a stale scan
    through the writer, so it permitted exactly the sequence the review reproduced. This one races
    ADMISSION against TRANSFER and asserts they can never both succeed -- which is what makes the
    write boundary a gate rather than a check.

    The mechanism is a write to the identity (`SET p.last_admission_probe`) taken BEFORE the claim
    is read. Neo4j is read-committed and `MERGE` on an existing node takes no write lock, so
    without that probe the claim is validated against a value a concurrent transfer can already
    have replaced. Restoring the un-probed order makes this test fail.
    """
    host = f"cf257-host-{uuid.uuid4().hex[:6]}"
    seen: set[str] = set()
    violations: list[dict] = []

    # REPEATED, because a lost lock is a probabilistic failure and one attempt is not a test of
    # it. Measured on this instance, the two orderings split roughly 99:1, so a single run
    # observes the rare side almost never -- removing the probe passes a one-shot version of this
    # test every time. The loop also records which orderings were actually seen, so a run that
    # silently stopped racing (both sides serialising the same way every time) is visible rather
    # than being reported as a pass.
    for i in range(_RACE_ITERATIONS):
        root = f"/srv/{uuid.uuid4().hex[:8]}"
        x, y = pid(f"x{i}"), pid(f"y{i}")
        on_host(host)
        binding = bind_project_identity(repo, project_id=x, root_path=root)
        claim = _claim_for(binding, root, host)

        outcomes: dict[str, str] = {}
        gate = threading.Barrier(2)
        handles: list = []

        def admit():
            try:
                gate.wait(timeout=10)
                handles.append(admit_structure_writer(repo, label="proj", claim=claim))
                outcomes["admit"] = "committed"
            except Exception as exc:  # noqa: BLE001 - the class is the assertion
                outcomes["admit"] = type(exc).__name__

        def transfer():
            try:
                gate.wait(timeout=10)
                bind_project_identity(repo, project_id=y, root_path=root, rebind=True)
                outcomes["transfer"] = "committed"
            except Exception as exc:  # noqa: BLE001
                outcomes["transfer"] = type(exc).__name__

        threads = [threading.Thread(target=admit), threading.Thread(target=transfer)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        for h in handles:
            release_structure_writer(repo, h)

        seen.add(f"{outcomes.get('admit')}/{outcomes.get('transfer')}")
        if outcomes.get("admit") == "committed" and outcomes.get("transfer") == "committed":
            violations.append(dict(outcomes, root=root))
        assert "committed" in outcomes.values(), f"both sides failed at {i}: {outcomes}"

    assert not violations, (
        f"{len(violations)}/{_RACE_ITERATIONS} races admitted a writer under an identity that "
        f"was being superseded in the same instant: {violations[:3]}. The scan that writer holds "
        "describes a directory that has changed hands."
    )
    assert len(seen) > 1, (
        f"every one of {_RACE_ITERATIONS} races resolved the same way ({seen}) -- the threads are "
        "not actually interleaving, so this test is not exercising the race it claims to."
    )


@pytest.mark.online
def test_a_superseded_claim_is_refused_by_the_real_statement(repo, pid, on_host):
    """Sequential form of the same thing, so a failure distinguishes the gate from the lock."""
    host = f"cf257-host-{uuid.uuid4().hex[:6]}"
    root = f"/srv/{uuid.uuid4().hex[:8]}"
    x, y = pid("x"), pid("y")
    on_host(host)
    stale = _claim_for(bind_project_identity(repo, project_id=x, root_path=root), root, host)
    bind_project_identity(repo, project_id=y, root_path=root, rebind=True)

    with pytest.raises(StaleIdentityClaim):
        admit_structure_writer(repo, label="proj", claim=stale)


@pytest.mark.online
def test_a_transfer_is_refused_while_a_real_writer_is_registered(repo, pid, on_host):
    host = f"cf257-host-{uuid.uuid4().hex[:6]}"
    root = f"/srv/{uuid.uuid4().hex[:8]}"
    x, y = pid("x"), pid("y")
    on_host(host)
    binding = bind_project_identity(repo, project_id=x, root_path=root)
    handle = admit_structure_writer(repo, label="proj", claim=_claim_for(binding, root, host))
    try:
        with pytest.raises(IdentityRootContested, match="structure writer is registered"):
            bind_project_identity(repo, project_id=y, root_path=root, rebind=True)
        assert _active_for(repo, host, root) == [x], "the incumbent was disturbed by a refusal"
    finally:
        release_structure_writer(repo, handle)

    bind_project_identity(repo, project_id=y, root_path=root, rebind=True)
    assert _active_for(repo, host, root) == [y]


@pytest.mark.online
def test_a_transfer_bumps_the_generation_so_earlier_scans_cannot_write(repo, pid, on_host):
    host = f"cf257-host-{uuid.uuid4().hex[:6]}"
    root = f"/srv/{uuid.uuid4().hex[:8]}"
    x, y = pid("x"), pid("y")
    on_host(host)
    first = bind_project_identity(repo, project_id=x, root_path=root)
    bind_project_identity(repo, project_id=y, root_path=root, rebind=True)
    back = bind_project_identity(repo, project_id=x, root_path=root, rebind=True)

    assert back.claim_generation > first.claim_generation
    with pytest.raises(StaleIdentityClaim):
        admit_structure_writer(repo, label="proj", claim=_claim_for(first, root, host))
    handle = admit_structure_writer(repo, label="proj", claim=_claim_for(back, root, host))
    release_structure_writer(repo, handle)


@pytest.mark.online
def test_a_claim_naming_another_directory_is_refused_by_the_real_statement(repo, pid, on_host):
    """The claim must authorise the directory the scan describes.

    Without this the admission would accept any live identity regardless of which directory the
    payload is for -- which is the original CF-257 collision arriving through the write boundary
    instead of the MERGE key.
    """
    host = f"cf257-host-{uuid.uuid4().hex[:6]}"
    root = f"/srv/{uuid.uuid4().hex[:8]}"
    other = f"/srv/{uuid.uuid4().hex[:8]}"
    x = pid("x")
    on_host(host)
    binding = bind_project_identity(repo, project_id=x, root_path=root)

    with pytest.raises(StaleIdentityClaim):
        admit_structure_writer(repo, label="proj", claim=_claim_for(binding, other, host))


@pytest.mark.online
def test_a_claim_from_another_host_is_refused_by_the_real_statement(repo, pid, on_host):
    host = f"cf257-host-{uuid.uuid4().hex[:6]}"
    root = f"/srv/{uuid.uuid4().hex[:8]}"
    x = pid("x")
    on_host(host)
    binding = bind_project_identity(repo, project_id=x, root_path=root)
    foreign = IdentityClaim(
        project_id=x,
        root_key=root_key_for(root),
        generation=binding.claim_generation,
        host=f"cf257-elsewhere-{uuid.uuid4().hex[:6]}",
    )
    with pytest.raises(StaleIdentityClaim):
        admit_structure_writer(repo, label="proj", claim=foreign)


@pytest.mark.online
def test_a_refused_transfer_leaves_the_writer_slot_and_the_incumbent_intact(repo, pid, on_host):
    """A refusal must be inert. If it retired the incumbent before discovering the writer, the
    directory would be left ownerless and the next scan would mint a third identity into it."""
    host = f"cf257-host-{uuid.uuid4().hex[:6]}"
    root = f"/srv/{uuid.uuid4().hex[:8]}"
    x, y = pid("x"), pid("y")
    on_host(host)
    binding = bind_project_identity(repo, project_id=x, root_path=root)
    handle = admit_structure_writer(repo, label="proj", claim=_claim_for(binding, root, host))
    try:
        with pytest.raises(IdentityRootContested):
            bind_project_identity(repo, project_id=y, root_path=root, rebind=True)

        row = repo.execute(
            "MATCH (p:ProjectIdentity {project_id:$id}) RETURN coalesce(p.state,'bound') AS state,"
            " p.root_key AS rk, coalesce(p.claim_generation,0) AS gen",
            {"id": x},
        )[0]
        assert row["state"] == "bound"
        assert row["rk"] == root_key_for(root)
        assert row["gen"] == binding.claim_generation, "a refused transfer bumped the generation"
        assert not repo.execute(
            "MATCH (p:ProjectIdentity {project_id:$id}) RETURN p", {"id": y}
        ), "the refused transfer created its target"
        # The writer it refused for is still admitted and can still finish.
        assert repo.execute(
            "MATCH (p:ProjectIdentity {project_id:$id}) RETURN size(coalesce(p.active_writers,[])) AS n",
            {"id": x},
        )[0]["n"] == 1
    finally:
        release_structure_writer(repo, handle)


@pytest.mark.online
def test_a_conflicted_identity_admits_no_writer(repo, pid, on_host):
    """`state = 'bound'` is a gate in its own right.

    Superseding nulls `root_key`, so a superseded identity is already refused by the directory
    check -- but CONFLICTING does not: a conflicted identity keeps its host and its key, and only
    the state records that the system can no longer tell which directory it belongs to. Without
    this test, deleting the state predicate passes everything.
    """
    host = f"cf257-host-{uuid.uuid4().hex[:6]}"
    root = f"/srv/{uuid.uuid4().hex[:8]}"
    x = pid("x")
    on_host(host)
    binding = bind_project_identity(repo, project_id=x, root_path=root)
    repo.execute(
        "MATCH (p:ProjectIdentity {project_id:$id}) SET p.state = 'conflicted'", {"id": x}
    )

    with pytest.raises(StaleIdentityClaim):
        admit_structure_writer(repo, label="proj", claim=_claim_for(binding, root, host))


@pytest.mark.online
def test_conflict_adoption_clears_and_claims_in_one_transfer(repo, pid, on_host):
    host = f"cf257-host-{uuid.uuid4().hex[:6]}"
    keep = f"/srv/{uuid.uuid4().hex[:8]}"
    copy = f"/srv/{uuid.uuid4().hex[:8]}"
    project_id = pid("resolve")
    on_host(host)

    bind_project_identity(repo, project_id=project_id, root_path=keep)
    with pytest.raises(IdentityBindingConflict):
        bind_project_identity(repo, project_id=project_id, root_path=copy)

    clear_conflict(repo, project_id=project_id, keep_root_path=keep)

    row = repo.execute(
        "MATCH (p:ProjectIdentity {project_id:$id}) RETURN p.state AS state, "
        "p.canonical_root_path AS root, p.root_key AS root_key, "
        "p.conflicting_root_path AS conflicting_root, "
        "p.conflicting_bound_host AS conflicting_host, p.conflicted_at AS conflicted_at",
        {"id": project_id},
    )[0]
    assert row["state"] == "bound"
    assert row["root"] == keep and row["root_key"] == root_key_for(keep)
    assert row["conflicting_root"] is None
    assert row["conflicting_host"] is None
    assert row["conflicted_at"] is None


@pytest.mark.online
def test_failed_conflict_resolution_keeps_the_real_node_conflicted(repo, pid, on_host):
    host = f"cf257-host-{uuid.uuid4().hex[:6]}"
    keep = f"/srv/{uuid.uuid4().hex[:8]}"
    copy = f"/srv/{uuid.uuid4().hex[:8]}"
    project_id = pid("failed-resolve")
    on_host(host)

    bind_project_identity(repo, project_id=project_id, root_path=keep)
    with pytest.raises(IdentityBindingConflict):
        bind_project_identity(repo, project_id=project_id, root_path=copy)
    repo.execute(
        "MATCH (p:ProjectIdentity {project_id:$id}) SET p.active_writers = ['test-writer']",
        {"id": project_id},
    )

    with pytest.raises(IdentityRootContested, match="Nothing was changed"):
        clear_conflict(repo, project_id=project_id, keep_root_path=keep)

    row = repo.execute(
        "MATCH (p:ProjectIdentity {project_id:$id}) RETURN p.state AS state, "
        "p.conflicting_root_path AS conflicting_root, p.conflicted_at AS conflicted_at",
        {"id": project_id},
    )[0]
    assert row["state"] == "conflicted"
    assert row["conflicting_root"] == copy
    assert row["conflicted_at"] is not None


@pytest.mark.online
def test_admission_reads_the_claim_under_a_lock_a_transfer_must_wait_for(repo, pid, on_host):
    """The lock, proven deterministically instead of by racing.

    The race test above cannot reliably provoke this: the gap between reading the claim and
    registering the writer is microseconds, so removing the lock still passes 150 randomised
    attempts. This test holds the transfer's write open in an uncommitted transaction and then
    calls the real admission, which makes the ordering exact:

    * WITH the probe, admission takes the identity's write lock as its FIRST action, so it blocks
      until the transfer commits and only then reads the claim -- seeing the superseded value, and
      refusing.
    * WITHOUT it, admission reads the claim first. Read-committed means it sees the pre-transfer
      value, judges the claim valid, and blocks only at its own write -- so it is admitted under
      an identity that was superseded while it waited.

    That is the difference between a check and a gate, and it is the whole reason
    `SET p.last_admission_probe` exists.
    """
    import os

    from neo4j import GraphDatabase

    host = f"cf257-host-{uuid.uuid4().hex[:6]}"
    root = f"/srv/{uuid.uuid4().hex[:8]}"
    x, y = pid("x"), pid("y")
    on_host(host)
    binding = bind_project_identity(repo, project_id=x, root_path=root)
    claim = _claim_for(binding, root, host)

    driver = GraphDatabase.driver(
        os.getenv("MENHIR_TEST_NEO4J_URI", "bolt://localhost:7688"),
        auth=(
            os.getenv("MENHIR_TEST_NEO4J_USER", "neo4j"),
            os.getenv("MENHIR_TEST_NEO4J_PASSWORD", "testpassword"),
        ),
    )
    result: dict = {}

    def admit_later():
        time.sleep(0.3)  # let the held transaction take the lock first
        try:
            handle = admit_structure_writer(repo, label="proj", claim=claim)
            result["outcome"] = "admitted"
            release_structure_writer(repo, handle)
        except StaleIdentityClaim:
            result["outcome"] = "refused"
        except Exception as exc:  # noqa: BLE001
            result["outcome"] = type(exc).__name__

    try:
        with driver.session() as session:
            tx = session.begin_transaction()
            # The transfer's supersede, held open. Same shape as `_transfer`: lock, then mutate.
            tx.run(
                "MATCH (p:ProjectIdentity {project_id:$id}) "
                "SET p.last_transfer_probe = timestamp() "
                "SET p.root_key = null, p.state = 'superseded', p.superseded_by = $new",
                {"id": x, "new": y},
            ).consume()

            worker = threading.Thread(target=admit_later)
            worker.start()
            time.sleep(1.0)
            assert "outcome" not in result, (
                "admission completed while a transfer held the identity's write lock -- it did "
                "not take the lock before reading the claim"
            )
            tx.commit()
            worker.join(timeout=30)
    finally:
        driver.close()

    assert result.get("outcome") == "refused", (
        f"admission returned {result.get('outcome')!r} for an identity superseded while it "
        "waited; the claim was read before the lock was held"
    )


@pytest.mark.online
def test_release_clears_both_slots_in_the_real_statement(repo, pid, on_host):
    """Release removes the writer from the identity AND the fence, or it is a slow outage.

    A slot left on the identity blocks every future transfer of that directory; one left on the
    fence blocks the migration drain. Neither fails loudly -- they just make a later operation
    hang on a writer that finished long ago -- so this is asserted from the graph rather than
    from the absence of an exception. It has to run online: the offline fake implements release
    itself, so it cannot tell whether the Cypher does.
    """
    host = f"cf257-host-{uuid.uuid4().hex[:6]}"
    root = f"/srv/{uuid.uuid4().hex[:8]}"
    x, y = pid("x"), pid("y")
    on_host(host)
    binding = bind_project_identity(repo, project_id=x, root_path=root)
    handle = admit_structure_writer(repo, label="proj", claim=_claim_for(binding, root, host))

    def slots():
        identity = repo.execute(
            "MATCH (p:ProjectIdentity {project_id:$id}) "
            "RETURN size(coalesce(p.active_writers,[])) AS n",
            {"id": x},
        )[0]["n"]
        fence = repo.execute(
            "MATCH (f:StructureWriteFence {id:'singleton'}) "
            "RETURN size([w IN coalesce(f.writers,[]) WHERE w STARTS WITH $p]) AS n",
            {"p": handle.writer_id + "|"},
        )[0]["n"]
        return identity, fence

    assert slots() == (1, 1), "admission did not register on both"
    release_structure_writer(repo, handle)
    assert slots() == (0, 0), "release left a slot behind"

    # And the proof that it matters: the transfer this writer was blocking now succeeds.
    bind_project_identity(repo, project_id=y, root_path=root, rebind=True)
    assert _active_for(repo, host, root) == [y]


@pytest.mark.online
def test_release_and_admission_share_identity_then_fence_lock_order(repo, pid, on_host):
    """Queue release behind the fence, then admission behind release's identity lock.

    Admission's identity-then-fence order is authoritative. With the old reverse-order release,
    dropping the external fence lock gives release the fence while admission holds the identity,
    making each operation wait for the other. Neo4j must kill one as a deadlock victim, and a
    best-effort release can then return with its old slot stranded. Matching and writing the exact
    identity first makes the queue linear instead: release finishes, then admission finishes.
    """
    host = f"cf257-host-{uuid.uuid4().hex[:6]}"
    root = f"/srv/{uuid.uuid4().hex[:8]}"
    x = pid("x")
    on_host(host)
    binding = bind_project_identity(repo, project_id=x, root_path=root)
    claim = _claim_for(binding, root, host)
    first = admit_structure_writer(repo, label="first", claim=claim)
    admitted = [first]
    outcomes: dict[str, object] = {}
    release_called = threading.Event()
    admission_called = threading.Event()
    workers: list[threading.Thread] = []

    def release_while_fence_is_held():
        release_called.set()
        release_structure_writer(repo, first)
        outcomes["release"] = "returned"

    def admit_behind_release():
        admission_called.set()
        try:
            handle = admit_structure_writer(repo, label="second", claim=claim)
            admitted.append(handle)
            outcomes["admission"] = handle
        except Exception as exc:  # noqa: BLE001 - the exception class is the assertion
            outcomes["admission"] = exc

    session = None
    tx = None
    try:
        session = repo._driver.session()
        tx = session.begin_transaction()
        tx.run(
            "MATCH (f:StructureWriteFence {id:'singleton'}) "
            "SET f.cf257_release_lock_probe = timestamp()"
        ).consume()

        release_worker = threading.Thread(target=release_while_fence_is_held, daemon=True)
        workers.append(release_worker)
        release_worker.start()
        assert release_called.wait(timeout=5), "release worker did not start"
        time.sleep(0.5)
        assert release_worker.is_alive(), "release was not queued behind the held fence lock"

        admission_worker = threading.Thread(target=admit_behind_release, daemon=True)
        workers.append(admission_worker)
        admission_worker.start()
        assert admission_called.wait(timeout=5), "admission worker did not start"
        time.sleep(0.5)
        assert admission_worker.is_alive(), "admission did not queue behind release"

        tx.commit()
        tx = None
        for worker in workers:
            worker.join(timeout=30)

        assert all(not worker.is_alive() for worker in workers), "a worker did not finish"
        assert outcomes.get("release") == "returned"
        second = outcomes.get("admission")
        assert not isinstance(second, Exception), (
            f"admission became a deadlock victim: {type(second).__name__}: {second}"
        )
        assert second is not None, "admission finished without returning a handle"

        release_structure_writer(repo, second)
        slots = repo.execute(
            "MATCH (p:ProjectIdentity {project_id:$id}) "
            "MATCH (f:StructureWriteFence {id:'singleton'}) "
            "RETURN size(coalesce(p.active_writers,[])) AS identity, "
            "size(coalesce(f.writers,[])) AS fence",
            {"id": x},
        )[0]
        assert slots == {"identity": 0, "fence": 0}, "release stranded a writer slot"
    finally:
        if tx is not None:
            tx.rollback()
        if session is not None:
            session.close()
        for worker in workers:
            worker.join(timeout=30)
        for handle in reversed(admitted):
            release_structure_writer(repo, handle)
        repo.execute(
            "MATCH (f:StructureWriteFence {id:'singleton'}) "
            "REMOVE f.cf257_release_lock_probe"
        )
