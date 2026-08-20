"""CF-178(b): the malformed-entity log-once sets must not grow without bound.

`_MALFORMED_ENTITY_GROUP_IDS_LOGGED` and `_MALFORMED_ENTITY_DATES_LOGGED` are keyed by entity
`uuid` inside `_safe_entity_node_from_record`, which patches
`graphiti_core.search.search_utils.get_entity_node_from_record` and so runs once per record of
every node search. Their cardinality is bounded only by the malformed-entity population.

These are module-level globals imported BY REFERENCE by `graphiti_patches`, and the suite runs
with `-n 8`, so every test here restores them.
"""

from __future__ import annotations

import pytest

from menhir.infrastructure import graphiti_model_patches as patches


@pytest.fixture(autouse=True)
def _isolate_log_sets():
    saved = (
        set(patches._MALFORMED_ENTITY_GROUP_IDS_LOGGED),
        set(patches._MALFORMED_ENTITY_DATES_LOGGED),
    )
    patches._MALFORMED_ENTITY_GROUP_IDS_LOGGED.clear()
    patches._MALFORMED_ENTITY_DATES_LOGGED.clear()
    yield
    patches._MALFORMED_ENTITY_GROUP_IDS_LOGGED.clear()
    patches._MALFORMED_ENTITY_GROUP_IDS_LOGGED.update(saved[0])
    patches._MALFORMED_ENTITY_DATES_LOGGED.clear()
    patches._MALFORMED_ENTITY_DATES_LOGGED.update(saved[1])


@pytest.mark.unit
def test_first_offer_is_new_and_repeat_offer_is_not() -> None:
    """POSITIVE CONTROL: without this, the cap test would pass against a helper storing nothing."""
    seen = patches._MALFORMED_ENTITY_GROUP_IDS_LOGGED

    assert patches._first_time_seen(seen, "uuid-a") is True
    assert patches._first_time_seen(seen, "uuid-a") is False, "must dedupe, that is its whole job"
    assert patches._first_time_seen(seen, "uuid-b") is True
    assert seen == {"uuid-a", "uuid-b"}


@pytest.mark.unit
def test_unbounded_uuid_cardinality_stays_capped() -> None:
    seen = patches._MALFORMED_ENTITY_DATES_LOGGED
    cap = patches._MALFORMED_LOG_KEYS_MAX

    for i in range(cap * 3):
        patches._first_time_seen(seen, f"uuid-{i}")

    assert len(seen) <= cap


@pytest.mark.unit
def test_eviction_removes_exactly_one_key() -> None:
    seen = patches._MALFORMED_ENTITY_GROUP_IDS_LOGGED
    cap = patches._MALFORMED_LOG_KEYS_MAX

    for i in range(cap):
        patches._first_time_seen(seen, f"uuid-{i}")
    assert len(seen) == cap

    assert patches._first_time_seen(seen, "uuid-OVERFLOW") is True
    assert len(seen) == cap, "one in, exactly one out"
    assert "uuid-OVERFLOW" in seen


@pytest.mark.unit
def test_eviction_never_fires_for_a_key_already_present() -> None:
    """At capacity, re-offering a KNOWN key must not evict anything -- it adds no entry."""
    seen = patches._MALFORMED_ENTITY_DATES_LOGGED
    cap = patches._MALFORMED_LOG_KEYS_MAX

    for i in range(cap):
        patches._first_time_seen(seen, f"uuid-{i}")
    snapshot = set(seen)

    assert patches._first_time_seen(seen, next(iter(snapshot))) is False
    assert seen == snapshot


@pytest.mark.unit
def test_the_shared_object_identity_is_preserved_for_importers() -> None:
    """`graphiti_patches` imports these sets BY REFERENCE; eviction must mutate, never rebind."""
    from menhir.infrastructure import graphiti_patches

    assert (
        graphiti_patches._MALFORMED_ENTITY_GROUP_IDS_LOGGED
        is patches._MALFORMED_ENTITY_GROUP_IDS_LOGGED
    )
    before = patches._MALFORMED_ENTITY_GROUP_IDS_LOGGED
    for i in range(patches._MALFORMED_LOG_KEYS_MAX + 5):
        patches._first_time_seen(patches._MALFORMED_ENTITY_GROUP_IDS_LOGGED, f"uuid-{i}")

    assert patches._MALFORMED_ENTITY_GROUP_IDS_LOGGED is before, "must not rebind the global"
    assert graphiti_patches._MALFORMED_ENTITY_GROUP_IDS_LOGGED is before
