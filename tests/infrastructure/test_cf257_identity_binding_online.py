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
import uuid

import pytest

from menhir.infrastructure.project_identity_binding import (
    PROJECT_IDENTITY_CONSTRAINTS,
    IdentityRootContested,
    bind_project_identity,
    binding_for_root,
)

pytestmark = [pytest.mark.online]


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
