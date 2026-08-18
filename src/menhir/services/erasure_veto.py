"""Erasure read-suppression veto (CF-165 Phase F).

A durable erasure saga guarantees the purge eventually completes, but by itself it does not stop
another live Menhir process from reading still-unpurged content in the window between "erasure
intent committed" and "physical purge finished". This module centralizes the decision that a
PREPARED erasure must act as an immediate READ VETO: once the intent is durable, reads of those
subjects are suppressed even though their rows may still exist.

Centralizing here means individual callers cannot forget the check -- they either filter rows,
ask explicitly, or refuse to expose a single erased subject. The module is deliberately decoupled
from any particular store via the :class:`LiveErasureLookup` protocol, so it is trivial to test
and to wire against whatever subject store a deployment provides.

Design choices, both deliberate:

* **Short-lived instances.** An :class:`ErasureVeto` memoizes lookup results in an in-instance
  cache, so one instance must be a SHORT-LIVED, per-request object and must NOT be cached across
  requests -- otherwise it would keep serving a stale "not suppressed" answer after an erasure
  begins mid-deployment. The dataclass is frozen, so the cache is a mutable ``field(default_factory=dict)``
  that is mutated in place rather than reassigned.
* **Fail-closed lookups.** If ``lookup.has_live_erasure`` raises, the subject is treated as
  SUPPRESSED and the exception path suppresses rather than exposes. A veto that failed open would
  leak exactly the content this module exists to hide. The failure is logged at warning level.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol

logger = logging.getLogger(__name__)

# Must match ErasureSubjectStore.SUBJECT_TYPES exactly. That store raises on an unknown
# subject_type, and this veto fails CLOSED, so a casing mismatch here would not surface as a
# lookup error -- it would make every read look suppressed. Named constants so the two
# vocabularies cannot drift apart silently again.
SUBJECT_TYPE_NODE = "NODE_UUID"
SUBJECT_TYPE_NAMESPACE = "NAMESPACE"


class LiveErasureLookup(Protocol):
    """A store query answering whether a subject currently has a live (unpurged) erasure."""

    def has_live_erasure(self, *, subject_type: str, subject_value: str) -> bool:
        """Return True when ``subject_value`` of ``subject_type`` is under an active erasure."""
        ...


class ErasedSubjectError(Exception):
    """Raised when a read targets a subject that is under an active (unpurged) erasure.

    Raised by :meth:`ErasureVeto.assert_readable` for explicit single-subject getters, where
    returning ``None`` would be indistinguishable from "not found".
    """


@dataclass(frozen=True)
class ErasureVeto:
    """Decides whether reads of a subject are suppressed by a live erasure.

    Because results are cached in-instance, an instance is a SHORT-LIVED, per-request object.
    Do NOT retain or reuse it across requests, or it will keep serving a stale "not suppressed"
    answer after an erasure begins. Pass ``cache_enabled=False`` when the instance must always
    consult the live store.
    """

    lookup: LiveErasureLookup
    cache_enabled: bool = True
    _cache: dict[tuple[str, str], bool] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def _is_suppressed(self, subject_type: str, subject_value: str) -> bool:
        key = (subject_type, subject_value)
        if self.cache_enabled:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
        try:
            suppressed = self.lookup.has_live_erasure(
                subject_type=subject_type, subject_value=subject_value
            )
        except Exception:
            # Fail-closed: a lookup that raises must not expose content we are meant to hide.
            # Treat the subject as suppressed rather than leak it.
            logger.warning(
                "Erasure lookup failed for subject_type=%r subject_value=%r; treating as "
                "suppressed (fail-closed).",
                subject_type,
                subject_value,
                exc_info=True,
            )
            suppressed = True
        if self.cache_enabled:
            self._cache[key] = suppressed
        return suppressed

    def is_suppressed(
        self, *, node_uuid: str | None = None, namespace: str | None = None
    ) -> bool:
        """Return True when the node or its namespace has a live erasure.

        A node inside a namespace being erased is suppressed even when the node itself was never
        named individually -- that is the load-bearing case for a namespace erase. Blank/None
        values are not treated as a match and issue no lookup.
        """
        if node_uuid:
            if self._is_suppressed(SUBJECT_TYPE_NODE, node_uuid):
                return True
        if namespace:
            if self._is_suppressed(SUBJECT_TYPE_NAMESPACE, namespace):
                return True
        return False

    def filter_rows(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        uuid_key: str = "uuid",
        namespace_key: str = "group_id",
    ) -> list[dict[str, Any]]:
        """Drop every row whose uuid or namespace is suppressed.

        Missing keys never raise: a row lacking both keys passes through unchanged. Returned rows
        are plain dicts. This module must never become a way to silently drop unrelated data.
        """
        kept: list[dict[str, Any]] = []
        for row in rows:
            node_uuid = row.get(uuid_key)
            namespace = row.get(namespace_key)
            if self.is_suppressed(node_uuid=node_uuid, namespace=namespace):
                continue
            kept.append(dict(row))
        return kept

    def assert_readable(
        self, *, node_uuid: str | None = None, namespace: str | None = None
    ) -> None:
        """Raise :class:`ErasedSubjectError` when the subject is suppressed, else do nothing.

        Intended for explicit single-subject getters, where returning ``None`` would be
        indistinguishable from "not found".
        """
        if self.is_suppressed(node_uuid=node_uuid, namespace=namespace):
            raise ErasedSubjectError(
                f"Read suppressed: subject is under a live erasure "
                f"(node_uuid={node_uuid!r}, namespace={namespace!r})."
            )


__all__ = [
    "ErasedSubjectError",
    "ErasureVeto",
    "LiveErasureLookup",
]
