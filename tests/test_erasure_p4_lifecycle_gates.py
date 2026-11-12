"""Offline checks for the P4 queue-node lifecycle gates (query text and call shapes)."""

from __future__ import annotations

from typing import Any

from menhir.infrastructure.cypher import (
    EPISODE_NOT_ERASED,
    EPISODE_NOT_MARKED,
    build_reset_or_fail_query,
)
from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository
from menhir.infrastructure.erasure_fence import marker_absent_cypher
from menhir.infrastructure.typed_assertion_models import _RECORD_CYPHER


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(
        self, query: str, params: dict[str, Any] | None = None, **_: Any
    ) -> list[dict[str, Any]]:
        self.calls.append((query, dict(params or {})))
        return []


class _Repo(EpisodeLifecycleRepository):
    def __init__(self) -> None:
        self.neo4j = _Recorder()


def test_episode_not_marked_is_the_marker_predicate_over_n() -> None:
    assert EPISODE_NOT_MARKED == marker_absent_cypher("n")
    assert "subject_uuid: n.uuid" in EPISODE_NOT_MARKED
    assert EPISODE_NOT_ERASED  # P4 adds, never replaces


def test_reset_or_fail_query_carries_the_marker_gate() -> None:
    query = build_reset_or_fail_query(
        where=["n.processing_state = 'ENRICHING'"],
        exhausted_substage="'a'",
        reset_substage="'b'",
        exhausted_error="'c'",
        reset_error="'d'",
    )
    assert EPISODE_NOT_MARKED in query


def test_claim_query_gates_reserves_and_returns_fence_keys() -> None:
    repo = _Repo()
    assert (
        repo.claim_pending_episode(
            "q1", max_attempts=3, worker_id="w", lease_seconds=60
        )
        is None
    )
    claim_query, params = next(
        (q, p) for q, p in repo.neo4j.calls if "reserved_twin_uuid" in q
    )
    assert EPISODE_NOT_MARKED in claim_query
    assert (
        "n.reserved_twin_uuids = coalesce(n.reserved_twin_uuids, []) + $reserved_twin_uuid"
        in claim_query
    )
    assert "n.publish_protocol = 2" in claim_query
    for key in ("resolved_episode_uuid", "fence_namespace_key", "publish_protocol"):
        assert f"AS {key}" in claim_query
    assert params["reserved_twin_uuid"]


def test_claim_generates_a_fresh_reservation_each_call() -> None:
    repo = _Repo()
    repo.claim_pending_episode("q1", max_attempts=3, worker_id="w", lease_seconds=60)
    repo.claim_pending_episode("q1", max_attempts=3, worker_id="w", lease_seconds=60)
    reserved = [
        p["reserved_twin_uuid"]
        for q, p in repo.neo4j.calls
        if "reserved_twin_uuid" in p
    ]
    assert len(reserved) == 2 and reserved[0] != reserved[1]


def test_find_completed_episode_artifact_is_gone() -> None:
    assert not hasattr(EpisodeLifecycleRepository, "find_completed_episode_artifact")
    assert hasattr(EpisodeLifecycleRepository, "linked_twin_artifact")


def test_record_cypher_is_gated_on_the_anchor_marker() -> None:
    assert _RECORD_CYPHER.lstrip().startswith("WITH 1 AS _gate")
    assert (
        "subject_uuid: $episode_uuid"
        in _RECORD_CYPHER.split("MERGE (h:TypedAssertionHead")[0]
    )
