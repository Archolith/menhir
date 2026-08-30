"""Public View repository assembled from focused model and persistence owners."""

from menhir.infrastructure.scalar_view_repository import ScalarViewRepositoryMixin
from menhir.infrastructure.view_models import (
    AdmissionAuditKind, CounterKind, ScalarHistoryKind, ScalarStateKind, TimelineKind,
    ViewClass, ViewKind, _COUNT_TOKEN, _checked_template, _is_older, _label_for,
    _normalize_episode_uuids, _parse_dt, _scalar_norm,
)
from menhir.infrastructure.view_query_repository import ViewQueryRepositoryMixin
from menhir.infrastructure.view_write_repository import ViewWriteRepositoryMixin


class ViewRepository(
    ViewWriteRepositoryMixin, ScalarViewRepositoryMixin, ViewQueryRepositoryMixin
):
    """Neo4j View repository composed from focused operation families."""
