"""ScalarState + ScalarHistory fold/rebuild orchestration.

`rebuild_scalar_state(subject_uuid)` reads the entity's CURRENT, fully-bound :TypedAssertion rows,
runs the pure deterministic fold (`domain.scalar_state_fold.fold_assertions`), and AUTHORITATIVELY
upserts one kind='scalar_state' View per non-abstained slot via `record_scalar_state` (which writes
in projection-authoritative mode: it bypasses the incremental LWW guard so a correction can move the
current value backward in time, and fully re-renders derived fields). It then reconciles: any current
View whose slot is absent from the freshly-folded desired set is retired. Because the same pure fold
backs both the live ingest path and this replay, rebuilding from the durable log reproduces the live
View exactly.

`rebuild_scalar_history(subject_uuid)` reads the same materializable assertions and builds advisory,
ordered scalar_history Views per slot — including delta-only slots that scalar_state correctly
refuses to ground. See `domain.scalar_history.build_history`.

`rebuild_scalar_projections(subject_uuid)` is the coordinator: it runs both enabled projections and
clears projection_pending only after all succeed. Callers that need both projections rebuilt should
prefer this method. The individual methods remain as compatibility entrypoints.

Authority's source of truth is the EVENT LOG, read at recall time via `current_authority()` (the
weakest-contributor effective tier per slot) — never the View's stamped audit receipt. The receipt is
a best-effort snapshot kept fresh for observability; Piece D must resolve authority through
`current_authority()`, not by trusting node props.

This module is the facade for the ``scalar_state_service_*`` sibling modules: the shared
protocols/helpers live in ``scalar_state_service_common``, the scalar_state projection methods in
``scalar_state_service_state``, the scalar_history projection + coordinator in
``scalar_state_service_history``, and the merge lifecycle in ``scalar_state_service_merge``. Every
moved symbol is re-exported here so existing import sites keep working unchanged.
"""

from __future__ import annotations

import logging

from menhir.services.scalar_state_service_common import (
    _AssertionSource,
    _ViewSink,
    _blocked_slots,
    _projection_complete,
    _retirement_reason,
    _slot_of_view,
)
from menhir.services.scalar_state_service_history import ScalarHistoryProjectionMixin
from menhir.services.scalar_state_service_merge import ScalarMergeLifecycleMixin
from menhir.services.scalar_state_service_state import ScalarStateProjectionMixin

logger = logging.getLogger(__name__)


class ScalarStateService(
    ScalarStateProjectionMixin,
    ScalarHistoryProjectionMixin,
    ScalarMergeLifecycleMixin,
):
    """Fold durable assertions into ScalarStateViews and rebuild them deterministically."""

    def __init__(
        self, assertions: _AssertionSource, views: _ViewSink, *,
        scalar_history_enabled: bool = False,
    ) -> None:
        self._assertions = assertions
        self._views = views
        self._scalar_history_enabled = scalar_history_enabled
