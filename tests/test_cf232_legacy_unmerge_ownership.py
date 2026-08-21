"""CF-232: the legacy unmerge lane mutated outside the ownership scope.

Five of six saga coordinators wrap their mutation window in `owned_mutation(...)`, which does two
things that belong together: it holds the writer's claim (the heartbeat renews the lease) and it
PUBLISHES the revocation predicate that `Neo4jRepository.execute` consults before every statement.
`legacy_unmerge_coordinator` ran `prepare -> restore_merge_snapshot -> verify -> mark_committed`
with neither, so `_revocation` stayed at its default `None` and every statement dispatched
unconditionally.

Its module docstring claimed "Journaled and atomic exactly like the exact lane", which was not true
of the ownership half.

WHY IT WAS SAFE, AND WHY THAT IS NOT ENOUGH. `saga_reconcile_dispatcher` maps
`LEGACY_ENTITY_UNMERGE` to a disposition that always quarantines and never replays, so there was no
concurrent writer for a heartbeat to protect against. That is a fact about a DIFFERENT module. The
day that disposition becomes replayable, this lane has no heartbeat and nothing would say so.

THE TEST IS IN TWO HALVES THAT COMPOSE, rather than one mimic of the guard:

  (A) the coordinator publishes a live revocation predicate for the duration of the restore --
      asserted by reading the real `_revocation` ContextVar from inside the mutation;
  (B) a REAL `Neo4jRepository.execute` refuses to dispatch when that predicate is false.

(B) needs no driver and no database: the revocation check runs BEFORE `self._get_driver()`, so the
refusal happens on a repository whose driver would fail if it were ever reached. Together the two
halves are the guarantee -- revocation during the restore stops the mutation -- without a fake
standing in for the thing under test.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from menhir.infrastructure import neo4j as n4
from menhir.services import legacy_unmerge_coordinator as luc

pytestmark = pytest.mark.unit


class _AuditStore:
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot

    def fetch_merge_audit(self, *, absorbed_uuid: str, limit: int = 1) -> list[dict[str, Any]]:
        return [{"snapshot_json": json.dumps(self._snapshot)}]


class _Journal:
    """Records the saga transitions without a database."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def _ensure_ready(self) -> None:
        pass

    def prepare(self, **kw: Any) -> None:
        self.events.append(("prepare", str(kw.get("op_id"))))

    def record_attempt(self, op_id: str, error: str = "") -> None:
        self.events.append(("attempt", op_id))

    def mark_needs_review(self, op_id: str, observed_error: str = "") -> None:
        self.events.append(("needs_review", op_id))

    def mark_committed(self, op_id: str) -> None:
        self.events.append(("committed", op_id))

    # WriterHeartbeat renews through the journal; accept and record whatever it calls.
    def renew_claim(self, *a: Any, **k: Any) -> bool:
        self.events.append(("renew", str(a[0]) if a else ""))
        return True

    def __getattr__(self, name: str) -> Any:  # tolerate other heartbeat calls
        def _noop(*a: Any, **k: Any) -> Any:
            return True

        return _noop


class _Adapter:
    """Graph adapter that records the revocation predicate visible DURING the mutation."""

    def __init__(self) -> None:
        self.revocation_during_mutation: Any = "not-called"
        self.state_calls = 0

    def fetch_merge_state(self, survivor_uuid: str, absorbed_uuid: str) -> dict[str, Any]:
        self.state_calls += 1
        if self.state_calls == 1:
            return {"survivor_present": True, "absorbed_present": False, "lineage_recorded": True}
        return {"survivor_present": True, "absorbed_present": True, "lineage_recorded": False}

    def peers_exist(self, uuids: list[str]) -> set[str]:
        return set(uuids)

    def restore_merge_snapshot(self, **kw: Any) -> dict[str, Any]:
        # The real repository reads exactly this ContextVar before dispatching a statement.
        self.revocation_during_mutation = n4._revocation.get()
        return {"restored": 1}


# Shape taken from `domain/legacy_snapshot.parse`, which requires survivor_uuid, absorbed_uuid and
# a `properties` dict carrying a uuid -- guessing at the field names produced MALFORMED_SNAPSHOT
# and the lane bailed before it ever reached the mutation, which would have made every assertion
# below vacuous.
_SNAPSHOT = {
    "survivor_uuid": "surv-1",
    "absorbed_uuid": "abs-1",
    "properties": {"uuid": "abs-1", "name": "absorbed"},
    "relationships": [],
    "absorbed_episodes": [],
    "rebound_episodes": [],
}


def _coordinator() -> tuple[luc.LegacyUnmergeCoordinator, _Adapter, _Journal]:
    adapter = _Adapter()
    journal = _Journal()
    coord = luc.LegacyUnmergeCoordinator(
        graph_adapter=adapter, journal=journal, telemetry_store=_AuditStore(_SNAPSHOT)
    )
    return coord, adapter, journal


def _run(coord: luc.LegacyUnmergeCoordinator) -> dict[str, Any]:
    return coord.unmerge_legacy("abs-1", manifest=["abs-1"], acknowledge_degraded=True)


# ---------------------------------------------------------------------------
# (A) the predicate is published for the duration of the mutation
# ---------------------------------------------------------------------------


def test_the_restore_runs_with_a_published_revocation_predicate() -> None:
    """THE FINDING. Before the fix this read `None`, which is what `Neo4jRepository.execute`
    treats as "no owner to check" -- every statement dispatched unconditionally."""
    coord, adapter, _journal = _coordinator()

    _run(coord)

    assert adapter.revocation_during_mutation != "not-called", "the mutation never ran"
    assert adapter.revocation_during_mutation is not None
    assert callable(adapter.revocation_during_mutation)


def test_the_published_predicate_reports_a_live_claim_during_the_restore() -> None:
    """A predicate that is published but already false would abort every legitimate restore.
    Publishing it is only half the contract; it has to say 'still mine'."""
    coord, adapter, _journal = _coordinator()

    _run(coord)

    assert adapter.revocation_during_mutation() is True


def test_the_predicate_is_not_left_published_after_the_mutation() -> None:
    """POSITIVE CONTROL for scope. `owned_mutation` is a context manager; if it leaked, an
    unrelated later statement would be gated on a dead claim."""
    coord, _adapter, _journal = _coordinator()

    _run(coord)

    assert n4._revocation.get() is None


def test_the_saga_still_reaches_committed() -> None:
    """POSITIVE CONTROL, the one that matters most: adding the ownership scope must not break the
    lane. A fix that raised or refused would satisfy the assertions above."""
    coord, _adapter, journal = _coordinator()

    result = _run(coord)

    assert [kind for kind, _ in journal.events][:1] == ["prepare"]
    assert ("committed", journal.events[-1][1]) == journal.events[-1]
    assert result["exact"] is False  # non-negotiable for this lane
    assert result["survivor_restored"] is False


# ---------------------------------------------------------------------------
# (B) a real repository refuses to dispatch when the predicate is false
# ---------------------------------------------------------------------------


def test_a_real_repository_refuses_to_dispatch_under_a_false_predicate() -> None:
    """The other half of the guarantee, against the REAL `Neo4jRepository.execute`.

    No driver and no database: the revocation check runs before `self._get_driver()`, so a
    repository pointed at nothing still demonstrates the refusal. If the check ever moved below the
    driver call, this test would fail with a connection error instead of the refusal -- which is
    the regression worth catching."""
    repo = n4.Neo4jRepository(uri="bolt://nowhere:1", database="d", user="u", password="p")

    token = n4._revocation.set(lambda: False)
    try:
        with pytest.raises(n4.SagaOwnershipRevoked, match="ownership was lost"):
            repo.execute("RETURN 1")
    finally:
        n4._revocation.reset(token)


def test_a_real_repository_checks_the_predicate_before_the_first_statement() -> None:
    """POSITIVE CONTROL for (B): the refusal is not merely a retry-loop artefact. With a TRUE
    predicate the same call proceeds far enough to fail on the driver instead, proving the guard
    let it through rather than short-circuiting everything."""
    repo = n4.Neo4jRepository(uri="bolt://nowhere:1", database="d", user="u", password="p")

    token = n4._revocation.set(lambda: True)
    try:
        with pytest.raises(Exception) as caught:
            repo.execute("RETURN 1")
        assert not isinstance(caught.value, n4.SagaOwnershipRevoked)
    finally:
        n4._revocation.reset(token)
