"""Write-path half of the Metric saga: the producer entry points and the PREPARE step.

``MetricSagaWriteMixin`` carries ``record_run_tally`` / ``record_telemetry_fold`` /
``record_telemetry_absolute`` / ``fetch_metric`` and the ``_saga_write`` body that freezes the
request, commits the journal row and the receipt in ONE SQLite transaction, and hands off to the
apply half. It is mixed into :class:`~menhir.services.metric_write_coordinator.MetricWriteCoordinator`,
which remains the only public import surface for producers.
"""

from __future__ import annotations

import uuid as uuidlib

from typing import Any

from menhir.clock import utc_now_iso as _utc_now_iso
from menhir.infrastructure.telemetry import connect_telemetry_db
from menhir.services.metric_write_coordinator_common import (
    MetricChainConflict,
    _canonical,
    chain_digest,
    state_fingerprint,
)

#: Bump when a fold's DEFINITION changes. A new query_version starts a new explicitly migrated
#: lineage; it never silently reinterprets a pruned history (plan C4).
QUERY_VERSION = "v1"

#: Only these telemetry tables may back a TELEMETRY_FOLD receipt.
_ALLOWED_SOURCE_TABLES = frozenset({"failure_events", "memory_revisions"})


class MetricSagaWriteMixin:
    """Producer entry points plus the PREPARE half of the saga.

    Expects the host dataclass (:class:`MetricWriteCoordinator`) to provide ``graph_adapter``,
    ``receipts``, ``journal``, ``telemetry_db_path``, ``namespace``, and the ``view_key``
    staticmethod.
    """

    def record_run_tally(
        self,
        *,
        subject: str,
        counter: str,
        value: float,
        namespace: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """A job-level instrumentation tally (perception abstentions, correction outcomes).

        RUN_TALLY receipts have no source table and no cutoff: the value IS the run's observation,
        not a fold over retained rows. Honest by construction -- it never claims a lineage it
        cannot reproduce.
        """
        ns = namespace if namespace is not None else self.namespace
        return self._saga_write(
            subject=subject,
            counter=counter,
            value=float(value),
            namespace=ns,
            receipt_kind="RUN_TALLY",
            source="instrumentation",
            run_id=run_id,
            source_table=None,
            grouping=None,
            cutoff_id=None,
            receipt_row_ids=[],
        )

    def record_telemetry_fold(
        self,
        *,
        subject: str,
        counter: str,
        source_table: str,
        grouping: dict[str, Any],
        cutoff_id: int,
        delta_row_ids: list[int],
        delta_count: int,
        namespace: str | None = None,
        source: str = "failure-telemetry",
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """A cutoff-bound fold over a telemetry table, accumulated onto the prior receipt (C3/C4).

        ``delta_count`` is the count of NEW rows in (previous_cutoff, cutoff]; the new aggregate is
        the prior committed aggregate plus that delta. The raw rows may later be pruned -- the
        chained digest keeps the lineage verifiable regardless.
        """
        if source_table not in _ALLOWED_SOURCE_TABLES:
            raise ValueError(f"source_table {source_table!r} is not allowlisted")
        ns = namespace if namespace is not None else self.namespace
        key = self.view_key(ns, subject, counter)

        prior = self.receipts.latest_for_key(key)
        prior_aggregate = float(prior["aggregate_value"]) if prior else 0.0
        prior_digest = prior["input_digest"] if prior else None
        prior_op = prior["op_id"] if prior else None

        aggregate = prior_aggregate + float(delta_count)
        digest = chain_digest(prior_digest, delta_row_ids, aggregate)

        return self._saga_write(
            subject=subject,
            counter=counter,
            value=aggregate,
            namespace=ns,
            receipt_kind="TELEMETRY_FOLD",
            source=source,
            run_id=run_id,
            source_table=source_table,
            grouping=grouping,
            cutoff_id=cutoff_id,
            receipt_row_ids=delta_row_ids,
            previous_receipt_op_id=prior_op,
            input_digest=digest,
            source_row_count=int(delta_count),
            # `prior` was read outside any transaction; this makes the write conditional on the
            # head still being it (CF-244).
            enforce_chain_head=True,
        )

    def record_telemetry_absolute(
        self,
        *,
        subject: str,
        counter: str,
        source_table: str,
        grouping: dict[str, Any],
        cutoff_id: int,
        absolute_count: float,
        row_ids: list[int],
        namespace: str | None = None,
        source: str = "failure-telemetry",
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """A cutoff-bound fold recorded as an ABSOLUTE lifetime count (the no-prune path).

        Unlike record_telemetry_fold (chained deltas, for when raw rows get pruned), this stores the
        full recomputed count at ``cutoff_id`` and the complete contributing ``row_ids``. It is
        always correct without any cross-receipt arithmetic -- the honest choice when telemetry is
        retained, which menhir currently does. The receipt still chains ``previous_receipt_op_id``
        for history and its digest commits to the full row set, so the lineage is auditable; it just
        does not DERIVE the aggregate from the chain.

        Idempotent by value: if the absolute count is unchanged, the saga's unchanged-value path
        refreshes the receipt pointer and commits without a new version.
        """
        if source_table not in _ALLOWED_SOURCE_TABLES:
            raise ValueError(f"source_table {source_table!r} is not allowlisted")
        ns = namespace if namespace is not None else self.namespace
        key = self.view_key(ns, subject, counter)
        prior = self.receipts.latest_for_key(key)
        prior_op = prior["op_id"] if prior else None
        # Digest commits to the ABSOLUTE row set at this cutoff (not a delta), chaining the prior
        # digest for continuity. Verifiable as long as the rows exist; this path assumes they do.
        digest = chain_digest(prior["input_digest"] if prior else None, row_ids, float(absolute_count))

        return self._saga_write(
            subject=subject,
            counter=counter,
            value=float(absolute_count),
            namespace=ns,
            receipt_kind="TELEMETRY_FOLD",
            source=source,
            run_id=run_id,
            source_table=source_table,
            grouping=grouping,
            cutoff_id=cutoff_id,
            receipt_row_ids=row_ids,
            previous_receipt_op_id=prior_op,
            input_digest=digest,
            source_row_count=int(absolute_count),
        )

    def fetch_metric(
        self, *, subject: str, counter: str, namespace: str | None = None
    ) -> dict[str, Any] | None:
        """Unchanged-value probe for producers (avoids a pointless saga on a no-op)."""
        ns = namespace if namespace is not None else self.namespace
        return self.graph_adapter.fetch_metric(subject=subject, counter=counter, namespace=ns)

    def _saga_write(
        self,
        *,
        subject: str,
        counter: str,
        value: float,
        namespace: str,
        receipt_kind: str,
        source: str,
        run_id: str | None,
        source_table: str | None,
        grouping: dict[str, Any] | None,
        cutoff_id: int | None,
        receipt_row_ids: list[int],  # the rows THIS receipt records: a delta set for the
                                     # accumulator path, the absolute set for the recompute path
        previous_receipt_op_id: str | None = None,
        input_digest: str | None = None,
        source_row_count: int | None = None,
        enforce_chain_head: bool = False,
    ) -> dict[str, Any]:
        # `enforce_chain_head` is opt-in because only the ACCUMULATOR path chains: it derives its
        # aggregate from the prior receipt, so a moved head invalidates the value it is about to
        # write. The recompute path derives its aggregate from the source rows themselves and is
        # correct regardless of what the head is, so forcing the check there would reject writes
        # that are perfectly valid.
        key = self.view_key(namespace, subject, counter)
        op_id = uuidlib.uuid4().hex

        # Freeze EVERY replay-sensitive input before any graph contact. A replay re-executes this
        # exact request -- it never regenerates a uuid, a timestamp, or a value.
        #
        # Which uuid to freeze depends on whether the value CHANGES: an unchanged value refreshes
        # the existing node in place (reuse its uuid), while a changed value creates a NEW version
        # node and supersedes the old one (mint a new uuid -- reusing the old one would collide with
        # the metric_uuid_unique constraint).
        before = self.graph_adapter.fetch_metric_state(view_key=key)
        unchanged = (
            before is not None
            and before.get("uuid")
            and float(before.get("value", float("nan"))) == float(value)
        )
        metric_uuid = str(before["uuid"]) if unchanged else str(uuidlib.uuid4())

        # The state the mutation will produce: same node refreshed (unchanged) or a NEW current
        # version carrying this op's receipt (changed). Frozen now so a replay can recognise
        # "already applied" without re-deriving it.
        expected_after_state = {
            "uuid": metric_uuid,
            "value": float(value),
            "type": "METRIC",
            "labels": ["Metric"],
            "view_current": True,
            "receipt_op_id": op_id,
            # Exactly one current version after a well-formed write. If the graph holds more, the
            # observed fingerprint won't match this and the saga routes to NEEDS_REVIEW.
            "current_count": 1,
        }
        request = {
            "op_id": op_id,
            "subject": subject,
            "counter": counter,
            "value": float(value),
            "namespace": namespace,
            "view_key": key,
            "metric_uuid": metric_uuid,
            "source": source,
            "valid_at": _utc_now_iso(),
            "receipt_kind": receipt_kind,
            # Preconditions frozen at PREPARE (plan E3's three accepted states).
            "expected_before_sha256": state_fingerprint(before, view_key=key),
        }
        expected_after = state_fingerprint(expected_after_state, view_key=key)

        # (1) PREPARED -- journal row AND receipt commit together, in ONE SQLite transaction.
        # If this commit fails, nothing was written and no graph mutation has occurred.
        #
        # `connect_telemetry_db` rather than a bare `sqlite3.connect`: the seam applies the busy
        # timeout, without which the second writer of a contended chain fails instantly with
        # `database is locked` instead of waiting for the first to finish (CF-144's seam, and the
        # chain check below makes contention here a normal event rather than a rarity).
        # Both stores lazily CREATE TABLE on first use, each on a connection of its own. Under the
        # BEGIN IMMEDIATE below that DDL would block on the write lock this transaction is holding
        # and fail on the busy timeout -- a self-deadlock reachable only on the first fold of a
        # process. Warm them before the lock is taken; both are no-ops afterwards.
        self.receipts._ensure_ready()
        self.journal._ensure_ready()
        with connect_telemetry_db(self.telemetry_db_path) as conn:
            if enforce_chain_head:
                # (CF-244) COMPARE-AND-SET on the chain head. `previous_receipt_op_id` was read
                # OUTSIDE any transaction, and `latest_for_key` only counts COMMITTED ops -- so a
                # concurrent fold that had already PREPAREd was invisible to it, and both folds
                # chained from the same parent. Whichever committed second overwrote the other's
                # aggregate (one delta silently lost) and both receipts claimed the same
                # `previous_receipt_op_id`, forking the digest chain that exists to make the fold
                # auditable.
                #
                # BEGIN IMMEDIATE takes the write lock before the re-read, so the head cannot
                # change between checking it and inserting on top of it. A losing writer raises
                # rather than proceeding: a fold is safe to retry, a forked chain is not
                # repairable after the fact.
                conn.execute("BEGIN IMMEDIATE")
                head = self.receipts.latest_for_key(key, conn=conn)
                head_op = str(head["op_id"]) if head else None
                if head_op != previous_receipt_op_id:
                    raise MetricChainConflict(
                        f"metric chain head moved for {key!r}: this fold accumulated onto "
                        f"{previous_receipt_op_id!r} but the committed head is now {head_op!r}; "
                        "the fold was not applied and is safe to retry"
                    )
            self.journal.prepare(
                operation_kind="METRIC_WRITE",
                request_json=_canonical(request),
                target_uuid=metric_uuid,
                target_key=key,
                expected_after_sha256=expected_after,
                op_id=op_id,
                conn=conn,
            )
            self.receipts.append(
                op_id=op_id,
                receipt_kind=receipt_kind,
                metric_uuid=metric_uuid,
                view_key=key,
                aggregate_value=float(value),
                source_table=source_table,
                grouping_json=_canonical(grouping) if grouping is not None else None,
                query_version=QUERY_VERSION,
                previous_receipt_op_id=previous_receipt_op_id,
                cutoff_id=cutoff_id,
                source_row_count=source_row_count,
                source_row_ids_json=_canonical(sorted(receipt_row_ids)) if receipt_row_ids else None,
                input_digest=input_digest,
                run_id=run_id,
                conn=conn,
            )
            conn.commit()

        # (2) MUTATE + (3) VERIFY + (4) COMMITTED
        return self._apply(request)
