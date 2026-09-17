"""Counterexamples for the extraction lease, written before the lease.

Design: `.agent/plans/menhir-snapshot-p3-extraction-design-2026-09-17.md` (decision 4).

The lease exists to stop two extractions of one project interleaving into one root. Everything
here is written as the failure it prevents, not as the API it offers, because that is what found
both P2B bugs: the quota was a check-then-act and the crash path returned a 500, and neither was
visible from reading the code.

Four properties, in the order they bite:

1. A lease expires, and an expired holder is not the holder any more.
2. A superseded holder cannot act, even before its own clock says it expired.
3. **A stale holder cannot release someone else's lease.** This is the dangerous one: expiry is
   obvious and everyone implements it, whereas a `release()` that only checks the project name
   hands the next holder's lease to whoever crashed last.
4. A crashed holder must not block the project forever.

Time is injected. A lease test that sleeps is a lease test that is slow AND flaky, and the
interesting moments are precisely at the boundary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from menhir.snapshot.extraction_lease import (
    ExtractionLease,
    LeaseError,
    LeaseStore,
)

pytestmark = pytest.mark.unit

_TTL = 60.0


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock) -> LeaseStore:
    return LeaseStore(tmp_path / "leases", ttl_s=_TTL, clock=clock)


def _acquire(
    store: LeaseStore, *, owner: str, upload: str, project: str = "proj"
) -> ExtractionLease:
    return store.acquire(project_key=project, upload_id=upload, owner=owner)


# --- 1. expiry ----------------------------------------------------------------------------------


def test_a_live_lease_blocks_a_second_holder(store: LeaseStore) -> None:
    _acquire(store, owner="worker-a", upload="snap-1")

    with pytest.raises(LeaseError) as excinfo:
        _acquire(store, owner="worker-b", upload="snap-2")

    assert excinfo.value.code == "snapshot.lease.held"


def test_an_expired_holder_is_no_longer_the_holder(
    store: LeaseStore, clock: FakeClock
) -> None:
    """The whole point of a TTL: A must stop being able to act, not merely stop being renewed."""
    lease = _acquire(store, owner="worker-a", upload="snap-1")
    clock.advance(_TTL + 1)

    with pytest.raises(LeaseError) as excinfo:
        store.check(lease)
    assert excinfo.value.code == "snapshot.lease.expired"


def test_checking_is_required_during_work_not_only_at_its_start(
    store: LeaseStore, clock: FakeClock
) -> None:
    """An extraction can outrun its lease.

    Valid at the start proves nothing about the write happening thirty seconds later, which is why
    the worker re-checks. Pinned here so the property survives someone "optimising" the check out
    of the loop.
    """
    lease = _acquire(store, owner="worker-a", upload="snap-1")
    store.check(lease)  # fine now

    clock.advance(_TTL - 1)
    store.check(lease)  # still fine, just

    clock.advance(2)
    with pytest.raises(LeaseError):
        store.check(lease)


# --- 2. supersession ----------------------------------------------------------------------------


def test_a_superseded_holder_cannot_act_even_before_its_own_expiry(
    store: LeaseStore, clock: FakeClock
) -> None:
    """A's clock is not the authority; the store is.

    A acquires, goes quiet past the TTL, B legitimately claims. If A then resumes -- a paused
    process, a long GC, a VM migration -- it must be refused by SUPERSESSION rather than only by
    its own expiry, because its own view of time may be wrong in either direction.
    """
    a = _acquire(store, owner="worker-a", upload="snap-1")
    clock.advance(_TTL + 1)
    _acquire(store, owner="worker-b", upload="snap-2")

    # Rewind A's view of the clock: from inside A, its lease still looks live.
    clock.now = 1000.0 + 1
    with pytest.raises(LeaseError) as excinfo:
        store.check(a)
    assert excinfo.value.code == "snapshot.lease.superseded"


def test_a_different_snapshot_cannot_ride_another_snapshots_lease(
    store: LeaseStore,
) -> None:
    """The lease binds the SNAPSHOT, not just the project.

    A lease for project P and snapshot 1 must not authorise work on project P and snapshot 2, or
    two extractions of different snapshots interleave into one root -- the exact failure the lease
    exists to prevent.
    """
    lease = _acquire(store, owner="worker-a", upload="snap-1")
    impostor = ExtractionLease(
        project_key=lease.project_key,
        upload_id="snap-2",
        owner=lease.owner,
        generation=lease.generation,
        expires_at=lease.expires_at,
    )

    with pytest.raises(LeaseError) as excinfo:
        store.check(impostor)
    assert excinfo.value.code == "snapshot.lease.not_held"


# --- 3. the dangerous one: a stale holder must not free someone else's lease ---------------------


def test_a_stale_holder_cannot_release_the_current_holders_lease(
    store: LeaseStore, clock: FakeClock
) -> None:
    """The failure a project-name-keyed release produces, and the reason for the generation.

    A expires, B claims. A wakes up and does what a well-behaved worker does on the way out: it
    releases its lease. If release matches on project alone, A has just freed B's lease while B is
    mid-extraction, and a third worker can now claim the project underneath it.
    """
    a = _acquire(store, owner="worker-a", upload="snap-1")
    clock.advance(_TTL + 1)
    b = _acquire(store, owner="worker-b", upload="snap-2")

    with pytest.raises(LeaseError) as excinfo:
        store.release(a)
    assert excinfo.value.code in {
        "snapshot.lease.superseded",
        "snapshot.lease.not_held",
    }

    # B is untouched and still authoritative.
    store.check(b)


def test_a_stale_holder_cannot_renew_its_way_back_in(
    store: LeaseStore, clock: FakeClock
) -> None:
    """Renewal is not a second chance to acquire.

    If renew() re-writes the lease whenever the project is free-looking, a paused A comes back and
    takes a project B is actively extracting.
    """
    a = _acquire(store, owner="worker-a", upload="snap-1")
    clock.advance(_TTL + 1)
    b = _acquire(store, owner="worker-b", upload="snap-2")

    with pytest.raises(LeaseError):
        store.renew(a)
    store.check(b)


# --- 4. a crash must not block the phase ---------------------------------------------------------


def test_a_crashed_holder_does_not_block_the_project_forever(
    store: LeaseStore, clock: FakeClock
) -> None:
    """The argument against a bare lock file, asserted rather than asserted-in-a-comment.

    A acquires and never releases -- it died. The project must become claimable again on its own,
    with no operator action, because a lock whose holder is gone is how a phase stops permanently
    at 3am.
    """
    _acquire(store, owner="worker-a", upload="snap-1")

    clock.advance(_TTL + 1)
    recovered = _acquire(store, owner="worker-b", upload="snap-2")

    store.check(recovered)
    assert recovered.owner == "worker-b"


def test_liveness_is_not_inferred_from_a_process_identifier(store: LeaseStore) -> None:
    """PID death is not evidence, so the lease must not record one.

    PIDs are reused, and a PID in another namespace is a different process entirely. A lease that
    stores one invites "the holder must be dead by now", which is an inference about a distributed
    system dressed up as a fact. Asserted structurally so the field cannot be added quietly.
    """
    lease = _acquire(store, owner="worker-a", upload="snap-1")

    fields = {f.lower() for f in vars(lease)}
    assert not any("pid" in f or "hostname" in f or "process" in f for f in fields), (
        f"the lease records process identity: {sorted(fields)}"
    )


# --- release and reacquire, the ordinary path ----------------------------------------------------


def test_the_holder_can_release_and_the_project_is_immediately_claimable(
    store: LeaseStore,
) -> None:
    lease = _acquire(store, owner="worker-a", upload="snap-1")
    store.release(lease)

    nxt = _acquire(store, owner="worker-b", upload="snap-2")
    assert nxt.owner == "worker-b"
    assert nxt.generation > lease.generation


def test_renewal_extends_the_holder_it_belongs_to(
    store: LeaseStore, clock: FakeClock
) -> None:
    lease = _acquire(store, owner="worker-a", upload="snap-1")
    clock.advance(_TTL - 1)

    renewed = store.renew(lease)
    clock.advance(2)

    store.check(renewed)
    assert renewed.expires_at > lease.expires_at


def test_a_generation_is_never_reused_across_release_and_reclaim(
    store: LeaseStore,
) -> None:
    """The bug the release test exposed, pinned as its own property.

    The first implementation deleted the lease file on release. With it gone the store saw no
    history, so the next claim computed generation 1 again -- and a generation that can be reused
    is not a version. A stale lease object from the previous turn would then match the new one on
    generation, leaving only the owner field between it and authority.

    Release now leaves an expired tombstone, so generations are monotonic for the life of the
    project and evidence from turn N can never be authority in turn N+1.
    """
    seen: list[int] = []
    for turn in range(4):
        lease = _acquire(store, owner=f"worker-{turn}", upload=f"snap-{turn}")
        seen.append(lease.generation)
        store.release(lease)

    assert seen == sorted(set(seen)), f"generations repeated or went backwards: {seen}"
    assert len(set(seen)) == len(seen)


def test_a_released_holder_cannot_act_on_its_old_lease(store: LeaseStore) -> None:
    """Releasing is giving up authority, not parking it.

    The same object that was authority a moment ago must be refused afterwards -- including when
    nobody else has claimed the project yet, which is the case where a naive implementation is
    most tempted to be lenient.
    """
    lease = _acquire(store, owner="worker-a", upload="snap-1")
    store.release(lease)

    with pytest.raises(LeaseError):
        store.check(lease)
    with pytest.raises(LeaseError):
        store.release(lease)


# --- found by mutation testing: two guards nothing was holding ------------------------------------


def test_losing_the_exclusive_create_race_is_a_refusal_not_a_silent_steal(
    tmp_path: Path, clock: FakeClock
) -> None:
    """The `O_CREAT|O_EXCL` guard, which is the entire concurrency control.

    Deleting its raise survived the suite: every other test arranges ONE claimant, so the branch
    where two workers compute the same next generation and the filesystem picks a winner was never
    reached. Here the loser's file is already there when it tries.

    Without the raise, the loser would return a lease it does not hold and both workers would
    proceed -- the exact interleaving the generation exists to prevent.
    """
    store = LeaseStore(tmp_path / "leases", ttl_s=_TTL, clock=clock)
    first = _acquire(store, owner="worker-a", upload="snap-1")
    clock.advance(_TTL + 1)  # first lapses, so the project looks free to both

    # Stand in for the racer that got there microseconds earlier: generation 2 already exists.
    directory = store._project_dir("proj")
    (directory / f"{first.generation + 1:012d}.json").write_text(
        '{"upload_id": "snap-9", "owner": "worker-z", "generation": 2, "expires_at": 1e12}',
        encoding="utf-8",
    )

    with pytest.raises(LeaseError) as excinfo:
        _acquire(store, owner="worker-b", upload="snap-2")
    assert excinfo.value.code == "snapshot.lease.held"


def test_a_lease_is_expired_exactly_at_its_deadline_not_a_moment_after(
    store: LeaseStore, clock: FakeClock
) -> None:
    """The expiry comparison, at the boundary rather than well past it.

    Every other expiry test advances the clock by TTL+1, so `>=` and `>` behave identically and
    flipping one survived. The moment that decides it is `now == expires_at`: at the deadline the
    lease is gone, and one tick before it is still held.

    Off by one here means two workers each believe they hold the project for one clock tick, which
    is precisely long enough to both start writing.
    """
    lease = _acquire(store, owner="worker-a", upload="snap-1")

    clock.now = lease.expires_at - 1
    store.check(lease)  # one tick before: still ours

    clock.now = lease.expires_at
    with pytest.raises(LeaseError) as excinfo:
        store.check(lease)
    assert excinfo.value.code == "snapshot.lease.expired"
