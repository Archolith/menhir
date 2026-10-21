"""Live-owner claim lifecycle of the graph-operations saga journal.

Split out of ``graph_operations.py`` (file-size refactor): heartbeat renewal, the claim of an
abandoned PREPARED row before replay, and operator death attestation. ``GraphOperationsJournal``
composes this mixin; every method runs against the facade's ``self.db_path``.
"""

from __future__ import annotations

from menhir.infrastructure import operation_owner as oo
from menhir.infrastructure.graph_operations_model import GraphOperationError
from menhir.infrastructure.telemetry import connect_telemetry_db
from menhir.clock import utc_now_iso as _utc_now_iso


class _GraphOperationsOwnershipMixin:
    """Ownership claims over PREPARED rows: renew, take over, attest death."""

    def renew_owner_heartbeat(
        self,
        op_id: str,
        *,
        seconds: int = oo.DEFAULT_LEASE_SECONDS,
        owner_token: str | None = None,
    ) -> bool:
        """Extend this process's claim on a PREPARED operation. Returns whether it still holds it.

        A single conditional UPDATE, so the check and the extension cannot interleave: it renews
        only if the row is still PREPARED AND still carries this process's token. The boolean is
        the point -- long-running saga code must be able to discover that it LOST its claim (the
        row was quarantined, committed, or taken over) and stop before its next side effect,
        rather than carrying on believing it owns work someone else may now be replaying.
        """
        self._ensure_ready()
        now = _utc_now_iso()
        token = owner_token or oo.process_owner_token()
        with connect_telemetry_db(self.db_path) as conn:
            cursor = conn.execute(
                "UPDATE graph_operations "
                "SET owner_heartbeat_at = ?, owner_lease_expires_at = ?, "
                # A renewal is the owner proving it is alive, which DISPROVES any attestation of its
                # death. Leaving one in place would let it reactivate the moment this fresh lease
                # later expired.
                "    owner_death_attested_by = NULL, owner_death_attested_at = NULL, "
                "    owner_death_attested_for_token = NULL "
                "WHERE op_id = ? AND state = 'PREPARED' AND owner_token = ?",
                (now, oo.lease_expiry_iso(seconds=seconds), op_id, token),
            )
            conn.commit()
            return cursor.rowcount == 1

    def claim_abandoned_operation(
        self,
        op_id: str,
        *,
        owner_token: str | None = None,
        seconds: int | None = None,
    ) -> bool:
        """Take ownership of an ABANDONED PREPARED row before replaying it (CF-20c).

        ``seconds`` defaults to the TTL derived from the row's OWN operation_kind, read inside the
        same transaction as the claim.

        Returns True only if this call is the one that took it. The whole point is that the
        transfer happens BEFORE any graph side effect: without it, two reconcilers can both read a
        row as abandoned, both begin mutating, and only discover the conflict at the journal
        transition -- by which time both have already touched the graph.

        One conditional UPDATE under BEGIN IMMEDIATE, so the liveness test and the transfer cannot
        interleave. The WHERE clause is the guard, and each conjunct is load-bearing:

        * ``state = 'PREPARED'`` -- a row that reached a terminal state is finished, not recoverable.
        * ``owner_token IS NOT NULL`` -- an ownerless row is OWNER_UNKNOWN, never ABANDONED. During
          a mixed-version rollout an older binary with no ownership support may still be executing
          it, so claiming it is exactly the double-apply this design exists to prevent.
        * ``owner_lease_expires_at <= now`` -- a live heartbeat is a hard veto. Comparison is done
          in SQL against the stored ISO string so the read and the write are one statement; ISO-8601
          UTC strings from ``_utc_now_iso`` sort lexicographically in timestamp order, which is what
          makes that valid.

        Those conjuncts are NECESSARY but not SUFFICIENT. Expiry alone is a staleness signal, never
        a death certificate, so the same :func:`classify_ownership` rule the observer applies is
        re-evaluated inside this transaction and must return ABANDONED before the UPDATE runs.

        A row this process already owns is NOT claimable through here: it is not abandoned, and a
        reconciler that finds its own live claim should be verifying its own state machine rather
        than re-taking a lease it never lost.
        """
        self._ensure_ready()
        token = owner_token or oo.process_owner_token()
        now = _utc_now_iso()
        with connect_telemetry_db(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Derive the new expiry from the ROW's own kind, inside the same transaction that reads
            # it. A claimant that stamped the single-statement default onto a two-statement
            # operation would hand itself a claim shorter than the work it is about to do.
            kind_row = conn.execute(
                "SELECT operation_kind FROM graph_operations WHERE op_id = ?", (op_id,)
            ).fetchone()
            claim_seconds = (
                seconds if seconds is not None
                else oo.lease_seconds_for_kind(kind_row[0] if kind_row else "")
            )
            # Enforce the SAME rule classification applies, inside the same transaction. Claiming
            # on expiry alone would route straight past the death-evidence requirement and
            # reintroduce exactly the unproven premise it exists to remove.
            row = conn.execute(
                "SELECT owner_token, owner_lease_expires_at, owner_death_attested_by, state, "
                "owner_death_attested_for_token "
                "FROM graph_operations WHERE op_id = ?",
                (op_id,),
            ).fetchone()
            if row is None or row[3] != "PREPARED":
                conn.commit()
                return False
            candidate = {
                "owner_token": row[0],
                "owner_lease_expires_at": row[1],
                "owner_death_attested_by": row[2],
                "owner_death_attested_for_token": row[4],
            }
            if oo.classify_ownership(candidate) != oo.ABANDONED:
                conn.commit()
                return False

            cursor = conn.execute(
                "UPDATE graph_operations "
                "SET owner_token = ?, owner_heartbeat_at = ?, owner_lease_expires_at = ?, "
                "    updated_at = ?, "
                # The attestation dies with the owner it named. Carrying it across a transfer would
                # make it evidence about the CLAIMANT: once this new lease later went stale, the
                # stale attestation would declare a possibly-live reconciler ABANDONED and invite a
                # third process to replay underneath it -- the exact false-death path Option 3
                # exists to remove.
                "    owner_death_attested_by = NULL, owner_death_attested_at = NULL, "
                "    owner_death_attested_for_token = NULL "
                "WHERE op_id = ? "
                "  AND state = 'PREPARED' "
                "  AND owner_token IS NOT NULL "
                "  AND owner_token != ? "
                "  AND owner_lease_expires_at IS NOT NULL "
                "  AND owner_lease_expires_at <= ?",
                (token, now, oo.lease_expiry_iso(seconds=claim_seconds), now, op_id, token, now),
            )
            conn.commit()
            return cursor.rowcount == 1

    def attest_owner_death(self, op_id: str, *, attested_by: str) -> bool:
        """Record independent operator evidence that a PREPARED row's writer is gone.

        The sanctioned path for the case automation cannot decide: an owner on another host, or one
        whose PID has been recycled, where this process can never prove death by inspection. It is
        deliberately a human act with a name attached -- the alternative is inferring death from a
        clock, which is the premise this design removed.

        An attestation is a safety OVERRIDE for evidence automation cannot gather -- it is not a
        faster clock, and each WHERE conjunct keeps it from becoming one:

        * ``state = 'PREPARED'`` -- a terminal operation has no writer to attest about.
        * ``owner_token IS NOT NULL`` -- an ownerless row names no writer, so there is nobody to
          attest the death OF. Those rows stay OWNER_UNKNOWN and need direct repair.
        * ``owner_lease_expires_at IS NOT NULL AND <= now`` -- the claim must ALREADY be stale.
          Attesting against a live heartbeat would let a mistaken operator authorise a replay
          underneath a writer that renewed its lease seconds ago. :func:`classify_ownership`
          independently refuses to read an attestation on a fresh lease, so this is the durable
          half of a check enforced on both the write and the read side.

        Both the name and the instant are stored, because an override that cannot be audited after
        the fact is indistinguishable from the clock-based inference this replaced.
        """
        if not str(attested_by or "").strip():
            raise GraphOperationError("attested_by is required: attestation must name a person")
        self._ensure_ready()
        now = _utc_now_iso()
        with connect_telemetry_db(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE graph_operations "
                "SET owner_death_attested_by = ?, owner_death_attested_at = ?, updated_at = ?, "
                # Bind the attestation to the EXACT token it is about. An operator attests that one
                # named process is dead, never that "whoever holds this row" is dead.
                "    owner_death_attested_for_token = owner_token "
                "WHERE op_id = ? "
                "  AND state = 'PREPARED' "
                "  AND owner_token IS NOT NULL "
                "  AND owner_lease_expires_at IS NOT NULL "
                "  AND owner_lease_expires_at <= ?",
                (str(attested_by).strip(), now, now, op_id, now),
            )
            conn.commit()
            return cursor.rowcount == 1
