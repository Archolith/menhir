"""CF-22: the daily orphan sweep must not delete an episode for being isolated.

`cleanup_orphan_episodes` issued a `DETACH DELETE` against `:Episodic` nodes whose only
disqualifying condition was having no `:Entity` neighbour -- no age bound, no journal -- from the
default-on daily consolidation job (`lifecycle_consolidation_enabled: bool = True`).

Two siblings forbid exactly that inference by name:

    services/delete_coordinator.py   "Evidence that becomes unreferenced is REPORTED, never
                                      deleted. Isolation is not authorization -- that inference
                                      is what caused the incident above."
    services/lifecycle_decay.py      "A normal decay sweep must never delete a node for being
                                      isolated; an isolated node ... is benign."

The blast radius was specific: an episode whose extraction produced no entities is READY,
unflagged, and edgeless, because `stamp_and_finalize` marks that state READY as a *success*. So
the rows most likely to be destroyed were ones the pipeline considered correctly processed, and
their text is unrecoverable -- recall searches `:Entity`, and an `:Episodic` node is not one.

These tests assert the SHAPE OF THE QUERY, which is what this repository method actually produces.
The pre-existing suite tests it the same way (see `tests/test_memory_graph_adapter_methods.py`).
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from menhir.infrastructure.episode_maintenance import (
    _ORPHAN_EPISODE_MIN_AGE_DAYS,
    EpisodeMaintenanceRepository,
)


class _StubNeo4j:
    def __init__(self, responses: list[list[dict[str, Any]]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = responses or [[]]

    def execute(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append({"query": query, "params": params})
        return self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]


def _repo(responses: list[list[dict[str, Any]]] | None = None) -> EpisodeMaintenanceRepository:
    repo = EpisodeMaintenanceRepository()
    repo.neo4j = _StubNeo4j(responses)
    return repo


def _query(repo: EpisodeMaintenanceRepository) -> str:
    return repo.neo4j.calls[0]["query"]


@pytest.mark.unit
def test_an_episode_that_still_has_text_is_never_eligible() -> None:
    """The core of the finding: content must never be destroyed on an isolation inference."""
    repo = _repo()
    repo.cleanup_orphan_episodes("session-1")

    assert "coalesce(trim(n.content), '') = ''" in _query(repo), (
        "the sweep must exclude episodes that still carry text"
    )


@pytest.mark.unit
def test_the_delete_is_age_bounded() -> None:
    """Without a bound, a just-processed episode is reachable by a sweep that runs daily."""
    repo = _repo()
    repo.cleanup_orphan_episodes("session-1")

    assert f"duration({{days: {_ORPHAN_EPISODE_MIN_AGE_DAYS}}})" in _query(repo)
    assert "n.created_at < datetime() -" in _query(repo)


@pytest.mark.unit
def test_the_age_bound_is_a_real_bound() -> None:
    """A zero or negative constant would satisfy the test above while bounding nothing."""
    assert _ORPHAN_EPISODE_MIN_AGE_DAYS >= 1


@pytest.mark.unit
def test_the_deleted_uuids_are_captured_from_the_mutation() -> None:
    """`delete_coordinator` requires the uuid list to come FROM the mutation, not from a
    separate read that could disagree with what was actually destroyed."""
    repo = _repo()
    repo.cleanup_orphan_episodes("session-1")
    query = _query(repo)

    assert "n.uuid AS deleted_uuid" in query
    assert query.index("n.uuid AS deleted_uuid") < query.index("DETACH DELETE n"), (
        "the uuid must be bound BEFORE the delete, or it cannot be returned"
    )
    assert "collect(deleted_uuid) AS deleted_uuids" in query


@pytest.mark.unit
def test_a_delete_is_journalled_with_its_uuids(caplog: pytest.LogCaptureFixture) -> None:
    repo = _repo([[{"deleted": 2, "deleted_uuids": ["ep-a", "ep-b"]}]])

    with caplog.at_level(logging.INFO, logger="menhir.infrastructure.episode_maintenance"):
        result = repo.cleanup_orphan_episodes("session-1")

    assert result == 2
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "ep-a" in logged and "ep-b" in logged, f"deletion not journalled: {logged!r}"


@pytest.mark.unit
def test_nothing_is_logged_when_nothing_was_deleted(caplog: pytest.LogCaptureFixture) -> None:
    """POSITIVE CONTROL for the journal test: it must key off real deletions, not fire always."""
    repo = _repo([[{"deleted": 0, "deleted_uuids": []}]])

    with caplog.at_level(logging.INFO, logger="menhir.infrastructure.episode_maintenance"):
        result = repo.cleanup_orphan_episodes("session-1")

    assert result == 0
    assert not [r for r in caplog.records if "cleanup_orphan_episodes deleted" in r.getMessage()]


@pytest.mark.unit
def test_the_original_safety_conditions_are_still_present() -> None:
    """REGRESSION GUARD: the new conditions must be additive. Dropping any pre-existing one
    would widen the delete while the new tests above still passed."""
    repo = _repo()
    repo.cleanup_orphan_episodes("session-1")
    query = _query(repo)

    for condition in (
        "n.scope = 'SESSION'",
        "n.processing_state = 'READY'",
        "coalesce(n.user_flagged, false) = false",
        "NOT EXISTS { MATCH (n)-[]-(e:Entity) }",
        "n.session_id = $session_id",
    ):
        assert condition in query, f"pre-existing guard lost: {condition}"


@pytest.mark.unit
def test_the_parameter_set_is_unchanged() -> None:
    """The age bound is inlined deliberately so the params stay exactly {session_id}; a
    pre-existing test asserts that dict by equality."""
    repo = _repo()
    repo.cleanup_orphan_episodes("session-1")
    assert repo.neo4j.calls[0]["params"] == {"session_id": "session-1"}

    repo_all = _repo()
    repo_all.cleanup_orphan_episodes(None)
    assert repo_all.neo4j.calls[0]["params"] == {}
    assert "n.session_id = $session_id" not in _query(repo_all)


@pytest.mark.unit
def test_an_empty_result_set_returns_zero() -> None:
    """POSITIVE CONTROL: the method still returns a count and does not raise on no rows."""
    repo = _repo([[]])
    assert repo.cleanup_orphan_episodes("session-1") == 0
    assert repo.neo4j.calls, "the query must still have been issued"
