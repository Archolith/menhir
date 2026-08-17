"""Per-operation ownership for saga recovery (CF-20b).

The problem this exists to solve is NOT two reconcilers racing each other -- a reconciliation
lease covers that. It is the harder case: process A PREPAREs an operation and starts mutating
the graph, process B starts up, sees the row still PREPARED, and replays it while A is still
executing. Both processes are legitimate and only one is a reconciler, so reconciler exclusivity
does nothing here. The only way to tell "crashed midway" from "still running elsewhere" is for
the PREPARED row itself to carry proof of a live writer.

In CF-20b this metadata is written and classified but never acted on: nothing replays yet.

Fail-closed is the rule throughout. Every ambiguous case resolves to OWNER_UNKNOWN rather than
ABANDONED, because misjudging a live writer as abandoned is what produces a double-apply, while
misjudging an abandoned row as live merely defers recovery.
"""

from __future__ import annotations

import math
import os
import uuid as uuidlib
from datetime import datetime, timedelta, timezone

from menhir.infrastructure.neo4j import (
    SAGA_MUTATION_TIMEOUT_S,
    mutation_window_seconds,
)

#: Extra margin on top of the computed mutation window. Absorbs scheduling jitter, a slow
#: heartbeat thread waking late, and clock granularity -- none of which the window itself models.
LEASE_SAFETY_MARGIN_S = 30

#: Bounded mutation statements each saga kind dispatches. The TTL must cover every one of them,
#: because each is separately bounded AND separately retried.
#:
#: METRIC_WRITE is 2: _write_version then _link_episodes. Getting this wrong in the low direction
#: is the dangerous direction -- it yields a lease shorter than the work it must outlive.
SAGA_STATEMENT_COUNTS: dict[str, int] = {
    "ENTITY_MERGE": 1,
    "ENTITY_UNMERGE": 1,
    "LEGACY_ENTITY_UNMERGE": 1,
    "ENTITY_DELETE": 1,
    "SESSION_TTL_DELETE": 1,
    "METRIC_WRITE": 2,
}

#: Conservative fallback for a kind not listed above. Deliberately the LARGEST known count: a lease
#: that is too long merely delays recovery, while one that is too short lets a live writer be
#: declared abandoned and replayed underneath itself.
_UNKNOWN_KIND_STATEMENTS = max(SAGA_STATEMENT_COUNTS.values())


#: The heartbeat renews every ``lease / RENEW_DIVISOR`` seconds. It lives here, beside the TTL
#: formula, because the two are not independent: the renewal cadence is the worst-case DETECTION LAG
#: for a lost claim, and the TTL derivation below has to include it. Keeping them apart is what let
#: the first version of this formula be wrong.
RENEW_DIVISOR = 3


def mutation_window_for_kind(operation_kind: str) -> float:
    """Bounded mutation window for a saga kind, using its real statement count."""
    statements = SAGA_STATEMENT_COUNTS.get(str(operation_kind), _UNKNOWN_KIND_STATEMENTS)
    return mutation_window_seconds(SAGA_MUTATION_TIMEOUT_S, statements=statements)


def lease_seconds_for(*, statements: int = 1) -> int:
    """Ownership TTL DERIVED from the bounded mutation window AND the heartbeat cadence.

    Not an independent constant. A hand-picked number cannot be audited against the thing it has to
    outlive, and the original 120s was in fact shorter than a single statement's window -- so
    "expired means abandoned" was invalid without anyone being able to see it from the constant.

    ``TTL > W`` is NOT sufficient, and an earlier version of this function got that wrong. A writer
    can legitimately dispatch a mutation just BEFORE the next heartbeat discovers that renewal is
    failing, so with ``H`` the renewal interval:

        last successful renewal   t0
        mutation dispatches       ~t0 + H     (revocation not yet detected)
        mutation still in flight  ~t0 + H + W

    The claim must therefore outlive ``H + W + margin``, not ``W + margin``. Because ``H`` is itself
    ``TTL / RENEW_DIVISOR`` that is circular, and solving it gives:

        TTL > TTL/D + W + M   =>   TTL > D/(D - 1) * (W + M)

    which is what this returns. For D = 3 that is 1.5x the naive figure.

    This is belt-and-braces with the per-dispatch headroom check in ``WriterHeartbeat``: this makes
    the SCHEDULE sound, while that check makes each individual dispatch locally provable even if the
    heartbeat thread is late. Either alone leaves a hole; the scheduling argument depends on the
    thread being timely, and a late thread is exactly what a GC pause or a starved interpreter
    produces.

    Unbounded READS in the saga do not enter the calculation: once ownership is lost the revocation
    seam refuses to dispatch any further statement, so a hanging read can delay a writer but can
    never let it mutate.
    """
    window = mutation_window_seconds(SAGA_MUTATION_TIMEOUT_S, statements=statements)
    naive = window + LEASE_SAFETY_MARGIN_S
    return int(math.ceil(RENEW_DIVISOR / (RENEW_DIVISOR - 1) * naive))


def lease_seconds_for_kind(operation_kind: str) -> int:
    """TTL for a specific saga kind, using its real statement count."""
    statements = SAGA_STATEMENT_COUNTS.get(str(operation_kind), _UNKNOWN_KIND_STATEMENTS)
    return lease_seconds_for(statements=statements)


#: Default lease window, derived for a single-statement mutation. A writer must renew within this
#: or its claim looks abandoned.
DEFAULT_LEASE_SECONDS = lease_seconds_for(statements=1)

#: Classification results. LIVE_OWNER and OWNER_UNKNOWN mirror the saga-reconcile vocabulary
#: because they surface as dry-run outcomes; ABANDONED is internal -- an abandoned row carries
#: no veto and falls through to the saga's own replay classification.
LIVE_OWNER = "LIVE_OWNER"
OWNER_UNKNOWN = "OWNER_UNKNOWN"
ABANDONED = "ABANDONED"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return _utc_now().isoformat()


# The nonce is generated once, at import, so it is fixed for the life of the process and
# differs across processes. This is the part that makes the token trustworthy.
_PROCESS_NONCE = uuidlib.uuid4().hex


def _instance_label() -> str:
    """The deployment's instance label, or an explicit placeholder.

    MENHIR_INSTANCE_ID defaults to the EMPTY STRING and is routinely unset, so a token derived
    from it alone would be identical in every process -- which is precisely the collision that
    would let one process claim another's live operation. The label is therefore only ever one
    component of the token, never the whole of it, and an unset value is spelled out rather
    than left blank so a token is never ambiguous about which component was missing.
    """
    return os.environ.get("MENHIR_INSTANCE_ID", "").strip() or "instance-unset"


def process_owner_token() -> str:
    """A token identifying THIS process, stable for its lifetime and unique across processes.

    Three components: the instance label (which deployment), the PID (which process on that
    host), and a process-start nonce. The nonce is load-bearing: PIDs are recycled by the OS, so
    label+PID alone can repeat after a restart and let a fresh process inherit a dead process's
    claim on a PREPARED row.
    """
    return f"{_instance_label()}:{os.getpid()}:{_PROCESS_NONCE}"


def lease_expiry_iso(*, seconds: int = DEFAULT_LEASE_SECONDS, now: datetime | None = None) -> str:
    """The ISO instant at which a claim taken/renewed now stops proving liveness.

    ``seconds`` is clamped to at least 1: minting a zero or negative lease would create a claim
    that is already dead on arrival, which reads as an abandoned row and invites a replay of work
    that is actually starting up. Because of that clamp this cannot be used to construct a PAST
    expiry -- pass an earlier ``now`` for that.
    """
    base = now or _utc_now()
    return (base + timedelta(seconds=max(1, int(seconds)))).isoformat()


def _parse_iso(value: object) -> datetime | None:
    """Parse a stored ISO timestamp, or None if it is absent or unreadable.

    Naive values are treated as UTC: every writer in this codebase stores timezone-aware UTC,
    so a naive value means a hand-edited or legacy row, and assuming UTC is closer than
    crashing. Callers must treat None as "cannot prove", never as "expired".
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def classify_ownership(row: object, *, now: datetime | None = None) -> str:
    """Decide whether a PREPARED row's original writer can be proven still alive.

    Returns LIVE_OWNER (a hard veto on replay), OWNER_UNKNOWN (liveness unprovable -- also a
    veto, but for a different reason and needing a different operator response), or ABANDONED
    (no live claim; the row is a legitimate recovery candidate).

    OWNER_UNKNOWN covers three genuinely different situations that share one property -- we
    cannot prove the writer is gone:

    * no owner token at all: a row written before this fence existed. During a mixed-version
      rollout an older binary with no ownership support may still be running and may still own
      it, so "ownerless" must NOT be read as "abandoned".
    * a token but no lease expiry: nothing to compare against.
    * a lease expiry that will not parse: a corrupt or hand-edited value.

    Only an owner whose lease has demonstrably passed is ABANDONED.
    """
    if not isinstance(row, dict):
        return OWNER_UNKNOWN

    token = row.get("owner_token")
    if not isinstance(token, str) or not token.strip():
        return OWNER_UNKNOWN

    expires_at = _parse_iso(row.get("owner_lease_expires_at"))
    if expires_at is None:
        return OWNER_UNKNOWN

    return ABANDONED if expires_at <= (now or _utc_now()) else LIVE_OWNER


def is_own_claim(row: object, *, token: str | None = None) -> bool:
    """Whether this process is itself the recorded owner of the row.

    Recovery needs this to avoid vetoing itself: a process that PREPAREd an operation and then
    runs its own reconciler would otherwise see its own fresh heartbeat and classify the row
    LIVE_OWNER forever.
    """
    if not isinstance(row, dict):
        return False
    recorded = row.get("owner_token")
    return isinstance(recorded, str) and recorded == (token or process_owner_token())


__all__ = [
    "ABANDONED",
    "DEFAULT_LEASE_SECONDS",
    "LEASE_SAFETY_MARGIN_S",
    "RENEW_DIVISOR",
    "SAGA_STATEMENT_COUNTS",
    "mutation_window_for_kind",
    "lease_seconds_for",
    "lease_seconds_for_kind",
    "LIVE_OWNER",
    "OWNER_UNKNOWN",
    "classify_ownership",
    "is_own_claim",
    "lease_expiry_iso",
    "process_owner_token",
    "utc_now_iso",
]
