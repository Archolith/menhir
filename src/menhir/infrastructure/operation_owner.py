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

import os
import uuid as uuidlib
from datetime import datetime, timedelta, timezone

#: Default lease window. A writer must renew within this or its claim looks abandoned.
DEFAULT_LEASE_SECONDS = 120

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
    "LIVE_OWNER",
    "OWNER_UNKNOWN",
    "classify_ownership",
    "is_own_claim",
    "lease_expiry_iso",
    "process_owner_token",
    "utc_now_iso",
]
