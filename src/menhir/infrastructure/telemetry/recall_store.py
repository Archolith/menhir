"""Recall feedback, aggregate diagnostics, and client-session telemetry operations."""

from __future__ import annotations

from menhir.infrastructure.telemetry.recall_store_archive import TelemetryRecallArchiveMixin
from menhir.infrastructure.telemetry.recall_store_diagnostics import TelemetryDiagnosticsMixin
from menhir.infrastructure.telemetry.recall_store_folds import TelemetryRecallFoldsMixin
from menhir.infrastructure.telemetry.recall_store_receipts import TelemetryRecallReceiptsMixin
from menhir.infrastructure.telemetry.recall_store_sessions import TelemetryClientSessionsMixin


class TelemetryRecallStoreMixin(
    TelemetryRecallReceiptsMixin,
    TelemetryRecallFoldsMixin,
    TelemetryDiagnosticsMixin,
    TelemetryClientSessionsMixin,
    TelemetryRecallArchiveMixin,
):
    """Facade combining the recall-store operation mixins split into sibling recall_store_*.py modules."""
