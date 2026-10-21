"""Additive schema DDL for the durable projection lifecycle sidecars.

Moved verbatim from :mod:`menhir.infrastructure.projection_lifecycle_repository`, which
still re-exports both names.
"""

from __future__ import annotations

PROJECTION_LIFECYCLE_SCHEMA_QUERIES: tuple[str, ...] = (
    "CREATE CONSTRAINT projection_definition_state_id_unique IF NOT EXISTS "
    "FOR (d:ProjectionDefinitionState) REQUIRE d.definition_id IS UNIQUE",
    "CREATE CONSTRAINT projection_work_state_key_unique IF NOT EXISTS "
    "FOR (w:ProjectionWorkState) REQUIRE w.work_key IS UNIQUE",
    "CREATE INDEX projection_work_state_definition_idx IF NOT EXISTS "
    "FOR (w:ProjectionWorkState) ON (w.definition_id)",
    "CREATE CONSTRAINT projection_derivation_receipt_key_unique IF NOT EXISTS "
    "FOR (r:ProjectionDerivationReceipt) REQUIRE r.receipt_key IS UNIQUE",
    "CREATE INDEX projection_derivation_receipt_derivation_idx IF NOT EXISTS "
    "FOR (r:ProjectionDerivationReceipt) ON (r.derivation_id)",
)


def projection_lifecycle_schema_queries() -> list[str]:
    """Return the additive DDL required by the projection lifecycle sidecars."""

    return list(PROJECTION_LIFECYCLE_SCHEMA_QUERIES)
