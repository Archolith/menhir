from __future__ import annotations

import json

import pytest

from menhir.domain.projection import ProjectionTarget
from menhir.domain.projection_lifecycle import ProjectionLifecycleCorruptionError
from menhir.infrastructure.projection_lifecycle_repository import ProjectionLifecycleRepository
from menhir.infrastructure.realization_coverage_repository import (
    RealizationLifecycleRepository,
    ScalarStateProjectionHashSource,
)
from menhir.services.scalar_projection_definition import SCALAR_STATE_PROJECTION


class ScriptedNeo4j:
    def __init__(self, responses: list[list[dict[str, object]]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(
        self,
        query: str,
        params: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        self.calls.append((query, dict(params or {})))
        if not self.responses:
            raise AssertionError(f"unexpected query: {query}")
        return [dict(row) for row in self.responses.pop(0)]


def _target() -> ProjectionTarget:
    return ProjectionTarget(
        namespace="tenant-a",
        subject_id="entity-1",
        key=("height", "", "number", "cm"),
    )


def _target_json(target: ProjectionTarget) -> str:
    return json.dumps(
        {
            "namespace": target.namespace,
            "subject_id": target.subject_id,
            "key": list(target.key),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


@pytest.mark.unit
def test_lifecycle_target_snapshot_rehydrates_and_orders_current_work() -> None:
    target_b = ProjectionTarget(namespace="tenant-a", subject_id="b", key=("state",))
    target_a = ProjectionTarget(namespace="tenant-a", subject_id="a", key=("state",))
    rows = []
    for target in (target_b, target_a):
        rows.append(
            {
                "work_key": ProjectionLifecycleRepository.work_key(
                    SCALAR_STATE_PROJECTION.definition_id, target
                ),
                "definition_id": SCALAR_STATE_PROJECTION.definition_id,
                "definition_version": SCALAR_STATE_PROJECTION.version,
                "current_definition_version": SCALAR_STATE_PROJECTION.version,
                "target_json": _target_json(target),
                "generation": 3,
                "target_present": target is target_b,
                "reason": "target_set_reconciled",
            }
        )
    repository = RealizationLifecycleRepository(ScriptedNeo4j([rows]))

    tokens = repository.targets_for_definition(SCALAR_STATE_PROJECTION.definition_id)

    assert [token.target.subject_id for token in tokens] == ["a", "b"]
    assert tokens[0].target_present is False
    assert tokens[1].target_present is True


@pytest.mark.unit
def test_lifecycle_target_snapshot_fails_closed_on_wrong_current_version() -> None:
    target = _target()
    row = {
        "work_key": ProjectionLifecycleRepository.work_key(
            SCALAR_STATE_PROJECTION.definition_id, target
        ),
        "definition_id": SCALAR_STATE_PROJECTION.definition_id,
        "definition_version": 1,
        "current_definition_version": 2,
        "target_json": _target_json(target),
        "generation": 1,
        "target_present": True,
        "reason": "assertion_written",
    }
    repository = RealizationLifecycleRepository(ScriptedNeo4j([[row]]))

    with pytest.raises(ProjectionLifecycleCorruptionError, match="version disagrees"):
        repository.targets_for_definition(SCALAR_STATE_PROJECTION.definition_id)


@pytest.mark.unit
def test_scalar_hash_reflects_actual_installed_state_not_expected_membership() -> None:
    target = _target()
    present = {
        "value": 183,
        "valid_at": "2026-08-17T00:00:00+00:00",
        "scalar_contributors": ["b", "a"],
        "scalar_effective_tier": "user",
        "episode_uuids": ["episode-2", "episode-1"],
    }
    fake = ScriptedNeo4j([[present], [present]])
    source = ScalarStateProjectionHashSource(fake)

    expected_present = source.current_projection_hash(
        definition=SCALAR_STATE_PROJECTION,
        target=target,
        target_present=True,
    )
    expected_absent_membership = source.current_projection_hash(
        definition=SCALAR_STATE_PROJECTION,
        target=target,
        target_present=False,
    )

    assert expected_present == expected_absent_membership
    assert fake.calls[0][1]["subject_uuid"] == target.subject_id


@pytest.mark.unit
def test_scalar_hash_has_distinct_canonical_absent_state() -> None:
    target = _target()
    source = ScalarStateProjectionHashSource(ScriptedNeo4j([[], []]))

    absent_when_present_target = source.current_projection_hash(
        definition=SCALAR_STATE_PROJECTION,
        target=target,
        target_present=True,
    )
    absent_when_removed_target = source.current_projection_hash(
        definition=SCALAR_STATE_PROJECTION,
        target=target,
        target_present=False,
    )

    assert absent_when_present_target == absent_when_removed_target
    assert absent_when_present_target is not None


@pytest.mark.unit
def test_scalar_hash_canonicalizes_set_like_provenance_order() -> None:
    target = _target()
    first = {
        "value": 183,
        "valid_at": "2026-08-17T00:00:00+00:00",
        "scalar_contributors": ["b", "a"],
        "scalar_effective_tier": "user",
        "episode_uuids": ["episode-2", "episode-1"],
    }
    second = {
        **first,
        "scalar_contributors": ["a", "b"],
        "episode_uuids": ["episode-1", "episode-2"],
    }
    source = ScalarStateProjectionHashSource(ScriptedNeo4j([[first], [second]]))

    one = source.current_projection_hash(
        definition=SCALAR_STATE_PROJECTION,
        target=target,
        target_present=True,
    )
    two = source.current_projection_hash(
        definition=SCALAR_STATE_PROJECTION,
        target=target,
        target_present=True,
    )

    assert one == two


@pytest.mark.unit
def test_scalar_hash_refuses_duplicate_current_views() -> None:
    target = _target()
    row = {
        "value": 183,
        "valid_at": "2026-08-17T00:00:00+00:00",
        "scalar_contributors": [],
        "scalar_effective_tier": "user",
        "episode_uuids": [],
    }
    source = ScalarStateProjectionHashSource(ScriptedNeo4j([[row, row]]))

    with pytest.raises(ProjectionLifecycleCorruptionError, match="multiple current"):
        source.current_projection_hash(
            definition=SCALAR_STATE_PROJECTION,
            target=target,
            target_present=True,
        )
