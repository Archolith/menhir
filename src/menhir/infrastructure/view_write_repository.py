"""Generic View registration, version writing, and provenance persistence."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from menhir.infrastructure.view_kind_registry import resolve_view_kinds
from menhir.infrastructure.view_models import (
    AdmissionAuditKind,
    CounterKind,
    ScalarHistoryKind,
    ScalarStateKind,
    TimelineKind,
    ViewKind,
    _counter_retrieval_text,
    _normalize_entries,
    _timeline_surface,
)
from menhir.infrastructure.view_write_repository_provenance import ViewFactProvenanceMixin
from menhir.infrastructure.view_write_repository_record import ViewRecordMixin
from menhir.infrastructure.view_write_repository_write import ViewVersionWriteMixin

logger = logging.getLogger(__name__)


class ViewWriteRepositoryMixin(ViewRecordMixin, ViewVersionWriteMixin, ViewFactProvenanceMixin):
    """Single writer for View :Entity nodes (all kinds). Direct Neo4j CRUD.

    The `_write_version` core stamps + supersedes + links provenance identically for every kind;
    the registered `ViewKind` supplies the kind name, value slot, surface, signature, and read
    projection. That split IS the "one View shape, many folds" claim, with each kind as its SSOT."""

    #: registry — add a memory type by adding its ViewKind here, nothing else.
    KINDS: dict[str, ViewKind] = {k.name: k for k in (
        CounterKind(), TimelineKind(), AdmissionAuditKind(), ScalarStateKind(),
        ScalarHistoryKind())}

    def __init__(
        self,
        neo4j: Any,
        *,
        kinds: Mapping[str, ViewKind] | None = None,
    ) -> None:
        self.neo4j = neo4j
        self.KINDS = resolve_view_kinds(self.KINDS, kinds)

    # ------------------------------------------------------------------ keys / surfaces (compat)

    @staticmethod
    def _key(namespace: str | None, subject: str, discriminator: str,
             *, subject_uuid: str | None = None) -> str:
        """Build the view_key. Identity is the text subject by default (every existing kind); when
        `subject_uuid` is supplied (scalar_state), the resolved entity UUID is the identity segment
        instead, so the key is entity-anchored, not text-anchored. A present-but-blank UUID is a bug
        (never silently fall back to text keying — that would recreate the rejected lexical sidecar)."""
        # Keep the persisted View identity byte-compatible with deployed rows.  Logical default
        # aliases are unified by tenant-scope reads and the namespace fence, but changing this
        # segment from ``default`` to ``""`` needs a staged data migration; doing it in the shared
        # writer would otherwise create a second current View beside an existing ``default::`` row.
        ns = (namespace or "").strip()
        if subject_uuid is not None:
            ident = subject_uuid.strip()
            if not ident:
                raise ValueError("subject_uuid must be non-blank when provided (scalar-state identity)")
            subj_seg = ident.lower()
        else:
            subj_seg = subject.strip().lower()
        return f"{ns}::{subj_seg}::{discriminator.strip().lower()}"

    @staticmethod
    def retrieval_text(subject: str, counter: str, value: float) -> str:
        """Counter BM25/embedding surface (kept as a static for callers that pre-embed it)."""
        return _counter_retrieval_text(subject, counter, value)

    @staticmethod
    def _timeline_surface(subject: str, entries: list[dict[str, Any]]) -> str:
        return _timeline_surface(subject, _normalize_entries(entries))
