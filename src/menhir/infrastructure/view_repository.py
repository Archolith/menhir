"""Public View repository assembled from focused model and persistence owners."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from menhir.infrastructure.scalar_view_repository import ScalarViewRepositoryMixin
from menhir.infrastructure.view_models import (
    AdmissionAuditKind, CounterKind, ScalarHistoryKind, ScalarStateKind, TimelineKind,
    ViewClass, ViewKind, _COUNT_TOKEN, _checked_template, _is_older, _label_for,
    _normalize_episode_uuids, _parse_dt, _scalar_norm,
)
from menhir.infrastructure.view_query_repository import ViewQueryRepositoryMixin
from menhir.infrastructure.view_write_repository import ViewWriteRepositoryMixin

if TYPE_CHECKING:
    from menhir.extensions import RuntimeExtensions


class ViewRepository(
    ViewWriteRepositoryMixin, ScalarViewRepositoryMixin, ViewQueryRepositoryMixin
):
    """Neo4j View repository composed from focused operation families."""


def build_view_repository(
    graph: Any, *, extensions: RuntimeExtensions | None = None,
) -> ViewRepository:
    """Construct a product View repository from the host's resolved kind map."""
    from menhir.extensions import resolve_graph_extensions

    registry = resolve_graph_extensions(extensions, bind=True)
    return ViewRepository(graph, kinds=registry.view_kinds if registry else None)
