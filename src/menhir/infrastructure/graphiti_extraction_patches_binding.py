"""Binding-support helpers: endpoint-uuid queries and the binding-decision telemetry record.

Extracted verbatim from ``graphiti_extraction_patches``; that module re-exports everything here.
The binding decision itself (``_record_self_binding`` / ``_bind_subject_endpoint``) stays on the
facade module because its module globals are the tests' monkeypatch seams.
"""

from __future__ import annotations

import logging
from typing import Any

from menhir.infrastructure.graphiti_extraction_patches_receipt import CombinedExtractionReceipt
from menhir.infrastructure.self_binding import SelfBindResult

logger = logging.getLogger(__name__)


def _edge_endpoint_uuids(edge: Any) -> set[str]:
    return {str(getattr(edge, "source_node_uuid", "") or ""),
            str(getattr(edge, "target_node_uuid", "") or "")} - {""}


def _record_self_binding_decision(
    result: SelfBindResult, receipt: CombinedExtractionReceipt
) -> None:
    try:
        from menhir.infrastructure.telemetry.recorders import record_lifecycle_event

        record_lifecycle_event(
            component="self_binding",
            event="canonical_self_decision",
            state=str(result.outcome),
            episode_uuid=receipt.episode_key or None,
            details=result.telemetry_details(receipt.self_identity),
        )
    except Exception:  # noqa: BLE001 - observability must never fail an ingest
        logger.exception("Failed to record canonical-self binding telemetry")
