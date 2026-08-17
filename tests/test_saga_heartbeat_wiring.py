"""CF-211 part 2, wiring: every saga coordinator holds a heartbeat across its mutation.

The heartbeat and the revocation seam are tested in isolation elsewhere. What is under test HERE is
that the coordinators actually use them -- the difference between the mechanism existing and the
safety argument holding.

Two properties, deliberately separated because they are proven differently:

* **the revocation predicate is PUBLISHED while the mutation runs.** Asserted by having the stub
  graph adapter read the ambient ContextVar at the moment it is called. A test that merely checked
  "a heartbeat object was constructed" would pass even if the scope closed before the mutation.
* **the lease is derived per saga kind.** Asserted by spying on ``lease_seconds_for_kind``, so the
  two-statement METRIC_WRITE path cannot silently inherit a one-statement TTL.

That ``execute`` refuses to dispatch once the predicate is false is covered in
test_saga_writer_heartbeat.py. These stubs replace the adapter, so ``execute`` is never reached; the
two halves compose rather than overlap.
"""

from __future__ import annotations

import pytest

from menhir.infrastructure import neo4j as n4
from menhir.infrastructure import operation_owner as oo
from menhir.services.delete_coordinator import DeleteCoordinator
from menhir.services.merge_coordinator import MergeCoordinator, merge_state_fingerprint
from menhir.services.metric_write_coordinator import (
    MetricWriteCoordinator,
    state_fingerprint,
)
from menhir.services.unmerge_coordinator import UnmergeCoordinator

#: A merge runs BOTH-PRESENT -> ABSORBED-GONE. An unmerge runs the reverse. Naming them by the
#: state rather than by the operation keeps each coordinator's before/after the right way round --
#: getting that inverted is what made the first draft of this file fail.
_BOTH_PRESENT = {"survivor_present": True, "absorbed_present": True, "lineage_recorded": False}
_ABSORBED_GONE = {"survivor_present": True, "absorbed_present": False, "lineage_recorded": True}


class _Journal:
    """Stub journal. Records renewals so the heartbeat's target is visible."""

    def __init__(self, expected_after=None):
        self.expected_after = expected_after
        self.renewals: list[str] = []
        self.committed: list[str] = []
        self.reviews: list[str] = []
        self.db_path = ":memory:"

    def _ensure_ready(self):
        pass

    def get(self, op_id):
        return {"expected_after_sha256": self.expected_after}

    def renew_owner_heartbeat(self, op_id, *, seconds=None, owner_token=None):
        self.renewals.append(op_id)
        return True

    def mark_committed(self, op_id):
        self.committed.append(op_id)

    def mark_needs_review(self, op_id, *, observed_error=None):
        self.reviews.append(op_id)

    def mark_failed(self, op_id, *, reason=None):
        pass

    def mark_reversed(self, op_id):
        pass

    def record_attempt(self, op_id, *, error=None):
        pass

    def prepare(self, **kw):
        return kw.get("op_id")


class _RevocationProbe:
    """Reads the ambient revocation predicate at mutation time.

    This is the whole point: it proves the scope is OPEN when the graph is touched, not merely that
    a heartbeat was created somewhere earlier.
    """

    def __init__(self):
        self.predicate_at_mutation = "not-called"

    def _observe(self):
        self.predicate_at_mutation = n4._revocation.get()


@pytest.fixture()
def kind_spy(monkeypatch):
    """Records which saga kind each coordinator derives its lease for."""
    seen: list[str] = []
    real = oo.lease_seconds_for_kind

    def _spy(operation_kind):
        seen.append(operation_kind)
        return real(operation_kind)

    monkeypatch.setattr(oo, "lease_seconds_for_kind", _spy)
    return seen


# --------------------------------------------------------------------------- merge


class _MergeAdapter(_RevocationProbe):
    def __init__(self):
        super().__init__()
        self.state = dict(_BOTH_PRESENT)

    def fetch_merge_state(self, s, a):
        return dict(self.state)

    def merge_entity(self, s, a, *, similarity, operation_id=None):
        self._observe()
        self.state = dict(_ABSORBED_GONE)
        return {"merged": 1}


def _merge_request(op_id):
    return {
        "op_id": op_id, "survivor_uuid": "s", "absorbed_uuid": "a", "similarity": 0.9,
        "expected_before_sha256": merge_state_fingerprint(_BOTH_PRESENT, op_id=op_id),
    }


@pytest.mark.unit
def test_merge_publishes_the_revocation_predicate_across_its_mutation(kind_spy):
    op_id = "op-merge"
    journal = _Journal(expected_after=merge_state_fingerprint(_ABSORBED_GONE, op_id=op_id))
    adapter = _MergeAdapter()

    MergeCoordinator(graph_adapter=adapter, journal=journal)._apply(_merge_request(op_id))

    assert callable(adapter.predicate_at_mutation), (
        "a revocation predicate must be published while the merge mutates, not merely created"
    )
    assert adapter.predicate_at_mutation() is True, "a live claim must read as continue"
    assert kind_spy == ["ENTITY_MERGE"]


@pytest.mark.unit
def test_the_scope_is_closed_again_after_the_mutation(kind_spy):
    op_id = "op-merge"
    journal = _Journal(expected_after=merge_state_fingerprint(_ABSORBED_GONE, op_id=op_id))

    MergeCoordinator(graph_adapter=_MergeAdapter(), journal=journal)._apply(_merge_request(op_id))

    assert n4._revocation.get() is None, (
        "the predicate must not leak past the saga that published it"
    )


# --------------------------------------------------------------------------- unmerge


class _UnmergeAdapter(_RevocationProbe):
    def __init__(self):
        super().__init__()
        self.state = dict(_ABSORBED_GONE)

    def fetch_merge_state(self, s, a):
        return dict(self.state)

    def restore_merge_snapshot(self, **kw):
        self._observe()
        self.state = dict(_BOTH_PRESENT)
        return {"restored": 1}


@pytest.mark.unit
def test_unmerge_publishes_the_revocation_predicate_across_its_mutation(kind_spy):
    op_id = "op-unmerge"
    journal = _Journal(expected_after=merge_state_fingerprint(_BOTH_PRESENT, op_id=op_id))
    adapter = _UnmergeAdapter()
    request = {
        "op_id": op_id, "survivor_uuid": "s", "absorbed_uuid": "a", "merge_op_id": "m",
        "expected_before_sha256": merge_state_fingerprint(_ABSORBED_GONE, op_id=op_id),
    }

    UnmergeCoordinator(graph_adapter=adapter, journal=journal)._apply(request, {})

    assert callable(adapter.predicate_at_mutation)
    assert kind_spy == ["ENTITY_UNMERGE"]


# --------------------------------------------------------------------------- delete


class _DeleteAdapter(_RevocationProbe):
    def __init__(self):
        super().__init__()
        self.deleted_once = False

    def capture_node_state(self, uuid):
        return None if self.deleted_once else {"uuid": uuid}

    def newly_unreferenced_evidence(self, uuids):
        return []

    def delete_entities_returning_uuids(self, uuids, *, require_scope=None):
        self._observe()
        self.deleted_once = True
        return list(uuids)


@pytest.mark.unit
def test_delete_publishes_the_revocation_predicate_across_its_mutation(kind_spy):
    """Delete's mutation is inline rather than in an _apply, so its scope is opened separately."""
    adapter = _DeleteAdapter()
    coord = DeleteCoordinator(graph_adapter=adapter, journal=_Journal())

    result = coord.delete_entity("n1")

    assert result["deleted"] == ["n1"]
    assert callable(adapter.predicate_at_mutation)
    assert kind_spy == ["ENTITY_DELETE"]


# --------------------------------------------------------------------------- metric


class _MetricAdapter(_RevocationProbe):
    def __init__(self):
        super().__init__()
        self.state = None

    def fetch_metric_state(self, *, view_key):
        return self.state

    def record_metric(self, **kw):
        self._observe()
        return {"uuid": kw.get("node_uuid")}


class _Receipts:
    def _ensure_ready(self):
        pass


@pytest.mark.unit
def test_metric_write_uses_its_own_two_statement_lease(kind_spy):
    """The specific mistake this guards: applying a one-statement TTL to the two-statement path."""
    op_id = "op-metric"
    key = "s|c"
    adapter = _MetricAdapter()
    coord = MetricWriteCoordinator(
        graph_adapter=adapter, journal=_Journal(expected_after=None), receipts=_Receipts(),
        telemetry_db_path=":memory:",
    )
    request = {
        "op_id": op_id, "view_key": key, "subject": "s", "counter": "c", "value": 1.0,
        "namespace": "ns", "valid_at": "2026-01-01T00:00:00+00:00", "source": "t",
        "metric_uuid": "m-1",
        "expected_before_sha256": state_fingerprint(None, view_key=key),
    }

    coord._apply(request)

    assert callable(adapter.predicate_at_mutation)
    assert kind_spy == ["METRIC_WRITE"]
    assert oo.lease_seconds_for_kind("METRIC_WRITE") > oo.lease_seconds_for_kind("ENTITY_MERGE")


# --------------------------------------------------------------------------- coverage


@pytest.mark.unit
def test_every_saga_kind_a_coordinator_writes_has_a_declared_statement_count():
    """A kind missing from the table silently falls back, which is safe but hides a real omission."""
    written_kinds = {
        "ENTITY_MERGE", "ENTITY_UNMERGE", "ENTITY_DELETE", "SESSION_TTL_DELETE",
        "METRIC_WRITE", "LEGACY_ENTITY_UNMERGE",
    }
    missing = written_kinds - set(oo.SAGA_STATEMENT_COUNTS)
    assert not missing, f"undeclared saga kinds: {sorted(missing)}"
