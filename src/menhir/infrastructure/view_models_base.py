"""Shared View contract and write-path machinery, split from ``view_models.py``.

Holds what every View kind and every View repository share: the storage-class and audience
enums, the closed Cypher-label interface, the ``ViewKind`` SSOT base class, the shared recall
stamps, and the small write-path helpers (provenance normalization, summary-template check,
tolerant timestamp compare). Every symbol here is re-exported from ``view_models.py``, so the
original import path keeps working unchanged.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from menhir.domain.temporal import parse_iso8601

logger = logging.getLogger(__name__)

#: Placeholder for the supporting-event count inside a kind's `summary_template`. The unchanged-value
#: provenance refresh (plan D2) computes the count INSIDE Cypher (the union must be atomic), so the
#: summary cannot be pre-rendered in Python — it is rendered as a template here and the count is
#: substituted server-side in the same statement that writes the union.
_COUNT_TOKEN = "__N__"

# The kind-agnostic recall stamps every View version must carry (mirrors a graphiti :Entity).
_SHARED_STAMPS = (
    "type: 'SEMANTIC', scope: 'PERSISTENT', freshness: 'ACTIVE', user_flagged: false, "
    "edge_count: 0, sharpness: 0.0"
)


class ViewClass(Enum):
    """Storage class for a View node (Metric plan Part A).

    FACT  -> :Entity, a recallable memory (counters, timelines, verifier registers).
    METRIC -> :Metric, operator-only instrumentation, excluded from semantic recall BY LABEL
              (recall matches :Entity), and never carrying a name_embedding or MENTIONS.

    The two classes share the entire versioning machinery (view_key/view_sig/view_current/
    SUPERSEDES); the only differences are the node label, the type stamp, and provenance.
    """

    FACT = "FACT"
    METRIC = "METRIC"


class ViewAudience(Enum):
    """Consumer audience for a materialized View.

    ``RECALL`` Views may enter generic semantic recall once the remaining lifecycle gates pass.
    ``OPERATOR`` Views remain addressable through explicit inspection surfaces but must never enter
    agent context merely because they are stored as ``:Entity`` nodes.
    """

    RECALL = "RECALL"
    OPERATOR = "OPERATOR"


# Allowlisted enum -> Cypher label literal. Neo4j cannot bind a label as a query parameter, so
# these queries interpolate the label -- but ONLY ever from this closed map, NEVER from a caller
# string or free text. That is the plan's "closed label interface" (A1).
_CLASS_LABELS: dict[ViewClass, str] = {ViewClass.FACT: "Entity", ViewClass.METRIC: "Metric"}


def _label_for(view_class: ViewClass) -> str:
    """The allowlisted Cypher label literal for a ViewClass. Raises on anything unknown."""
    label = _CLASS_LABELS.get(view_class)
    if label is None:
        raise ValueError(f"unknown ViewClass {view_class!r}")
    return label


# =============================================================================================
# ViewKind — the SSOT for one memory type. Everything per-kind, write AND read, lives here.
# =============================================================================================

class ViewKind(ABC):
    """One memory type's complete definition: value slot, retrieval surface, idempotency
    signature, and read projection. The repository supplies the shared shape; a kind supplies
    only what makes it that kind. Payloads are plain dicts (kwargs the wrapper collected)."""

    #: the discriminator stored as `view_kind` and used in the `view_key`
    name: str

    #: LWW-register semantics (fold-algebra Law 1 / EXTREME(valid_at)): the current version is the
    #: one with the greatest world-time `valid_at`, so a temporally-OLDER event must not overwrite
    #: it. True for value registers (counter). False for set/list kinds (timeline), whose
    #: supersession is driven by the signature (the whole set changed), not by which arrived latest.
    lww_register: bool = False

    @abstractmethod
    def key_discriminator(self, payload: dict[str, Any]) -> str:
        """The per-subject key segment (`ns::subject::<this>`). Constant for single-slot kinds
        (timeline), value-bearing for keyed kinds (a counter's counter name)."""

    @abstractmethod
    def signature(self, payload: dict[str, Any]) -> str:
        """Idempotency key: equal signature => no-op (refresh provenance only); different =>
        supersede. Counter = the value; timeline = a hash of the ordered events."""

    @abstractmethod
    def surface(self, subject: str, payload: dict[str, Any]) -> tuple[str, str]:
        """(name, summary): the BM25/embedding recall surface + the human-readable body. This is
        what makes the View findable by a natural-language query."""

    def summary_template(self, subject: str, payload: dict[str, Any]) -> str | None:
        """The kind's summary with `_COUNT_TOKEN` where the supporting-event count goes, or None if
        the summary does not quote that count.

        Only used by the unchanged-value provenance refresh (plan D2): the union of episode UUIDs is
        computed inside Cypher, so the final count is not known in Python — the count is substituted
        into this template server-side. A kind that returns None keeps its summary as written."""
        return None

    @abstractmethod
    def write_props(self, subject: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        """The value slot (+ any compat mirror) merged onto the node. Must NOT set the shared
        view_* identity props — `_write_version` owns those."""

    #: RETURN columns for a current-version fetch. Defined ONCE per kind (was duplicated in the
    #: old per-kind fetch queries), consumed by `_fetch_current`.
    read_fields: str

    @abstractmethod
    def parse(self, row: dict[str, Any]) -> dict[str, Any]:
        """Map a fetched row (shaped by `read_fields`) to the kind's public dict."""

    # -- shared defaults; a kind overrides only if it differs ---------------------------------
    def subtype(self, payload: dict[str, Any]) -> str:
        """Return the payload-aware semantic subtype stored on the View.

        Most kinds have one subtype and therefore use their kind name. Timeline overrides this
        because legacy subject-only timelines and query-sufficient event lanes have different
        audiences despite sharing the same storage kind.
        """
        return self.name

    def audience(self, payload: dict[str, Any]) -> ViewAudience:
        """Return the payload-aware consumer audience, failing closed by default."""
        return ViewAudience.OPERATOR

    def view_subtype(self, payload: dict[str, Any]) -> str:
        """Named stamp accessor used by persistence writers."""
        return self.subtype(payload)

    def view_audience(self, payload: dict[str, Any]) -> ViewAudience:
        """Named stamp accessor used by persistence writers."""
        return self.audience(payload)

    def view_stamps(
        self, payload: dict[str, Any], *, view_class: ViewClass = ViewClass.FACT
    ) -> dict[str, str]:
        """Return the complete lifecycle classification stamped on one View version.

        Metrics are operator-only regardless of the underlying kind. This keeps a metric counter
        from inheriting the recall audience of a FACT counter while preserving one shared kind.
        """
        audience = (
            ViewAudience.OPERATOR
            if view_class is ViewClass.METRIC
            else self.view_audience(payload)
        )
        return {
            "view_class": view_class.value,
            "view_subtype": self.view_subtype(payload),
            "view_audience": audience.value,
        }

    def episode_uuids(self, payload: dict[str, Any]) -> list[str]:
        return [str(u) for u in (payload.get("episode_uuids") or [])]

    def valid_at(self, payload: dict[str, Any]) -> str | None:
        return payload.get("valid_at")


# =============================================================================================
# ViewRepository — owns the SHARED shape; dispatches per-kind work to a ViewKind.
# =============================================================================================


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_episode_uuids(episode_uuids: Any) -> list[str]:
    """The durable provenance list's canonical form (plan D1): sorted, deduplicated, no blanks.
    Sorted+dedup is what makes `supporting_event_count` an exact, order-independent length and lets
    the Cypher-side union be compared against the Python-side one."""
    return sorted({str(u).strip() for u in (episode_uuids or []) if str(u or "").strip()})


def _checked_template(kind: ViewKind, subject: str, payload: dict[str, Any], summary: str,
                      n_eps: int) -> str | None:
    """The kind's summary template, but ONLY if it provably reconstructs the summary just rendered.

    The template mechanism assumes `_COUNT_TOKEN` appears exactly where the count goes and nowhere
    else. A subject or counter whose text literally contains the token would break that assumption
    and let Cypher rewrite part of the fact's own summary text. So the invariant is checked here
    rather than trusted: substitute the real count back in and require it to reproduce the rendered
    summary byte-for-byte. If it does not, the template is discarded -- the provenance refresh then
    leaves `summary`/`content` alone (a slightly stale count) instead of corrupting them."""
    tpl = kind.summary_template(subject, payload)
    if tpl is None:
        return None
    if tpl.replace(_COUNT_TOKEN, str(n_eps)) != summary:
        logger.warning(
            "view summary template does not reconstruct the summary for %r/%r; "
            "skipping count refresh (does the text contain %r?)",
            subject, kind.name, _COUNT_TOKEN,
        )
        return None
    return tpl


def _log_missing_episodes(node_uuid: str, stored: list[str], missing: list[str]) -> None:
    """One structured line per write when provenance points at episodes that no longer exist
    (plan D3). Missing UUIDs are KEPT and keep counting -- the fact really was supported by them --
    so this is the only place that gap becomes visible. Sample is bounded."""
    if not missing:
        return
    logger.info(
        "view provenance: %d/%d supporting episodes missing for view %s (sample=%s)",
        len(missing), len(stored), node_uuid, missing[:5],
    )


#: Tolerant Neo4j-timestamp parse. Single definition in `domain.temporal` -- do not re-inline it:
#: unparseable here makes `_is_older` fail open, silently disabling the LWW guard below.
_parse_dt = parse_iso8601


def _is_older(candidate: Any, current: Any) -> bool:
    """True iff `candidate` valid_at is strictly earlier than `current` valid_at. Both parsed
    tolerantly; a naive timestamp is treated as UTC. Unparseable on either side -> False (do not
    skip — fail open to the pre-guard behavior)."""
    a, b = _parse_dt(candidate), _parse_dt(current)
    if a is None or b is None:
        return False
    if (a.tzinfo is None) != (b.tzinfo is None):
        a = a.replace(tzinfo=timezone.utc) if a.tzinfo is None else a
        b = b.replace(tzinfo=timezone.utc) if b.tzinfo is None else b
    return a < b
