"""CF-20c: atomically claiming an ABANDONED PREPARED row before replaying it.

The claim exists so ownership transfers BEFORE any graph side effect. Without it two reconcilers
can both read a row as abandoned, both start mutating, and discover the conflict only at the
journal transition -- by which point both have already touched the graph, which is precisely the
double-apply the ownership model exists to prevent.

Only an expired, owned row is claimable. Ownerless rows are OWNER_UNKNOWN, not abandoned: during a
mixed-version rollout an older binary without ownership support may still be executing one.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from menhir.infrastructure import operation_owner as oo
from menhir.infrastructure.graph_operations import GraphOperationsJournal

_PAST = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
_FUTURE = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


@pytest.fixture()
def journal(tmp_path):
    j = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    j._ensure_ready()
    return j


def _insert(journal, op_id, *, token="other:1:abc", expires=_PAST, state="PREPARED"):
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute(
            "INSERT INTO graph_operations (op_id, operation_kind, request_json, state, "
            "created_at, updated_at, owner_token, owner_lease_expires_at) "
            "VALUES (?, 'ENTITY_MERGE', ?, ?, ?, ?, ?, ?)",
            (op_id, json.dumps({"op_id": op_id}), state,
             "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", token, expires),
        )
        conn.commit()


# --------------------------------------------------------------------------- what may be claimed


@pytest.mark.unit
def test_an_expired_claim_can_be_taken(journal):
    _insert(journal, "op-1", expires=_PAST)

    assert journal.claim_abandoned_operation("op-1") is True

    row = journal.get("op-1")
    assert row["owner_token"] == oo.process_owner_token()
    assert oo.classify_ownership(row) == oo.LIVE_OWNER, (
        "after claiming, the row must look live to any OTHER reconciler"
    )
    assert row["state"] == "PREPARED", "claiming transfers ownership, it does not change state"


@pytest.mark.unit
def test_a_live_claim_cannot_be_stolen(journal):
    """A fresh heartbeat is a hard veto: the original writer may be mid-mutation."""
    _insert(journal, "op-1", expires=_FUTURE)

    assert journal.claim_abandoned_operation("op-1") is False
    assert journal.get("op-1")["owner_token"] == "other:1:abc", "owner must be untouched"


@pytest.mark.unit
def test_an_ownerless_row_cannot_be_claimed(journal):
    """OWNER_UNKNOWN is not ABANDONED.

    A legacy row predates the ownership fence, so an older binary may still be running it. Claiming
    it would be exactly the double-apply this design prevents.
    """
    _insert(journal, "op-legacy", token=None, expires=None)

    assert journal.claim_abandoned_operation("op-legacy") is False
    assert journal.get("op-legacy")["owner_token"] is None


@pytest.mark.unit
def test_a_row_with_an_owner_but_no_expiry_cannot_be_claimed(journal):
    """Nothing to compare against, so liveness is unprovable -- fail closed."""
    _insert(journal, "op-1", token="other:1:abc", expires=None)

    assert journal.claim_abandoned_operation("op-1") is False


@pytest.mark.unit
@pytest.mark.parametrize("state", ["COMMITTED", "NEEDS_REVIEW", "FAILED", "REVERSED"])
def test_a_row_that_left_prepared_cannot_be_claimed(journal, state):
    """PREPARED is the only state in which work can still be in flight."""
    _insert(journal, "op-1", expires=_PAST, state=state)

    assert journal.claim_abandoned_operation("op-1") is False


@pytest.mark.unit
def test_a_missing_row_is_not_claimable(journal):
    assert journal.claim_abandoned_operation("nope") is False


@pytest.mark.unit
def test_a_row_we_already_own_is_not_reclaimable(journal):
    """Not abandoned. A reconciler seeing its own claim should renew, not re-take a lease."""
    _insert(journal, "op-1", token=oo.process_owner_token(), expires=_PAST)

    assert journal.claim_abandoned_operation("op-1") is False


# --------------------------------------------------------------------------- atomicity


@pytest.mark.unit
def test_only_one_of_two_concurrent_reconcilers_wins_the_claim(journal):
    """The property the whole mechanism rests on.

    Two threads race for the same abandoned row. Exactly one must win, and the row must end up
    owned by that winner -- not by whichever wrote last.
    """
    _insert(journal, "op-contested", expires=_PAST)

    results: list[tuple[str, bool]] = []
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def _claim(token: str) -> None:
        barrier.wait()
        try:
            won = journal.claim_abandoned_operation("op-contested", owner_token=token)
        except Exception as exc:  # noqa: BLE001 -- recorded, then asserted on below
            with lock:
                results.append((token, f"raised {type(exc).__name__}"))
            return
        with lock:
            results.append((token, won))

    threads = [
        threading.Thread(target=_claim, args=(f"inst-{name}:1:{name * 3}",))
        for name in ("a", "b")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(results) == 2, "both threads must have finished"
    assert all(isinstance(won, bool) for _, won in results), f"a claim raised: {results}"
    winners = [token for token, won in results if won is True]
    assert len(winners) == 1, f"exactly one reconciler may win, got {results}"
    assert journal.get("op-contested")["owner_token"] == winners[0]


@pytest.mark.unit
def test_a_second_claim_after_the_first_is_rejected(journal):
    """Sequential form of the same property, without thread timing."""
    _insert(journal, "op-1", expires=_PAST)

    assert journal.claim_abandoned_operation("op-1", owner_token="inst-a:1:aaa") is True
    assert journal.claim_abandoned_operation("op-1", owner_token="inst-b:2:bbb") is False
    assert journal.get("op-1")["owner_token"] == "inst-a:1:aaa"


# --------------------------------------------------------------------------- after claiming


@pytest.mark.unit
def test_the_claimant_can_then_renew_its_own_heartbeat(journal):
    """Claim then renew is the real sequence: take the row, then hold it while replaying."""
    _insert(journal, "op-1", expires=_PAST)
    assert journal.claim_abandoned_operation("op-1") is True

    assert journal.renew_owner_heartbeat("op-1") is True


@pytest.mark.unit
def test_the_previous_owner_can_no_longer_renew_after_being_displaced(journal):
    """The displaced writer must be able to discover it lost the row and stop.

    If it could still renew, it would keep mutating while believing it owned work the claimant is
    now replaying -- the double-apply, just with the roles reversed.
    """
    _insert(journal, "op-1", token="old-owner:1:aaa", expires=_PAST)
    assert journal.claim_abandoned_operation("op-1", owner_token="new-owner:2:bbb") is True

    assert journal.renew_owner_heartbeat("op-1", owner_token="old-owner:1:aaa") is False


@pytest.mark.unit
def test_claiming_leaves_the_request_and_snapshot_untouched(journal):
    """Ownership is metadata. The frozen intent must survive a transfer unchanged."""
    _insert(journal, "op-1", expires=_PAST)
    before = journal.get("op-1")

    journal.claim_abandoned_operation("op-1")
    after = journal.get("op-1")

    for field in ("request_json", "before_snapshot_json", "expected_after_sha256",
                  "operation_kind", "created_at", "state"):
        assert after[field] == before[field], f"{field} must not change on claim"
