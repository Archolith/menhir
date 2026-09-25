"""The saga reconcile dispatcher's routing table: which coordinator owns each operation kind.

Extracted from :mod:`menhir.services.saga_reconcile_dispatcher` for file size and re-exported
from there; every public name in this module remains importable from the dispatcher module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from menhir.services.saga_reconcile_outcomes import WOULD_NEEDS_REVIEW


@dataclass(frozen=True)
class NonReplayableKind:
    """A recorded decision that a saga kind must never be recovered by replaying it (CF-209).

    This is a THIRD disposition, and the distinction it draws is the point. Before it, the routing
    table could only say "a coordinator owns this kind" or nothing at all, and nothing at all was
    reported as UNKNOWN_KIND -- which reads as "something unexpected is in the journal". For
    ``LEGACY_ENTITY_UNMERGE`` that was the wrong message twice over: its presence is entirely
    expected, and UNKNOWN_KIND blocks write-readiness for the WHOLE run, so one such row would
    stall recovery of every other row in the backlog.

    Quarantine, never replay. The decision rests on the legacy lane's own contract:

    * The forward operation requires ``acknowledge_degraded=True`` -- an explicit human
      acknowledgement, given for one specific invocation. Replaying it unattended means a machine
      re-asserting that acknowledgement in a situation the human never saw.
    * The restore is NEVER exact (``exact: False``, unconditionally). ``legacy_unmerge_coordinator``
      exists to avoid "handing back a partially-restored graph and letting an operator believe it is
      repaired" -- and an unwatched replay produces exactly that, with a degradation list nobody
      read.
    * The restore is multi-part (labels, properties, in/out relationships, episode rebinding) and
      the legacy snapshot is lossy, so a crash can leave it partially applied with no reliable way
      to tell how far it got.

    So the row is routed to WOULD_NEEDS_REVIEW: visible, counted, and adjudicable by an operator
    through the existing NEEDS_REVIEW clearance, which releases the participant fence when they
    resolve it. The two entity UUIDs stay fenced until then, deliberately -- the graph really is in
    an unknown partial state, and letting other operations touch those nodes meanwhile is worse
    than making a human look.

    Kinds that are genuinely unexpected (``METRIC_MIGRATE``, ``METRIC_REVERSE``: declared in
    ``OPERATION_KINDS`` but written by no code in the tree) stay unmapped on purpose. "We decided
    this cannot be replayed" and "we do not know what this is" are different facts and must not
    collapse into one outcome.
    """

    kind: str
    reason: str

    def classify_prepared_row(self, row: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """Always quarantine. No graph read, because no graph state could change the answer."""
        return WOULD_NEEDS_REVIEW, {"observed_error": self.reason}


#: The recorded disposition for legacy unmerge rows. Held as a module constant so the routing table
#: keeps one home and the decision is greppable from the kind name.
LEGACY_UNMERGE_DISPOSITION = NonReplayableKind(
    kind="LEGACY_ENTITY_UNMERGE",
    reason=(
        "LEGACY_ENTITY_UNMERGE is structurally non-replayable: the restore is degraded, never "
        "exact, and gated on a per-invocation operator acknowledgement that recovery cannot give "
        "on an operator's behalf. Quarantined for adjudication (CF-209)."
    ),
)


def build_handlers(
    *,
    merge: Any = None,
    unmerge: Any = None,
    metric_write: Any = None,
    delete: Any = None,
    erasure: Any = None,
) -> dict[str, Any]:
    """Map operation_kind -> the coordinator that owns it.

    Defined here, as data, so the routing table has exactly one home. Three dispositions exist:

    * **a coordinator owns the kind** -- it is replayed.
    * **a recorded non-replayable decision** -- ``LEGACY_ENTITY_UNMERGE`` is registered
      unconditionally to :data:`LEGACY_UNMERGE_DISPOSITION` and always quarantines. It takes no
      coordinator argument because the disposition is a property of the KIND, not of whether some
      service happens to be wired up.
    * **deliberately unmapped** -- ``METRIC_MIGRATE`` / ``METRIC_REVERSE`` are declared in
      ``OPERATION_KINDS`` but no code in the tree writes or reconciles them, so a row of either
      kind means something unexpected has happened. They report UNKNOWN_KIND and block readiness,
      which is the correct response to a journal row nobody can account for.

    Passing ``None`` for a coordinator leaves its kinds unmapped, which is how a caller can observe
    a subset without pretending the rest are handled.
    """
    handlers: dict[str, Any] = {"LEGACY_ENTITY_UNMERGE": LEGACY_UNMERGE_DISPOSITION}
    if merge is not None:
        handlers["ENTITY_MERGE"] = merge
    if unmerge is not None:
        handlers["ENTITY_UNMERGE"] = unmerge
    if metric_write is not None:
        handlers["METRIC_WRITE"] = metric_write
    if delete is not None:
        handlers["ENTITY_DELETE"] = delete
        handlers["SESSION_TTL_DELETE"] = delete
    if erasure is not None:
        handlers["EXPLICIT_ERASURE"] = erasure
    return handlers
