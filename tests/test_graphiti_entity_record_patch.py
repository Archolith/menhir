"""Regression tests for malformed stored Graphiti entity records."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
from graphiti_core.driver.driver import GraphProvider

from menhir.infrastructure.graphiti_patches import (
    _MALFORMED_ENTITY_DATES_LOGGED,
    _MALFORMED_ENTITY_GROUP_IDS_LOGGED,
    _patch_graphiti_entity_record_group_id,
)


@pytest.mark.unit
def test_entity_record_patch_infers_named_namespace_and_logs(caplog) -> None:
    import graphiti_core.search.search_utils as search_utils

    uuid = "missing-group-id-test"
    _MALFORMED_ENTITY_GROUP_IDS_LOGGED.discard(uuid)
    _patch_graphiti_entity_record_group_id()
    record = {
        "uuid": uuid,
        "name": "legacy entity",
        "name_embedding": None,
        "group_id": None,
        "labels": ["Entity"],
        "created_at": datetime.now(timezone.utc),
        "summary": "legacy",
        "attributes": {"namespace": "home-media"},
    }

    with caplog.at_level(logging.ERROR):
        node = search_utils.get_entity_node_from_record(record, GraphProvider.NEO4J)

    assert node.group_id == "home-media"
    assert record["group_id"] is None
    assert record["attributes"] == {"namespace": "home-media"}
    assert uuid in caplog.text
    assert "NULL group_id" in caplog.text


@pytest.mark.unit
def test_entity_record_patch_maps_default_namespace_to_empty_group() -> None:
    import graphiti_core.search.search_utils as search_utils

    _patch_graphiti_entity_record_group_id()
    node = search_utils.get_entity_node_from_record(
        {
            "uuid": "missing-default-group-id-test",
            "name": "legacy default entity",
            "name_embedding": None,
            "group_id": None,
            "labels": ["Entity"],
            "created_at": datetime.now(timezone.utc),
            "summary": "legacy",
            "attributes": {"namespace": "default"},
        },
        GraphProvider.NEO4J,
    )

    assert node.group_id == ""


@pytest.mark.unit
def test_entity_record_patch_normalizes_legacy_utc_suffix_and_logs(caplog) -> None:
    import graphiti_core.search.search_utils as search_utils

    uuid = "malformed-created-at-test"
    _MALFORMED_ENTITY_DATES_LOGGED.discard(uuid)
    _patch_graphiti_entity_record_group_id()
    with caplog.at_level(logging.ERROR):
        node = search_utils.get_entity_node_from_record(
            {
                "uuid": uuid,
                "name": "legacy date entity",
                "name_embedding": None,
                "group_id": "",
                "labels": ["Entity"],
                "created_at": "2026-06-15T23:33:56.712109Z[UTC]",
                "summary": "legacy",
                "attributes": {"namespace": "default"},
            },
            GraphProvider.NEO4J,
        )

    assert node.created_at == datetime(2026, 6, 15, 23, 33, 56, 712109, tzinfo=timezone.utc)
    assert uuid in caplog.text
    assert "non-ISO created_at" in caplog.text
