"""Public View repository assembled from focused model and persistence owners."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from menhir.infrastructure.scalar_view_repository import ScalarViewRepositoryMixin
from menhir.infrastructure.view_models import (
    AdmissionAuditKind,
    CounterKind,
    ScalarHistoryKind,
    ScalarStateKind,
    TimelineKind,
    ViewClass,
    ViewKind,
    _COUNT_TOKEN,
    _checked_template,
    _is_older,
    _label_for,
    _normalize_episode_uuids,
    _parse_dt,
    _scalar_norm,
)
from menhir.infrastructure.view_query_repository import ViewQueryRepositoryMixin
from menhir.infrastructure.view_write_repository import ViewWriteRepositoryMixin


class ViewRepository(
    ViewWriteRepositoryMixin, ScalarViewRepositoryMixin, ViewQueryRepositoryMixin
):
    """Neo4j View repository composed from focused operation families.

    ``kinds`` is an optional complete registry for this repository instance.  Omitting it preserves
    the built-in ``ViewWriteRepositoryMixin.KINDS`` universe exactly.  Supplying it lets an extension
    add or replace ViewKind definitions without mutating the process-wide class registry or editing
    the shared writer/query mixins.
    """

    def __init__(
        self,
        neo4j: Any,
        *,
        kinds: Mapping[str, ViewKind] | None = None,
    ) -> None:
        self.neo4j = neo4j
        resolved = dict(self.KINDS if kinds is None else kinds)
        for registered_name, kind in resolved.items():
            if registered_name != kind.name:
                raise ValueError(
                    f"ViewKind registry key {registered_name!r} does not match "
                    f"kind.name {kind.name!r}"
                )
        # Instance-local copy: callers may reuse or mutate their input mapping without changing this
        # repository, and the inherited class-level KINDS remains the exact built-in compatibility
        # surface for existing code that inspects ViewRepository.KINDS.
        self.KINDS = resolved
