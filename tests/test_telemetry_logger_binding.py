"""Verify the three telemetry mixin modules bind a module-level logger.

Regression test for the Critical bug where ``event_store``, ``lifecycle_store``,
and ``recall_store`` each called ``logger.warning(...)`` in ``except sqlite3.Error``
handlers without ever binding ``logger``. On any SQLite failure the handler raised
``NameError`` (masking the real error) instead of degrading. These tests confirm:

1. each module exposes a module-level ``logging.Logger`` named after its ``__name__``, and
2. the degradation path (``fetch_merge_audit`` returning ``[]``) now works instead of
   raising ``NameError`` when a read fails.
"""

import logging
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from menhir.infrastructure.telemetry import event_store, lifecycle_store, recall_store
from menhir.mcp.telemetry import McpTelemetryStore

_MODULES = (event_store, lifecycle_store, recall_store)


@pytest.mark.parametrize("mod", _MODULES, ids=lambda m: m.__name__)
def test_each_module_binds_a_module_logger(mod):
    assert hasattr(mod, "logger"), f"{mod.__name__} does not expose a module-level logger"
    logger = mod.logger
    assert isinstance(logger, logging.Logger)
    assert logger.name == mod.__name__


def test_fetch_merge_audit_degrades_on_connect_failure(tmp_path: Path):
    """A SQLite read failure must degrade to [] rather than raise NameError."""
    db_path = tmp_path / "mcp_telemetry.db"
    store = McpTelemetryStore(db_path=db_path)
    store._ensure_ready()  # initialise so only the read path is exercised

    def _fail_connect():
        raise sqlite3.OperationalError("database is locked")

    with patch.object(store, "_connect", side_effect=_fail_connect):
        rows = store.fetch_merge_audit(survivor_uuid="n1")

    assert rows == []
