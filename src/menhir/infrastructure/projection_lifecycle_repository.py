"""Durable projection lifecycle sidecars and generation/version fencing.

This repository owns no assertion semantics and no View payload schema. It stores only the shared
semantic-definition version, durable target membership/work generations, immutable derivation
receipts, and the freshness certificate for the currently applied generation.

The materialization callback runs inside the same Neo4j transaction as the work fence. It receives a
transaction-scoped ``execute`` adapter, so an existing View repository can be instantiated against
that adapter. If the fence fails, any View mutation performed by the callback rolls back with it.
"""

from __future__ import annotations

from typing import Any

from menhir.infrastructure.projection_lifecycle_repository_encoding import MaterializationCallback
from menhir.infrastructure.projection_lifecycle_repository_reads import _ProjectionLifecycleReadsMixin
from menhir.infrastructure.projection_lifecycle_repository_schema import (
    PROJECTION_LIFECYCLE_SCHEMA_QUERIES,
    projection_lifecycle_schema_queries,
)
from menhir.infrastructure.projection_lifecycle_repository_writes import _ProjectionLifecycleWritesMixin

__all__ = [
    "MaterializationCallback",
    "PROJECTION_LIFECYCLE_SCHEMA_QUERIES",
    "ProjectionLifecycleRepository",
    "projection_lifecycle_schema_queries",
]


class ProjectionLifecycleRepository(_ProjectionLifecycleWritesMixin, _ProjectionLifecycleReadsMixin):
    """Neo4j-backed lifecycle sidecar for generic projection definitions."""

    def __init__(self, neo4j: Any) -> None:
        self._neo4j = neo4j
