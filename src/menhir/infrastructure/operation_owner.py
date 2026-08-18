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

from menhir.infrastructure import process_liveness
from menhir.infrastructure.neo4j import (
    SAGA_MUTATION_TIMEOUT_S,
    mutation_window_seconds,
)

#: Extra margin on top of the computed mutation window. Absorbs scheduling jitter, a slow
#: heartbeat thread waking late, and clock granularity -- none of which the window itself models.
LEASE_SAFETY_MARGIN_S = 30

#: Upper BOUND on the bounded mutation statements each saga kind may dispatch. The TTL must cover
#: every one of them, because each is separately bounded AND separately retried.
#:
#: METRIC_WRITE is held at 2 deliberately, as a bound rather than a measurement. On the CURRENT
#: source it issues one mutating statement: record_metric passes episode_uuids=[] and _link_episodes
#: returns immediately on an empty list, so the second statement never runs. Keeping 2 costs only a
#: longer lease -- which merely delays recovery -- while assuming 1 would break the moment a caller
#: supplies episodes. Getting this wrong in the LOW direction is the dangerous one: it yields a
#: lease shorter than the work it comfortably covers. (Sizing only -- an undersized lease costs
#: renewal churn and delayed recovery, never a replay underneath a live writer.)
SAGA_STATEMENT_COUNTS: dict[str, int] = {
    "ENTITY_MERGE": 1,
    "ENTITY_UNMERGE": 1,
    "LEGACY_ENTITY_UNMERGE": 1,
    "ENTITY_DELETE": 1,
    "SESSION_TTL_DELETE": 1,
    "METRIC_WRITE": 2,
}

#: Conservative fallback for a kind not listed above. Deliberately the LARGEST known count: a lease
#: that is too long merely delays recovery, while one that is too short causes needless renewal
#: churn. Neither can produce a replay underneath a live writer: abandonment requires positive
#: death evidence, not an expired clock.
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

    Pairs with the per-dispatch headroom check in ``WriterHeartbeat``: this keeps the renewal
    SCHEDULE comfortable, while that check stops a writer dispatching new work when its own claim is
    nearly spent even if the heartbeat thread ran late (a GC pause or a starved interpreter).

    **Neither is a proof of anything, and an earlier version of this docstring said otherwise.** It
    claimed the headroom check made each dispatch "locally provable" and that the pair closed a
    hole. Recovery no longer infers abandonment from a clock, so there is no hole here to close:
    both mechanisms reduce churn and avoid pointless work, and abandonment authority rests entirely
    on positive death evidence.

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

    Four components: the instance label (which deployment), the HOSTNAME (which machine), the PID
    (which process on that machine), and a process-start nonce.

    The hostname is what makes the PID usable as death evidence: a PID may only be inspected on the
    host that recorded it, and asking locally about a remote PID answers about an unrelated process.

    The nonce is load-bearing for the opposite reason: PIDs are recycled, so label+host+PID alone
    can repeat after a restart. A recycled PID reads as ALIVE, which is the safe direction -- it
    yields "cannot prove death" rather than a false claim of death.
    """
    return f"{_instance_label()}:{process_liveness.hostname()}:{os.getpid()}:{_PROCESS_NONCE}"


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


def _owner_parts(token: str) -> tuple[str, int] | None:
    """(hostname, pid) from an owner token, or None if it is not in the current format."""
    parts = token.split(":")
    if len(parts) != 4:
        return None
    try:
        return parts[1], int(parts[2])
    except (TypeError, ValueError):
        return None


#: Deployment assertion: hostname equality in an owner token really does identify the SAME PID
#: namespace, so a local PID lookup answers about the recorded writer.
HOST_PID_NAMESPACE_ENV = "MENHIR_HOST_PID_NAMESPACE_VERIFIABLE"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def host_pid_namespace_is_verifiable() -> bool:
    """Whether this deployment asserts that a hostname uniquely identifies a PID namespace.

    :func:`classify_ownership` uses a local PID lookup as death evidence when an owner token
    records THIS hostname. That step is sound only if hostname equality actually implies the
    recorded PID belongs to the namespace this process can inspect -- and hostname equality does
    NOT establish that on its own. Containers sharing a kernel, cloned images, and two nodes
    mounting the same journal volume can all present the same hostname while their PIDs are
    unrelated. There ``pid_alive`` would answer about a different process entirely, and answering
    "not running" about the wrong process is a fabricated death certificate -- the exact failure
    mode the death-evidence rule was introduced to remove.

    Menhir cannot verify the property from inside the process, so it is an OPERATOR ASSERTION made
    per deployment and checked by the CF-20c preflight. It defaults to NOT asserted. Without it,
    automatic PID-based recovery is disabled: an expired local row fences as OWNER_UNKNOWN and the
    only route back is a named operator attestation. That costs automatic recovery in an
    unconfigured deployment, which is the direction this subsystem consistently prefers over
    inferring evidence it does not have.
    """
    return os.environ.get(HOST_PID_NAMESPACE_ENV, "").strip().lower() in _TRUTHY


#: Deployment assertion: NO GATE-UNAWARE SAGA WRITER CAN BE RUNNING here -- every saga writer is
#: either stopped or runs a build whose ``GraphOperationsJournal.prepare()`` honours the
#: reconciliation gate.
#:
#: Named for the invariant rather than for one way of reaching it. An earlier name
#: (``..._WRITERS_QUIESCED``) implied every writer must be stopped, which is stronger than what is
#: required and would read as false on a healthy single-version deployment whose writers are simply
#: running current code. Current-version peers need not be quiesced at all.
SAGA_WRITERS_GATE_AWARE_ENV = "MENHIR_SAGA_ALL_WRITERS_GATE_AWARE"


def all_saga_writers_are_gate_aware() -> bool:
    """Whether this deployment asserts no gate-unaware saga writer can be running.

    The global PREPARE pause is enforced INSIDE ``prepare()``, so it binds only writers whose code
    knows to check the reconciliation lease. An older binary with the journal protocol but without
    that check can still insert a PREPARED row while recovery holds the gate:

        recovery acquires gate -> preflight runs -> OLD writer PREPAREs anyway
        -> old writer begins mutating -> recovery misses the new row
        -> recovery reports write-ready over an operation that is still in flight

    That cannot be fixed from inside the new binary: the bypassing process is the one that does not
    have the check. The honest options are to enforce below the writer (a guard in the shared
    SQLite journal) or to declare mixed-version writers out of scope for live recovery. This is the
    second, recorded as an operator assertion so the assumption is stated rather than silent.

    **This is a promise, not a mechanism.** It records that an operator has confirmed no
    gate-unaware writer can be running. Nothing here prevents one; it makes the precondition
    explicit and fail-closed, which is what separates a known limitation from an unknown one.
    Defaults to NOT asserted, and the preflight treats its absence as a BLOCKER rather than a
    warning -- unlike the PID-namespace assertion, whose absence merely narrows recovery, this one
    admits a writer racing recovery.
    """
    return os.environ.get(SAGA_WRITERS_GATE_AWARE_ENV, "").strip().lower() in _TRUTHY


def classify_ownership(
    row: object,
    *,
    now: datetime | None = None,
    local_hostname: str | None = None,
) -> str:
    """Decide whether a PREPARED row may be recovered, requiring POSITIVE EVIDENCE of writer death.

    An expired lease is deliberately NOT sufficient. The earlier design read "lease expired" as
    "writer is gone", which silently depended on an unproven premise: that an already-dispatched
    graph mutation must have returned within a bounded time. It need not. ``Query(timeout=...)``
    bounds the SERVER transaction, while the client fetches records lazily over a socket that has
    no comparable read deadline, so elapsed time alone cannot establish that the original writer
    has stopped executing.

    So expiry now demotes a claim from "fresh" to "stale", and something independent must prove the
    writer is dead before recovery may act:

    * fresh lease                                        -> LIVE_OWNER (a hard veto)
    * expired, operator attested THIS owner's death      -> ABANDONED (recoverable)
    * expired, verifiable SAME host, PID demonstrably gone -> ABANDONED (recoverable)
    * expired, remote host or PID still present          -> OWNER_UNKNOWN (fenced, needs a human)
    * expired, same host but PID namespace unverifiable  -> OWNER_UNKNOWN (see the env assertion)
    * no token / no expiry / unreadable                  -> OWNER_UNKNOWN

    Expiry is evaluated BEFORE attestation. A fresh lease outranks everything, an attestation
    included: a live lease means the writer renewed it moments ago, so an attestation against it
    is stale or mistaken, and honouring it would replay underneath a running writer.

    The asymmetry is the point: a false ABANDONED double-applies a graph mutation, while a false
    LIVE_OWNER or OWNER_UNKNOWN only delays recovery. Every ambiguity therefore resolves away from
    recovery.
    """
    if not isinstance(row, dict):
        return OWNER_UNKNOWN

    token = row.get("owner_token")
    if not isinstance(token, str) or not token.strip():
        return OWNER_UNKNOWN

    expires_at = _parse_iso(row.get("owner_lease_expires_at"))
    if expires_at is None:
        return OWNER_UNKNOWN
    if expires_at > (now or _utc_now()):
        return LIVE_OWNER

    # Expired. That is a staleness signal, not a death certificate. Something INDEPENDENT of the
    # clock must establish that the recorded writer stopped.

    # An operator attesting the death is that independent evidence, and it is the sanctioned path
    # for an owner this process can never inspect. It is accepted only on an already-stale claim,
    # so it can never override a live heartbeat.
    #
    # It must also be an attestation about THIS owner. An attestation names one process; after
    # ownership transfers, that same row carries a different writer, and honouring the old
    # attestation would declare the NEW owner dead on the strength of evidence about its
    # predecessor. The write side clears the attestation on transfer and on renewal, so this
    # equality check is the read-side half of the same rule -- and it is what keeps a row written
    # by an older binary (attestation present, subject token absent) from being trusted.
    attested_by = str(row.get("owner_death_attested_by") or "").strip()
    attested_for = str(row.get("owner_death_attested_for_token") or "").strip()
    if attested_by and attested_for == token:
        return ABANDONED

    parts = _owner_parts(token)
    if parts is None:
        return OWNER_UNKNOWN
    owner_host, owner_pid = parts
    if owner_host != (local_hostname or process_liveness.hostname()):
        # A remote PID cannot be inspected from here, so death is unprovable by this process.
        return OWNER_UNKNOWN
    if not host_pid_namespace_is_verifiable():
        # Same hostname, but this deployment has NOT asserted that a hostname identifies exactly
        # one PID namespace. The local PID table may therefore describe an unrelated process, so
        # what looks like a death certificate would be about the wrong process.
        return OWNER_UNKNOWN
    if process_liveness.pid_alive(owner_pid):
        # Either the original writer is still running, or its PID was recycled. Both read as
        # "cannot prove death", and both must fence.
        return OWNER_UNKNOWN
    return ABANDONED


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
    "host_pid_namespace_is_verifiable",
    "all_saga_writers_are_gate_aware",
    "SAGA_WRITERS_GATE_AWARE_ENV",
    "HOST_PID_NAMESPACE_ENV",
    "is_own_claim",
    "lease_expiry_iso",
    "process_owner_token",
    "utc_now_iso",
]
