"""Verify the three telemetry mixin modules bind a module-level logger.

Regression test for the Critical bug where ``event_store``, ``lifecycle_store``,
and ``recall_store`` each called ``logger.warning(...)`` in ``except sqlite3.Error``
handlers without ever binding ``logger``. On any SQLite failure the handler raised
``NameError`` (masking the real error) instead of degrading. These tests confirm:

1. each module exposes a module-level ``logging.Logger`` named after its ``__name__``, and
2. the failure path (``fetch_merge_audit``) reports unavailability instead of
   raising ``NameError`` when a read fails.
"""

import logging
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from menhir.infrastructure.telemetry import event_store, lifecycle_store, recall_store
from menhir.infrastructure.telemetry.lifecycle_store import MergeAuditUnavailable
from menhir.mcp.telemetry import McpTelemetryStore

_MODULES = (event_store, lifecycle_store, recall_store)


@pytest.mark.parametrize("mod", _MODULES, ids=lambda m: m.__name__)
def test_each_module_binds_a_module_logger(mod):
    assert hasattr(mod, "logger"), f"{mod.__name__} does not expose a module-level logger"
    logger = mod.logger
    assert isinstance(logger, logging.Logger)
    assert logger.name == mod.__name__


def test_fetch_merge_audit_reports_a_failed_read_as_unavailable(tmp_path: Path):
    """A SQLite read failure must surface as unavailable -- not NameError, and not ``[]``.

    Two regressions in one test, because the fix for the first created the second.

    * CF-1: the handler called ``logger.warning`` without binding ``logger``, so it raised
      ``NameError`` and masked the real error. It must not do that again.
    * CF-205: binding the logger then made the swallow complete, and returning ``[]`` handed
      callers a *worse* answer than a crash -- ``[]`` is their evidence that no snapshot
      exists, and ``legacy_unmerge_coordinator`` turns it into "this merge is NOT
      recoverable". A failed read is not evidence of absence.
    """
    db_path = tmp_path / "mcp_telemetry.db"
    store = McpTelemetryStore(db_path=db_path)
    store._ensure_ready()  # initialise so only the read path is exercised

    def _fail_connect():
        raise sqlite3.OperationalError("database is locked")

    with patch.object(store, "_connect", side_effect=_fail_connect):
        with pytest.raises(MergeAuditUnavailable) as excinfo:
            store.fetch_merge_audit(survivor_uuid="n1")

    # The original error is preserved rather than replaced -- the CF-1 masking failure.
    assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)
    assert not isinstance(excinfo.value, NameError)
