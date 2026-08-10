"""QuantState — the counter *kind* of the generic View (see `view_repository.py`).

Historically its own repository; as of the Timeline-as-a-View build (2026-07-02) QuantState is
just `view_kind='counter'` on the one supersedable View node shape. This module is kept as a
back-compat alias so existing imports (`from menhir.infrastructure.quantstate_repository import
QuantStateRepository`) and the `record_counter/fetch_counter/list_counters/history` surface keep
working unchanged. New code should import `ViewRepository` directly.

The rename the architecture doc predicted (`QuantState -> View(kind)`) was earned when the SECOND
kind (timeline) dropped into the same node shape with only its value slot differing — so the
machinery moved to ViewRepository and this became an alias, exactly as forecast.
"""

from __future__ import annotations

from menhir.infrastructure.view_repository import ViewRepository

# QuantState IS a View (kind='counter'). Subclass, don't just alias, so `isinstance` checks and any
# subclass hooks still resolve to a distinct type while all behaviour lives in ViewRepository.
class QuantStateRepository(ViewRepository):
    """Back-compat name for the counter kind of ViewRepository. No added behaviour."""


__all__ = ["QuantStateRepository", "ViewRepository"]
