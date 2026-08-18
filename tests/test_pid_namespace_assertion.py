"""CF-20c: the deployment preconditions that make death evidence trustworthy.

Two separate holes are pinned here, both of which would let recovery act on evidence it does not
actually have.

**Hostname equality is not PID-namespace equality.** ``classify_ownership`` treats "the owner token
records my hostname" as licence to ask the local OS whether that PID is running. That inference is
valid only if a hostname identifies exactly one PID namespace -- which containers on a shared
kernel, cloned images, and two nodes mounting one journal volume all break. The process cannot
verify the property about itself, so it is an operator assertion, defaulting to NOT asserted.

**An attestation is an override, not a faster clock.** It must never outrank a live heartbeat, must
name a person, and must leave an audit trail -- otherwise it is the clock-based inference this
design removed, wearing a signature.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from menhir.infrastructure import operation_owner as oo
from menhir.infrastructure import process_liveness
from menhir.infrastructure.graph_operations import (
    GraphOperationError,
    GraphOperationsJournal,
)

#: A writer on THIS host whose PID is gone. PID 999999 is not a live process.
_DEAD_LOCAL = f"inst:{process_liveness.hostname()}:999999:deadnonce"
_REMOTE = "inst:other-host:4242:remotenonce"

_PAST = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
_FUTURE = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


@pytest.fixture()
def journal(tmp_path):
    j = GraphOperationsJournal(db_path=tmp_path / "ops.db")
    j._ensure_ready()
    return j


def _insert(journal, op_id, *, token=_DEAD_LOCAL, expires=_PAST, state="PREPARED"):
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute(
            "INSERT INTO graph_operations (op_id, operation_kind, request_json, state, "
            "created_at, updated_at, owner_token, owner_lease_expires_at) "
            "VALUES (?, 'ENTITY_MERGE', ?, ?, ?, ?, ?, ?)",
            (op_id, json.dumps({"op_id": op_id}), state,
             "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", token, expires),
        )
        conn.commit()


# ------------------------------------------------------- the PID-namespace deployment assertion


@pytest.mark.unit
def test_the_assertion_is_off_unless_the_deployment_sets_it(monkeypatch):
    """Fail closed: an unconfigured deployment does not get automatic PID-based recovery."""
    monkeypatch.delenv(oo.HOST_PID_NAMESPACE_ENV, raising=False)
    assert oo.host_pid_namespace_is_verifiable() is False


@pytest.mark.unit
@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " True "])
def test_the_assertion_accepts_the_usual_affirmatives(monkeypatch, value):
    monkeypatch.setenv(oo.HOST_PID_NAMESPACE_ENV, value)
    assert oo.host_pid_namespace_is_verifiable() is True


@pytest.mark.unit
@pytest.mark.parametrize("value", ["", "0", "false", "no", "maybe", "off"])
def test_anything_that_is_not_an_affirmative_is_not_an_assertion(monkeypatch, value):
    """A typo must not silently enable PID evidence."""
    monkeypatch.setenv(oo.HOST_PID_NAMESPACE_ENV, value)
    assert oo.host_pid_namespace_is_verifiable() is False


@pytest.mark.unit
def test_without_the_assertion_a_dead_local_pid_is_not_death_evidence(monkeypatch):
    """The whole point.

    Same hostname, PID demonstrably absent from THIS namespace -- and still not claimable, because
    nobody has established that the recorded PID was ever in this namespace to begin with. In a
    container fleet where every host reports the same name, "PID 999999 is not running here" says
    nothing whatsoever about the writer.
    """
    monkeypatch.delenv(oo.HOST_PID_NAMESPACE_ENV, raising=False)
    row = {"owner_token": _DEAD_LOCAL, "owner_lease_expires_at": _PAST}

    assert oo.classify_ownership(row) == oo.OWNER_UNKNOWN


@pytest.mark.unit
def test_with_the_assertion_the_same_row_becomes_recoverable(monkeypatch):
    """The contrast case, so the previous test pins the assertion and not something else."""
    monkeypatch.setenv(oo.HOST_PID_NAMESPACE_ENV, "1")
    row = {"owner_token": _DEAD_LOCAL, "owner_lease_expires_at": _PAST}

    assert oo.classify_ownership(row) == oo.ABANDONED


@pytest.mark.unit
def test_the_claim_path_honours_the_assertion_too(monkeypatch, journal):
    """A gate the observer applies but the mutating path skips is not a gate.

    ``claim_abandoned_operation`` re-runs classification inside its own transaction, so the
    assertion has to bind there as well -- otherwise the classifier would be decorative and the
    SQL would happily transfer ownership on expiry alone.
    """
    monkeypatch.delenv(oo.HOST_PID_NAMESPACE_ENV, raising=False)
    _insert(journal, "op-1")

    assert journal.claim_abandoned_operation("op-1") is False
    assert journal.get("op-1")["owner_token"] == _DEAD_LOCAL

    monkeypatch.setenv(oo.HOST_PID_NAMESPACE_ENV, "1")
    assert journal.claim_abandoned_operation("op-1") is True


@pytest.mark.unit
def test_the_assertion_does_not_make_a_remote_owner_claimable(monkeypatch, journal):
    """It licenses inspecting a LOCAL PID. It does not grant reach onto another machine."""
    monkeypatch.setenv(oo.HOST_PID_NAMESPACE_ENV, "1")
    _insert(journal, "op-remote", token=_REMOTE)

    assert oo.classify_ownership(journal.get("op-remote")) == oo.OWNER_UNKNOWN
    assert journal.claim_abandoned_operation("op-remote") is False


@pytest.mark.unit
def test_the_assertion_does_not_override_a_live_lease(monkeypatch, journal):
    """Nothing in this mechanism may weaken the fresh-heartbeat veto."""
    monkeypatch.setenv(oo.HOST_PID_NAMESPACE_ENV, "1")
    _insert(journal, "op-live", expires=_FUTURE)

    assert oo.classify_ownership(journal.get("op-live")) == oo.LIVE_OWNER
    assert journal.claim_abandoned_operation("op-live") is False


# ------------------------------------------------------------------ attestation is an override


@pytest.mark.unit
def test_an_attestation_cannot_outrank_a_live_heartbeat(monkeypatch):
    """Expiry is evaluated FIRST, deliberately.

    A fresh lease means the writer renewed it seconds ago, so an attestation against it is stale or
    mistaken. Honouring it would replay underneath a running writer -- the double-apply, arrived at
    through the operator surface instead of through the clock.
    """
    monkeypatch.setenv(oo.HOST_PID_NAMESPACE_ENV, "1")
    row = {
        "owner_token": _DEAD_LOCAL,
        "owner_lease_expires_at": _FUTURE,
        "owner_death_attested_by": "ctharvey",
    }

    assert oo.classify_ownership(row) == oo.LIVE_OWNER


@pytest.mark.unit
def test_a_live_claim_cannot_be_attested_in_the_first_place(journal):
    """The durable half of the same rule: the write side refuses before the read side has to."""
    _insert(journal, "op-live", expires=_FUTURE)

    assert journal.attest_owner_death("op-live", attested_by="ctharvey") is False
    assert journal.get("op-live")["owner_death_attested_by"] is None


@pytest.mark.unit
def test_an_ownerless_row_cannot_be_attested(journal):
    """There is no writer named on the row, so there is nobody whose death is being attested."""
    _insert(journal, "op-legacy", token=None, expires=None)

    assert journal.attest_owner_death("op-legacy", attested_by="ctharvey") is False


@pytest.mark.unit
@pytest.mark.parametrize("state", ["COMMITTED", "NEEDS_REVIEW", "FAILED", "REVERSED"])
def test_a_terminal_row_cannot_be_attested(journal, state):
    _insert(journal, "op-1", state=state)

    assert journal.attest_owner_death("op-1", attested_by="ctharvey") is False


@pytest.mark.unit
def test_an_attestation_records_who_and_when(journal):
    """An override that cannot be audited afterwards is indistinguishable from a guess."""
    _insert(journal, "op-1")

    assert journal.attest_owner_death("op-1", attested_by="  ctharvey  ") is True

    row = journal.get("op-1")
    assert row["owner_death_attested_by"] == "ctharvey", "the name must be stored trimmed"
    stamped = datetime.fromisoformat(row["owner_death_attested_at"])
    assert stamped.tzinfo is not None, "the attestation instant must be timezone-aware UTC"
    assert abs((datetime.now(timezone.utc) - stamped).total_seconds()) < 300


@pytest.mark.unit
@pytest.mark.parametrize("name", ["", "   ", None])
def test_an_unsigned_attestation_is_refused(journal, name):
    _insert(journal, "op-1")

    with pytest.raises(GraphOperationError, match="attested_by is required"):
        journal.attest_owner_death("op-1", attested_by=name)


@pytest.mark.unit
def test_an_attested_remote_owner_is_recoverable_without_the_pid_assertion(monkeypatch, journal):
    """Attestation is the ONLY route for a deployment that cannot verify its PID namespace.

    This is what keeps the fail-closed default operable rather than merely safe: an unconfigured
    deployment still has a way back, and it goes through a named human.
    """
    monkeypatch.delenv(oo.HOST_PID_NAMESPACE_ENV, raising=False)
    _insert(journal, "op-remote", token=_REMOTE)
    assert journal.claim_abandoned_operation("op-remote") is False

    assert journal.attest_owner_death("op-remote", attested_by="ctharvey") is True
    assert journal.claim_abandoned_operation("op-remote") is True


# ----------------------------------------------- an attestation does not outlive the owner it names


@pytest.mark.unit
def test_an_attestation_does_not_survive_ownership_transfer(journal):
    """attest A -> claim B -> B's lease goes stale. B must NOT read as abandoned.

    This is the Critical regression. The attestation was written about A. After the claim the row
    belongs to B, and B may still be executing. If the old attestation survived, the moment B's
    lease went stale a third reconciler would see "attested dead" and replay underneath B --
    reinstating the false-death double-apply that positive death evidence exists to prevent.
    """
    _insert(journal, "op-1", token=_REMOTE, expires=_PAST)
    assert journal.attest_owner_death("op-1", attested_by="ctharvey") is True
    assert journal.claim_abandoned_operation("op-1") is True

    row = journal.get("op-1")
    assert row["owner_token"] == oo.process_owner_token()
    assert row["owner_death_attested_by"] is None, "the attestation must not outlive its subject"
    assert row["owner_death_attested_for_token"] is None

    # Now age the new owner's claim, exactly as a slow replay would.
    with sqlite3.connect(journal.db_path) as conn:
        conn.execute(
            "UPDATE graph_operations SET owner_lease_expires_at = ? WHERE op_id = ?",
            (_PAST, "op-1"),
        )
        conn.commit()

    stale = journal.get("op-1")
    assert oo.classify_ownership(stale) != oo.ABANDONED, (
        "a stale claim held by THIS process must not be abandoned on a predecessor's attestation"
    )
    assert journal.claim_abandoned_operation("op-1", owner_token="inst:other:9:zzz") is False


@pytest.mark.unit
def test_a_successful_heartbeat_retires_an_attestation(journal):
    """attest A -> A renews -> A's lease later expires. The old attestation must be gone.

    A renewal is the owner proving it is alive, which disproves the attestation outright. Left in
    place it would lie dormant behind the fresh lease and reactivate the instant that lease expired,
    turning a transient stall into a licence to replay.
    """
    _insert(journal, "op-1", token=_DEAD_LOCAL, expires=_PAST)
    assert journal.attest_owner_death("op-1", attested_by="ctharvey") is True

    assert journal.renew_owner_heartbeat("op-1", owner_token=_DEAD_LOCAL) is True
    assert journal.get("op-1")["owner_death_attested_by"] is None

    with sqlite3.connect(journal.db_path) as conn:
        conn.execute(
            "UPDATE graph_operations SET owner_lease_expires_at = ? WHERE op_id = ?",
            (_PAST, "op-1"),
        )
        conn.commit()

    # Only the ordinary PID evidence may apply now -- never the retired attestation.
    monkey_free = journal.get("op-1")
    assert str(monkey_free.get("owner_death_attested_by") or "") == ""
