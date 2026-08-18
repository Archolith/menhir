"""CF-211: the lease/window timing invariant, and revocation across a real thread hop.

Two gaps closed here, both found by owner review after the first implementation looked complete.

**`TTL > W` was not sufficient.** A writer can legitimately dispatch a mutation just BEFORE the next
heartbeat discovers renewal is failing, so with H the renewal interval the statement can still be in
flight at ``t0 + H + W``. The claim must outlive ``H + W + margin``. Since ``H = TTL / D`` that is
circular, and solving gives ``TTL > D/(D-1) * (W + M)``. The original derivation returned ``W + M``
and was violated for every saga kind.

**Scheduling alone is not a proof.** Even a correct TTL leaves the argument resting on the heartbeat
thread being punctual, which a GC pause or a starved interpreter breaks. So dispatch is also gated on
remaining headroom, making each individual dispatch locally provable.

The last test is the real-path one: it drives a genuine ``Neo4jRepository.execute`` through
``asyncio.to_thread`` -- the boundary production actually uses -- because a stub adapter reading the
ContextVar proves scope lifetime but not propagation across a thread hop.
"""

from __future__ import annotations

import asyncio
from time import monotonic, sleep

import pytest

from menhir.infrastructure import neo4j as n4
from menhir.infrastructure import operation_owner as oo
from menhir.services.saga_writer_heartbeat import WriterHeartbeat


class _Journal:
    def __init__(self, result=True):
        self.result = result

    def renew_owner_heartbeat(self, op_id, *, seconds=None, owner_token=None):
        return self.result


# --------------------------------------------------------------------------- the inequality


@pytest.mark.unit
@pytest.mark.parametrize("kind", sorted(oo.SAGA_STATEMENT_COUNTS))
def test_ttl_outlives_detection_lag_plus_the_mutation_window(kind):
    """The invariant the whole ABANDONED classification rests on.

    A dispatch at t0+H that runs for W must finish before t0+TTL, or a reconciler can claim and
    replay a mutation that is still legitimately executing.
    """
    ttl = oo.lease_seconds_for_kind(kind)
    window = oo.mutation_window_for_kind(kind)
    detection_lag = ttl / oo.RENEW_DIVISOR

    assert ttl > detection_lag + window + oo.LEASE_SAFETY_MARGIN_S, (
        f"{kind}: TTL {ttl} must outlive H {detection_lag:.1f} + W {window:.1f} + margin "
        f"{oo.LEASE_SAFETY_MARGIN_S}"
    )


@pytest.mark.unit
def test_the_naive_window_only_bound_would_not_satisfy_the_invariant():
    """Pins WHY the derivation is 1.5x, so a future simplification back to W+M fails loudly."""
    window = oo.mutation_window_for_kind("ENTITY_MERGE")
    naive = window + oo.LEASE_SAFETY_MARGIN_S

    assert naive < naive / oo.RENEW_DIVISOR + window + oo.LEASE_SAFETY_MARGIN_S, (
        "a TTL of merely W + margin is insufficient once detection lag is counted"
    )
    assert oo.lease_seconds_for_kind("ENTITY_MERGE") > naive


@pytest.mark.unit
def test_the_renewal_divisor_is_shared_not_duplicated():
    """The TTL formula and the renewal interval must agree; separate copies is how this broke."""
    from menhir.services import saga_writer_heartbeat as hb

    assert hb._RENEW_DIVISOR is oo.RENEW_DIVISOR


# --------------------------------------------------------------------------- per-dispatch headroom


@pytest.mark.unit
def test_dispatch_is_allowed_while_ample_claim_remains():
    beat = WriterHeartbeat(_Journal(), "op-1", lease_seconds=600, required_headroom_s=100)

    assert beat.should_continue() is True
    assert beat.remaining_lease_s() > 500


@pytest.mark.unit
def test_dispatch_is_refused_when_too_little_claim_remains_even_though_not_revoked():
    """The local half of the proof: not-revoked is NOT sufficient to dispatch.

    A late heartbeat thread would leave `lost` unset while the claim quietly ran out. Without this
    check the writer would dispatch a mutation that outlives its own lease.
    """
    beat = WriterHeartbeat(_Journal(), "op-1", lease_seconds=10, required_headroom_s=100)

    assert beat.lost is False, "not revoked..."
    assert beat.should_continue() is False, "...but there is not enough lease left to dispatch"


@pytest.mark.unit
def test_revocation_still_wins_regardless_of_headroom():
    beat = WriterHeartbeat(_Journal(), "op-1", lease_seconds=6000, required_headroom_s=1)
    beat._lost.set()

    assert beat.should_continue() is False


@pytest.mark.unit
def test_no_headroom_requirement_keeps_the_old_revocation_only_behaviour():
    """Direct constructions without a kind (tests, opt-outs) must not silently start refusing."""
    beat = WriterHeartbeat(_Journal(), "op-1", lease_seconds=1, required_headroom_s=None)

    assert beat.should_continue() is True


@pytest.mark.unit
def test_a_successful_renewal_extends_the_local_headroom():
    """Drives the real renewal loop, not a hand-set field.

    Without this the headroom gate would degrade into a one-shot timer: seeded at construction and
    never refreshed, every long saga would eventually refuse to dispatch even while healthy.
    """
    beat = WriterHeartbeat(_Journal(result=True), "op-1", lease_seconds=3, required_headroom_s=1)
    seeded_expiry = beat._expires_at
    beat.start()
    try:
        deadline = monotonic() + 8.0
        while beat._expires_at <= seeded_expiry and monotonic() < deadline:
            sleep(0.05)
    finally:
        beat.stop()

    assert beat._expires_at > seeded_expiry, "a successful renewal must push the local expiry out"
    assert beat.lost is False


@pytest.mark.unit
def test_owned_mutation_supplies_the_kinds_window_as_headroom():
    """Wiring: the headroom must be the kind's OWN window, not a shared default."""
    from menhir.services.saga_writer_heartbeat import owned_mutation

    with owned_mutation(_Journal(), "op-1", operation_kind="METRIC_WRITE") as beat:
        assert beat is not None
        assert beat._required_headroom_s == oo.mutation_window_for_kind("METRIC_WRITE")
        assert beat._required_headroom_s > oo.mutation_window_for_kind("ENTITY_MERGE")


# --------------------------------------------------------------------------- real-path propagation


class _FakeSession:
    def __init__(self, recorder):
        self._recorder = recorder

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, **kwargs):
        self._recorder.append(query)
        return []


class _FakeDriver:
    def __init__(self, recorder):
        self._recorder = recorder

    def session(self, **_kw):
        return _FakeSession(self._recorder)


async def test_revocation_propagates_across_asyncio_to_thread(monkeypatch):
    """The real-path test: a genuine execute() reached through the production thread boundary.

    The stub-adapter wiring tests prove the scope is open when the graph is touched, but they never
    reach `execute`, and they never cross a thread. Production does: `_off_loop` dispatches blocking
    saga work via `asyncio.to_thread`. If ContextVar propagation across that hop ever broke -- or an
    intermediate layer introduced its own thread -- revocation would silently stop working while
    every in-process test kept passing.
    """
    calls: list = []
    repo = n4.Neo4jRepository(uri="bolt://x", database="neo4j", user="u", password="p")
    monkeypatch.setattr(repo, "_get_driver", lambda: _FakeDriver(calls))

    def _blocking_saga_mutation():
        return repo.execute("MATCH (n) RETURN n")

    # Live claim: the statement must reach the driver from inside the worker thread.
    with n4.revocation_scope(lambda: True):
        await asyncio.to_thread(_blocking_saga_mutation)
    assert len(calls) == 1, "a live claim must still dispatch across the thread hop"

    # Revoked: the predicate must be visible in the worker thread and refuse the dispatch.
    with n4.revocation_scope(lambda: False):
        with pytest.raises(n4.SagaOwnershipRevoked):
            await asyncio.to_thread(_blocking_saga_mutation)
    assert len(calls) == 1, "nothing further may be dispatched once revoked"


async def test_a_heartbeat_predicate_survives_the_thread_hop(monkeypatch):
    """Same boundary, but with the real WriterHeartbeat rather than a lambda."""
    calls: list = []
    repo = n4.Neo4jRepository(uri="bolt://x", database="neo4j", user="u", password="p")
    monkeypatch.setattr(repo, "_get_driver", lambda: _FakeDriver(calls))
    beat = WriterHeartbeat(_Journal(), "op-1", lease_seconds=600, required_headroom_s=10)

    with n4.revocation_scope(beat.should_continue):
        await asyncio.to_thread(lambda: repo.execute("MATCH (n) RETURN n"))
        assert len(calls) == 1

        beat._lost.set()
        with pytest.raises(n4.SagaOwnershipRevoked):
            await asyncio.to_thread(lambda: repo.execute("MATCH (n) RETURN n"))
    assert len(calls) == 1


async def test_insufficient_headroom_also_refuses_across_the_thread_hop(monkeypatch):
    """The headroom half of the gate must hold in the worker thread too, not just in-process."""
    calls: list = []
    repo = n4.Neo4jRepository(uri="bolt://x", database="neo4j", user="u", password="p")
    monkeypatch.setattr(repo, "_get_driver", lambda: _FakeDriver(calls))
    beat = WriterHeartbeat(_Journal(), "op-1", lease_seconds=5, required_headroom_s=1000)

    with n4.revocation_scope(beat.should_continue):
        with pytest.raises(n4.SagaOwnershipRevoked):
            await asyncio.to_thread(lambda: repo.execute("MATCH (n) RETURN n"))

    assert calls == []
