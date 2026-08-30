from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from menhir.infrastructure.view_projection_repair import (
    ViewProjectionRepairClaim,
    ViewProjectionRepairRepository,
)
from menhir.services.view_projection_repair import ViewProjectionRepairService


def _claim(**changes: Any) -> ViewProjectionRepairClaim:
    base = ViewProjectionRepairClaim(
        repair_key="op-1\x1fview-1",
        owner_id="worker-f",
        claim_token="token-1",
        view_uuid="view-1",
        view_key="ns\x1fsubject\x1fscalar_state\x1fslot",
        view_kind="scalar_state",
        view_subtype="scalar_state",
        source_family="typed_scalar_assertions",
        namespace="ns",
        namespace_key="ns",
        fence_generation=4,
        subject_uuid="subject-1",
        predicate="",
        domain="",
        attempt_count=2,
    )
    return replace(base, **changes)


class _Neo4j:
    def __init__(self, responses: list[list[dict[str, Any]]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def execute(
        self, query: str, params: dict[str, Any] | None = None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        self.calls.append((query, params or {}, kwargs))
        return self.responses.pop(0)


@pytest.mark.unit
def test_claim_is_locked_then_rechecks_eligibility_and_captures_fences() -> None:
    neo4j = _Neo4j([[
        {
            "repair_key": "r1",
            "owner_id": "worker-f",
            "claim_token": "opaque",
            "view_uuid": "v1",
            "view_key": "vk",
            "view_kind": "timeline",
            "view_subtype": "event_timeline",
            "source_family": "typed_event_assertions",
            "namespace": "ns",
            "namespace_key": "ns",
            "fence_generation": 7,
            "subject_uuid": "subject-1",
            "predicate": "purchased",
            "domain": "collecting",
            "attempt_count": 3,
        }
    ]])
    repo = ViewProjectionRepairRepository(neo4j)  # type: ignore[arg-type]

    claims = repo.claim_pending(owner_id="worker-f", limit=3, lease_seconds=90)

    query, params, kwargs = neo4j.calls[0]
    lock_at = query.index("SET r.claim_lock_version")
    assert query.index("WHERE r.status IN ['pending', 'failed', 'blocked']", lock_at) > lock_at
    assert "r.claim_token = randomUUID()" in query
    assert "duration({seconds: $lease_seconds})" in query
    assert "r.attempt_count = coalesce(r.attempt_count, 0) + 1" in query
    assert "r.claimed_fence_generation = coalesce(f.generation, 0)" in query
    assert params == {"owner_id": "worker-f", "limit": 3, "lease_seconds": 90}
    assert kwargs.get("safe_to_reexecute", False) is False
    assert claims[0].predicate == "purchased"
    assert claims[0].fence_generation == 7


@pytest.mark.unit
def test_completion_is_conditional_on_token_lease_fence_and_projection_identity() -> None:
    neo4j = _Neo4j([[{"completed": 1}]])
    repo = ViewProjectionRepairRepository(neo4j)  # type: ignore[arg-type]

    assert repo.complete(_claim()) is True

    query, params, _ = neo4j.calls[0]
    assert "r.claim_owner = $owner_id" in query
    assert "r.claim_token = $claim_token" in query
    assert "r.lease_expires_at > datetime()" in query
    assert "coalesce(f.generation, 0) = r.claimed_fence_generation" in query
    assert "all(v IN current_versions WHERE" in query
    assert "v.view_subject_uuid" in query
    assert "v.view_predicate" in query
    assert params == {
        "repair_key": "op-1\x1fview-1",
        "owner_id": "worker-f",
        "claim_token": "token-1",
    }


@pytest.mark.unit
def test_stale_or_superseded_claim_cannot_complete() -> None:
    neo4j = _Neo4j([[]])
    repo = ViewProjectionRepairRepository(neo4j)  # type: ignore[arg-type]

    assert repo.complete(_claim(claim_token="stale-token")) is False


@pytest.mark.unit
def test_retryable_failure_records_error_and_increments_failure_count() -> None:
    neo4j = _Neo4j([[{"updated": 1}]])
    repo = ViewProjectionRepairRepository(neo4j)  # type: ignore[arg-type]

    assert repo.fail(_claim(), "fold failed") is True

    query, params, _ = neo4j.calls[0]
    assert "r.failure_count = coalesce(r.failure_count, 0) + 1" in query
    assert params["status"] == "failed"
    assert params["error"] == "fold failed"
    assert params["terminal"] is False


class _Store:
    def __init__(self, claims: list[ViewProjectionRepairClaim]) -> None:
        self.claims = claims
        self.completed: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.terminal: list[tuple[str, str]] = []
        self.claim_args: dict[str, Any] = {}

    def claim_pending(self, **kwargs: Any) -> list[ViewProjectionRepairClaim]:
        self.claim_args = kwargs
        return list(self.claims)

    def complete(self, claim: ViewProjectionRepairClaim) -> bool:
        self.completed.append(claim.repair_key)
        return True

    def fail(self, claim: ViewProjectionRepairClaim, error: str) -> bool:
        self.failed.append((claim.repair_key, error))
        return True

    def terminal_not_rebuildable(self, claim: ViewProjectionRepairClaim, reason: str) -> bool:
        self.terminal.append((claim.repair_key, reason))
        return True


class _Scalar:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def rebuild_scalar_state(self, subject_uuid: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("state", subject_uuid, kwargs))
        return {"complete": True}

    def rebuild_scalar_history(self, subject_uuid: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("history", subject_uuid, kwargs))
        return {"complete": True}


class _Events:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    def rebuild_lane(self, lane: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((lane, kwargs))
        return {"complete": True}


@pytest.mark.unit
def test_dispatches_scalar_kinds_to_existing_deterministic_apis() -> None:
    store = _Store([
        _claim(repair_key="state", view_kind="scalar_state"),
        _claim(repair_key="history", view_kind="scalar_history", view_subtype="scalar_history"),
    ])
    scalar = _Scalar()
    service = ViewProjectionRepairService(store, scalar_rebuilder=scalar, event_rebuilder=_Events())

    result = service.run_pending(owner_id="worker-f", limit=5, lease_seconds=60)

    assert [call[:2] for call in scalar.calls] == [
        ("state", "subject-1"),
        ("history", "subject-1"),
    ]
    assert all(call[2] == {"namespace": "ns", "source": "view-projection-repair"}
               for call in scalar.calls)
    assert store.completed == ["state", "history"]
    assert result["complete"] == 2


@pytest.mark.unit
def test_dispatches_event_timeline_from_persisted_lane_identity() -> None:
    claim = _claim(
        view_kind="timeline",
        view_subtype="event_timeline",
        predicate="purchased",
        domain="collecting",
    )
    store = _Store([claim])
    events = _Events()
    service = ViewProjectionRepairService(store, scalar_rebuilder=_Scalar(), event_rebuilder=events)

    result = service.run_pending(owner_id="worker-f")

    lane, kwargs = events.calls[0]
    assert lane.key == ("ns", "subject-1", "purchased", "collecting")
    assert kwargs == {"source": "view-projection-repair"}
    assert result["complete"] == 1


@pytest.mark.unit
def test_event_source_family_with_missing_lane_fields_fails_instead_of_becoming_legacy() -> None:
    claim = _claim(
        view_kind="timeline",
        view_subtype="",
        source_family="typed_event_assertions",
        predicate="",
    )
    store = _Store([claim])
    service = ViewProjectionRepairService(store, scalar_rebuilder=_Scalar(), event_rebuilder=_Events())

    result = service.run_pending(owner_id="worker-f")

    assert result["failed"] == 1
    assert "predicate" in store.failed[0][1]
    assert store.terminal == []


@pytest.mark.unit
def test_legacy_timeline_and_admission_audit_are_terminal() -> None:
    store = _Store([
        _claim(repair_key="timeline", view_kind="timeline", view_subtype="legacy_timeline"),
        _claim(repair_key="audit", view_kind="admission_audit", view_subtype="admission_audit"),
    ])
    service = ViewProjectionRepairService(store, scalar_rebuilder=_Scalar(), event_rebuilder=_Events())

    result = service.run_pending(owner_id="worker-f")

    assert [key for key, _ in store.terminal] == ["timeline", "audit"]
    assert result["terminal_not_rebuildable"] == 2


@pytest.mark.unit
def test_counter_is_retryable_failure_without_fake_deterministic_rebuild() -> None:
    scalar = _Scalar()
    events = _Events()
    store = _Store([_claim(view_kind="counter", view_subtype="counter")])
    service = ViewProjectionRepairService(store, scalar_rebuilder=scalar, event_rebuilder=events)

    result = service.run_pending(owner_id="worker-f")

    assert scalar.calls == []
    assert events.calls == []
    assert "namespace-wide reperception" in store.failed[0][1]
    assert result["failed"] == 1


@pytest.mark.unit
def test_incomplete_rebuild_records_actionable_failure_details() -> None:
    class _IncompleteScalar(_Scalar):
        def rebuild_scalar_state(self, subject_uuid: str, **kwargs: Any) -> dict[str, Any]:
            return {"complete": False, "stale_skipped": [{"view_key": "slot-1"}]}

    store = _Store([_claim()])
    service = ViewProjectionRepairService(
        store, scalar_rebuilder=_IncompleteScalar(), event_rebuilder=_Events()
    )

    service.run_pending(owner_id="worker-f")

    assert "stale_skipped" in store.failed[0][1]


@pytest.mark.unit
def test_completion_rejected_after_dispatch_is_reported_as_lost_claim() -> None:
    class _LostStore(_Store):
        def complete(self, claim: ViewProjectionRepairClaim) -> bool:
            return False

    store = _LostStore([_claim()])
    service = ViewProjectionRepairService(
        store, scalar_rebuilder=_Scalar(), event_rebuilder=_Events()
    )

    result = service.run_pending(owner_id="worker-f")

    assert result["complete"] == 0
    assert result["lost_claim"] == 1


@pytest.mark.unit
def test_missing_erasure_receipt_identity_fails_closed_and_pass_is_empty_idempotent() -> None:
    store = _Store([_claim(subject_uuid="")])
    service = ViewProjectionRepairService(store, scalar_rebuilder=_Scalar(), event_rebuilder=_Events())

    result = service.run_pending(owner_id="worker-f")

    assert "subject_uuid" in store.failed[0][1]
    assert result["failed"] == 1

    empty = _Store([])
    second = ViewProjectionRepairService(
        empty, scalar_rebuilder=_Scalar(), event_rebuilder=_Events()
    ).run_pending(owner_id="worker-f")
    assert second == {
        "claimed": 0,
        "complete": 0,
        "failed": 0,
        "terminal_not_rebuildable": 0,
        "lost_claim": 0,
        "results": [],
    }
