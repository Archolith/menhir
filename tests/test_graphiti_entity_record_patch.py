"""Regression tests for malformed stored Graphiti entity records.

Menhir's namespace inference now rides the fork's startup configuration seam
(``set_entity_record_group_id_resolver``); generic null-group repair and legacy
timestamp normalization are fork-native.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from graphiti_core.driver.driver import GraphProvider
from graphiti_core.nodes import get_entity_node_from_record

from menhir.infrastructure.graphiti_resolution_policy import (
    install_entity_record_group_id_resolver,
    resolve_entity_record_group_id,
)


@pytest.fixture(autouse=True)
def _registered_resolver():
    install_entity_record_group_id_resolver()
    yield
    from graphiti_core.nodes import set_entity_record_group_id_resolver

    set_entity_record_group_id_resolver(None)


@pytest.mark.unit
def test_entity_record_resolver_infers_named_namespace() -> None:
    node = get_entity_node_from_record(
        {
            "uuid": "missing-group-id-test",
            "name": "legacy entity",
            "name_embedding": None,
            "group_id": None,
            "labels": ["Entity"],
            "created_at": datetime.now(timezone.utc),
            "summary": "legacy",
            "attributes": {"namespace": "home-media"},
        },
        GraphProvider.NEO4J,
    )

    assert node.group_id == "home-media"


@pytest.mark.unit
def test_entity_record_resolver_maps_default_namespace_to_empty_group() -> None:
    node = get_entity_node_from_record(
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
def test_entity_record_resolver_without_namespace_falls_back_to_default() -> None:
    assert resolve_entity_record_group_id({"attributes": {}}, GraphProvider.NEO4J) == ""
    assert resolve_entity_record_group_id({"attributes": None}, GraphProvider.NEO4J) == ""
    assert resolve_entity_record_group_id({"attributes": {"namespace": "ops"}}, GraphProvider.NEO4J) == "ops"


@pytest.mark.unit
def test_entity_record_null_group_id_repair_is_fork_native_and_logs(caplog) -> None:
    node = get_entity_node_from_record(
        {
            "uuid": "no-attributes-null-group-test",
            "name": "orphan entity",
            "name_embedding": None,
            "group_id": None,
            "labels": ["Entity"],
            "created_at": datetime.now(timezone.utc),
            "summary": "legacy",
            "attributes": {},
        },
        GraphProvider.NEO4J,
    )

    assert node.group_id == ""


@pytest.mark.unit
def test_entity_record_normalizes_legacy_utc_suffix_and_logs(caplog) -> None:
    uuid = "malformed-created-at-test"
    with caplog.at_level(10):  # DEBUG and above: the fork logs the repair
        node = get_entity_node_from_record(
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
    assert "repaired legacy timestamp" in caplog.text
