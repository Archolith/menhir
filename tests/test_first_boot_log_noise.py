"""First boot must not print a wall of benign ERROR/WARNING lines.

Two sources, both seen in a fresh-install walkthrough: Neo4j ``01N52`` notifications for
properties that do not exist yet on an empty graph, and Graphiti's
``EquivalentSchemaRuleAlreadyExists`` errors when its index shapes collide with Menhir's.
"""

from __future__ import annotations

import logging

import pytest

from menhir.infrastructure import graphiti_client as gc
from menhir.infrastructure.neo4j import DRIVER_NOTIFICATION_CONFIG, Neo4jRepository

pytestmark = [pytest.mark.unit]


def test_menhir_drivers_disable_unrecognized_notifications_only(monkeypatch: pytest.MonkeyPatch) -> None:
    assert DRIVER_NOTIFICATION_CONFIG == {"notifications_disabled_classifications": ["UNRECOGNIZED"]}

    seen: dict[str, object] = {}

    class _FakeGraphDatabase:
        @staticmethod
        def driver(uri, **kwargs):
            seen.update(kwargs)
            return object()

    import menhir.infrastructure.neo4j as neo4j_module

    monkeypatch.setattr(neo4j_module, "GraphDatabase", _FakeGraphDatabase)
    repo = Neo4jRepository(uri="bolt://x", database="neo4j", user="u", password="p")
    repo._get_driver()
    assert seen["notifications_disabled_classifications"] == ["UNRECOGNIZED"]
    assert "notifications_min_severity" not in seen, "other classifications must remain visible"


def test_equivalent_index_errors_are_downgraded_during_index_build(caplog: pytest.LogCaptureFixture) -> None:
    target = logging.getLogger("graphiti_core.driver.neo4j_driver")
    with caplog.at_level(logging.INFO, logger="graphiti_core.driver.neo4j_driver"):
        with gc._quiet_equivalent_index_errors():
            target.error(
                "Error executing Neo4j query: {neo4j_code: Neo.ClientError.Schema."
                "EquivalentSchemaRuleAlreadyExists} An equivalent index already exists"
            )
            target.error("Error executing Neo4j query: something genuinely broken")
        # The filter is process-wide now: Graphiti's constructor fires these before the
        # build_indices_and_constraints wrapper runs, so a scoped filter missed them.
        target.error("Neo.ClientError.Schema.EquivalentSchemaRuleAlreadyExists after build")

    messages = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert ("ERROR", "Error executing Neo4j query: something genuinely broken") in messages
    assert not any(lvl == "ERROR" and "EquivalentSchemaRuleAlreadyExists" in msg for lvl, msg in messages)


def test_mcp_server_info_reports_menhir_version_not_the_sdk() -> None:
    import menhir
    from menhir.api.mcp_remote import create_mcp_streamable_http_app

    _app, server = create_mcp_streamable_http_app()
    options = server._mcp_server.create_initialization_options()
    assert options.server_version == menhir.__version__


def test_unknown_property_key_notifications_are_dropped_for_every_driver(caplog: pytest.LogCaptureFixture) -> None:
    """Graphiti opens its own Neo4j driver; the logger-level filter covers it too."""
    from menhir.infrastructure.logging_config import install_neo4j_notification_filter

    install_neo4j_notification_filter()
    install_neo4j_notification_filter()  # idempotent
    target = logging.getLogger("neo4j.notifications")
    with caplog.at_level(logging.WARNING, logger="neo4j.notifications"):
        target.warning("Received notification from DBMS server: gql_status='01N52' ... The property `fact_embedding` does not exist.")
        target.warning("Received notification from DBMS server: gql_status='01N00' deprecated feature used")
    messages = [r.getMessage() for r in caplog.records]
    assert not any("01N52" in m for m in messages)
    assert any("deprecated feature" in m for m in messages)


def test_configure_logging_installs_the_equivalent_index_filter_process_wide(caplog: pytest.LogCaptureFixture) -> None:
    from menhir.infrastructure.logging_config import EquivalentIndexFilter, install_neo4j_notification_filter

    install_neo4j_notification_filter()  # what configure_logging calls
    target = logging.getLogger("graphiti_core.driver.neo4j_driver")
    assert sum(isinstance(f, EquivalentIndexFilter) for f in target.filters) == 1
    with caplog.at_level(logging.INFO, logger="graphiti_core.driver.neo4j_driver"):
        target.error("Error executing Neo4j query: {neo4j_code: Neo.ClientError.Schema.EquivalentSchemaRuleAlreadyExists} 'invalid_at_edge_index'")
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)
