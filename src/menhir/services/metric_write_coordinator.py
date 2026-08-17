"""MetricWriteCoordinator — the cross-store saga for every Metric write (Metric plan A6/C/E).

SQLite and Neo4j cannot share a transaction, so a Metric write is a recoverable saga:

    PREPARED   commit {graph_operations row + metric_receipts row} in ONE SQLite transaction
    MUTATE     idempotent, preconditioned Neo4j write keyed by op_id (ViewRepository.record_metric)
    VERIFY     compare the observed after-state fingerprint to the one frozen at PREPARE
    COMMITTED  only then mark the journal row committed
    RECONCILE  on startup, replay any row left PREPARED by a crash; drift -> NEEDS_REVIEW

Producers never call ``record_metric`` directly: the graph-only primitive cannot write a receipt,
so a direct caller could mint an unreceipted Metric. They go through ``record_telemetry_fold`` /
``record_run_tally`` here, which own both stores.

Everything replay-sensitive is FROZEN into request_json at PREPARE -- the target key, the chosen
metric UUID, the value/signature, the timestamps. A replay re-executes the frozen request; it never
regenerates a UUID or a timestamp, so replaying after a crash converges instead of forking.

Retention (owner-confirmed 2026-07-13): CHAINED ACCUMULATORS. Each receipt records only the delta
rows in (previous_cutoff, cutoff] and chains the prior digest + aggregate, so raw telemetry rows can
be pruned later without making the count or its lineage unverifiable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid as uuidlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from menhir.infrastructure.graph_operations import GraphOperationsJournal
from menhir.infrastructure.metric_receipts import MetricReceiptStore
from menhir.services.saga_writer_heartbeat import owned_mutation
from menhir.services.saga_reconcile_outcomes import (
    SKIP,
    WOULD_MARK_ALREADY_APPLIED,
    WOULD_NEEDS_REVIEW,
    WOULD_REPLAY,
    summarize_outcomes,
)

logger = logging.getLogger(__name__)

#: Bump when a fold's DEFINITION changes. A new query_version starts a new explicitly migrated
#: lineage; it never silently reinterprets a pruned history (plan C4).
QUERY_VERSION = "v1"

#: Only these telemetry tables may back a TELEMETRY_FOLD receipt.
_ALLOWED_SOURCE_TABLES = frozenset({"failure_events", "memory_revisions"})


class MetricGraphAdapter(Protocol):
    """The graph surface the coordinator needs (narrow, so producers can be faked in tests)."""

    def record_metric(self, **kwargs: Any) -> dict[str, Any]: ...
    def fetch_metric(
        self, *, subject: str, counter: str, namespace: str | None = None
    ) -> dict[str, Any] | None: ...
    def fetch_metric_state(self, *, view_key: str) -> dict[str, Any] | None:
        """Full PROTECTED state (uuid/value/type/labels/view_current/receipt_op) for fingerprinting.

        Distinct from fetch_metric (the public counter projection): the saga's precondition and
        after-state checks need labels and the type stamp, or a corrupted node fingerprints clean.
        """
        ...


class RunTallyRecorder(Protocol):
    """The narrow surface handed to perception/correction (plan A6).

    They get ONLY this -- not the telemetry store, not the graph journal -- so an instrumentation
    call site cannot bypass the saga or reach the graph directly.
    """

    def record_run_tally(
        self, *, subject: str, counter: str, value: float, namespace: str | None = ...,
        run_id: str | None = ...,
    ) -> dict[str, Any]: ...


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(payload: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace drift. The basis of every digest."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chain_digest(previous_digest: str | None, delta_row_ids: list[int], aggregate: float) -> str:
    """Chained accumulator digest (plan C4): H(previous_digest || canonical(delta) || aggregate).

    Chaining the PRIOR digest (not re-reading history) is what lets the raw rows be pruned while
    the lineage stays verifiable: each link commits to everything before it.
    """
    return _sha256(
        _canonical(
            {
                "prev": previous_digest or "",
                "delta_row_ids": sorted(int(x) for x in delta_row_ids),
                "aggregate": float(aggregate),
            }
        )
    )


#: Fingerprint of "no current Metric exists for this key" -- a legitimate before-state.
ABSENT = "absent"


def state_fingerprint(state: dict[str, Any] | None, *, view_key: str) -> str:
    """Fingerprint of a Metric node's PROTECTED state (plan E3).

    Covers everything the operation owns -- identity, key, value, LABEL SET, type stamp,
    currentness, and the receipt pointer -- so a node whose label or type was corrupted does NOT
    fingerprint as correct. Deliberately excludes volatile access timestamps (last_accessed), which
    change without the operation doing anything.

    ``None`` (no current Metric) fingerprints as ABSENT: the expected before-state of a first write.
    """
    if state is None:
        return _sha256(_canonical({"absent": True, "view_key": view_key}))
    return _sha256(
        _canonical(
            {
                "uuid": str(state.get("uuid") or ""),
                "view_key": view_key,
                "value": float(state.get("value") or 0.0),
                "type": str(state.get("type") or ""),
                "labels": sorted(str(x) for x in (state.get("labels") or [])),
                "view_current": bool(state.get("view_current", True)),
                "receipt_op_id": str(state.get("receipt_op_id") or ""),
                # Current-set cardinality (plan E, Phase 2). A changed write must leave EXACTLY one
                # current version; expected-after asserts 1. Folding it into the fingerprint means a
                # graph with two currents cannot match the expected after-state and routes to
                # NEEDS_REVIEW. Defaults to 1 so a caller/state without the field (e.g. a test fake
                # with a single current) fingerprints identically to the one-current graph read.
                "current_count": int(state.get("current_count", 1)),
            }
        )
    )


class MetricDrift(RuntimeError):
    """The graph is in neither the expected before-state nor the expected after-state."""


@dataclass
class MetricWriteCoordinator:
    """Owns the telemetry sidecar and the graph adapter; the ONLY sanctioned Metric writer."""

    graph_adapter: MetricGraphAdapter
    journal: GraphOperationsJournal
    receipts: MetricReceiptStore
    telemetry_db_path: Any = None  # Path; defaults to the journal's DB (same sidecar file)
    namespace: str = "agent-experience"
    _key_fn: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.telemetry_db_path is None:
            self.telemetry_db_path = self.journal.db_path
        # Ensure BOTH schemas exist up front. latest_for_key(committed_only=True) JOINs
        # graph_operations, and a fold READ can precede the first journal WRITE on a fresh DB --
        # without this the JOIN hits "no such table: graph_operations".
        self.journal._ensure_ready()
        self.receipts._ensure_ready()

    # ------------------------------------------------------------------ key helper
    @staticmethod
    def view_key(namespace: str | None, subject: str, counter: str) -> str:
        """Must match ViewRepository._key exactly -- the Metric's identity across both stores."""
        ns = (namespace or "").strip()
        return f"{ns}::{subject.strip().lower()}::{counter.strip().lower()}"

    # ------------------------------------------------------------------ public writes
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

    # ------------------------------------------------------------------ the saga
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
    ) -> dict[str, Any]:
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
        with sqlite3.connect(self.telemetry_db_path) as conn:
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

    def classify_prepared_row(self, row: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """Classify ONE PREPARED journal row. Pure: performs no durable mutation.

        ``row`` is a journal row dict (``op_id``, ``operation_kind``, ``request_json``,
        ...), NOT the parsed request. This thin wrapper layers the row-level concerns on
        top of ``_classify_replay``: the kind check, the ``request_json`` parse, and the
        "classifier raised" guard. A malformed row must never propagate an exception out
        of here -- one bad old row must not abort a scan and hide every newer row behind it.
        """
        operation_kind = row.get("operation_kind")
        if operation_kind != "METRIC_WRITE":
            return SKIP, {}
        try:
            request = json.loads(row["request_json"])
        except (TypeError, ValueError, KeyError):
            return WOULD_NEEDS_REVIEW, {"observed_error": "unparseable request_json"}
        # Narrow on purpose: these are the shapes a malformed ROW produces. A graph outage is not a
        # row defect, and folding it in would let a caller acting on this outcome quarantine a good
        # row over a transient failure. The dispatcher catches the rest, so an observe pass survives.
        try:
            return self._classify_replay(request)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            return (
                WOULD_NEEDS_REVIEW,
                {"observed_error": f"unclassifiable row: {type(exc).__name__}: {exc}"},
            )

    def _classify_replay(self, request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Classify a PREPARED request WITHOUT mutating anything (CF-20a observation contract).

        Returns ``(outcome, diagnostics)`` where ``outcome`` is one of the CF-20a replay
        vocabulary and ``diagnostics`` carries everything a live replay needs to act without
        re-reading the graph: ``op_id``, ``view_key``, ``observed``, ``observed_fp``,
        ``expected_before``, ``expected_after``, and for the two WOULD_NEEDS_REVIEW cases the
        exact ``observed_error`` string a live replay would pass to ``mark_needs_review``.

        Pure by construction: it only reads (the journal row and the graph) and never calls
        ``mark_committed``, ``mark_needs_review``, ``record_attempt``, or ``record_metric``.
        A dry-run can call it freely; ``_apply`` calls it exactly once and branches on the
        outcome, so the decision and the mutation cannot drift apart.
        """
        op_id = str(request["op_id"])
        key = str(request["view_key"])
        row = self.journal.get(op_id) or {}
        expected_after = row.get("expected_after_sha256")
        expected_before = request.get("expected_before_sha256")

        observed = self.graph_adapter.fetch_metric_state(view_key=key)
        observed_fp = state_fingerprint(observed, view_key=key)

        diag: dict[str, Any] = {
            "op_id": op_id,
            "view_key": key,
            "observed": observed,
            "observed_fp": observed_fp,
            "expected_before": expected_before,
            "expected_after": expected_after,
        }

        # (2) Already applied by a previous attempt that crashed before COMMITTED. The receipt
        # pointer proves THIS op produced it, so replaying is a no-op, not a conflict.
        if expected_after and observed_fp == expected_after:
            return WOULD_MARK_ALREADY_APPLIED, diag

        # (3) Neither expected state: refuse to mutate. A half-applied or externally-modified node
        # is an operator decision, not something a background replay should paper over.
        #
        # FAIL CLOSED: a request with no frozen precondition cannot be verified, so it must NOT be
        # applied. Waving it through would mutate a possibly-drifted graph with no check at all --
        # the opposite of the guarantee this contract exists to provide.
        if expected_before is None:
            diag["observed_error"] = (
                "request has no expected_before_sha256; cannot verify precondition"
            )
            return WOULD_NEEDS_REVIEW, diag
        if observed_fp != expected_before:
            diag["observed_error"] = (
                f"precondition drift: observed={observed_fp} "
                f"expected_before={expected_before} expected_after={expected_after}"
            )
            return WOULD_NEEDS_REVIEW, diag

        # (1) Expected before-state -> apply.
        return WOULD_REPLAY, diag

    def _apply(self, request: dict[str, Any]) -> dict[str, Any]:
        """Run the saga body under this process's ownership heartbeat (CF-211 part 2).

        The heartbeat is what lets a reconciler tell "still running here" from "crashed midway": it
        renews the claim on a thread, independently of the blocking driver call, and publishes a
        revocation predicate that stops any further statement being dispatched once the claim is
        lost. Wrapping the whole body means the claim is held from PREPARE through the terminal
        journal transition, which is the interval a reconciler must not replay across.

        The TTL is derived for METRIC_WRITE specifically, so its statement count -- not a shared
        constant -- determines how long an expired claim takes to become recoverable.
        """
        with owned_mutation(
            self.journal, str(request["op_id"]), operation_kind="METRIC_WRITE"
        ):
            return self._apply_owned(request)

    def _apply_owned(self, request: dict[str, Any]) -> dict[str, Any]:
        """Preconditioned, idempotent graph mutation for a PREPARED request (plan E3).

        Exactly three accepted states -- the precondition is checked BEFORE any mutation, so a
        drifted graph is never touched:

          1. observed == expected BEFORE-state  -> apply, verify the after-state, COMMITTED
          2. observed == expected AFTER-state   -> already applied (crash before COMMITTED):
                                                   no-op replay, COMMITTED
          3. anything else                      -> DO NOT MUTATE; NEEDS_REVIEW for an operator

        Used by both the initial write and reconciliation replay -- one code path, so a replay
        cannot diverge from the original.

        The pre-mutation decision lives in ``_classify_replay``; this method calls it once,
        branches on the outcome, and reuses the observed state out of the diagnostics so there
        is exactly one ``fetch_metric_state`` call on the precondition path.
        """
        outcome, diag = self._classify_replay(request)
        op_id = diag["op_id"]
        key = diag["view_key"]
        expected_after = diag["expected_after"]
        expected_before = diag["expected_before"]
        observed_fp = diag["observed_fp"]

        # (2) Already applied by a previous attempt that crashed before COMMITTED. The receipt
        # pointer proves THIS op produced it, so replaying is a no-op, not a conflict.
        if outcome == WOULD_MARK_ALREADY_APPLIED:
            self.journal.mark_committed(op_id)
            return {
                "uuid": str(request["metric_uuid"]), "view_key": key, "op_id": op_id,
                "created": False, "replayed": True,
            }

        # (3) Neither expected state: refuse to mutate. A half-applied or externally-modified node
        # is an operator decision, not something a background replay should paper over.
        #
        # FAIL CLOSED: a request with no frozen precondition cannot be verified, so it must NOT be
        # applied. Waving it through would mutate a possibly-drifted graph with no check at all --
        # the opposite of the guarantee this contract exists to provide.
        if outcome == WOULD_NEEDS_REVIEW:
            self.journal.mark_needs_review(op_id, observed_error=diag["observed_error"])
            if expected_before is None:
                raise MetricDrift(
                    f"op {op_id} for {key!r} has no frozen precondition; NOT mutating (fail closed)"
                )
            raise MetricDrift(
                f"precondition drift for {key!r} (op {op_id}): the graph is in neither the "
                f"expected before- nor after-state; NOT mutating"
            )

        # (1) Expected before-state -> apply.
        try:
            res = self.graph_adapter.record_metric(
                subject=request["subject"],
                counter=request["counter"],
                value=float(request["value"]),
                namespace=request["namespace"],
                valid_at=request["valid_at"],
                source=request["source"],
                receipt_op_id=op_id,
                # the uuid frozen at PREPARE: a replay recreates THIS node, never a fork
                node_uuid=str(request["metric_uuid"]),
            )
        except Exception as exc:  # noqa: BLE001
            self.journal.record_attempt(op_id, error=f"{type(exc).__name__}: {exc}")
            raise

        # LWW stale-skip: the counter kind is an LWW register, so _write_version REFUSES to let a
        # temporally-older value overwrite a newer current version -- it returns without writing.
        # The fence serialises ops (the frozen valid_at is never older than the current node's), so
        # this should be unreachable. Handle it explicitly anyway: silently falling through would
        # produce a cryptic "fingerprint mismatch" and fence the key, when the real cause is a stale
        # replay. Fail with an accurate diagnosis instead of a misleading one.
        if res.get("stale_skipped"):
            self.journal.mark_needs_review(
                op_id,
                observed_error=(
                    "LWW stale-skip: the graph holds a NEWER version than this operation's frozen "
                    f"valid_at ({request.get('valid_at')}); the write was refused, not applied"
                ),
            )
            raise MetricDrift(
                f"stale replay for {key!r} (op {op_id}): a newer version is current; NOT applied"
            )

        # Verify the mutation produced exactly the after-state frozen at PREPARE.
        after = self.graph_adapter.fetch_metric_state(view_key=key)
        actual = state_fingerprint(after, view_key=key)
        if expected_after and actual != expected_after:
            self.journal.mark_needs_review(
                op_id, observed_error=f"after-state mismatch expected={expected_after} actual={actual}"
            )
            raise MetricDrift(
                f"after-state drift for {key!r} (op {op_id}): expected {expected_after}, "
                f"observed {actual}"
            )

        self.journal.mark_committed(op_id)
        res["op_id"] = op_id
        return res

    # ------------------------------------------------------------------ reconciliation
    def reconcile(self, *, limit: int = 500, dry_run: bool = True) -> dict[str, Any]:
        """Classify the PREPARED backlog for this saga kind. Observation only.

        Live replay is NOT available here. A per-coordinator sweep cannot acquire the global
        PREPARE gate, cannot establish that a row's original writer is gone, and cannot atomically
        claim an abandoned row before touching the graph -- so replaying from here would mutate
        rows another process may still be executing. There is exactly one live replay authority,
        and it is the central dispatcher.

        The heartbeat that ``_apply`` opens does not close that hole either: it renews on an
        interval, so a reconciler acting on somebody else's row would dispatch its first mutation
        before the first renewal discovered the row was never its to claim.

        ``dry_run`` now defaults to True. Passing False raises rather than silently observing, so a
        caller cannot believe recovery ran.
        """
        if not dry_run:
            raise NotImplementedError(
                "per-coordinator live reconciliation is disabled: recovery must go through the "
                "central dispatcher, which holds the reconciliation gate, checks operation "
                "ownership, and claims an abandoned row before mutating. Use reconcile() to "
                "classify, or _replay_prepared() from an authority that already owns the rows."
            )
        return self._reconcile_sweep(limit=limit, dry_run=True)

    def _replay_prepared(self, *, limit: int = 500) -> dict[str, Any]:
        """The live replay sweep. Callable ONLY by an authority that already owns the rows.

        Private and unreachable through reconcile(). A caller must have taken the reconciliation
        gate, established that each row's original writer is gone, and claimed the row -- none of
        which this method does or can check for itself.

        It exists under a separate name rather than being deleted because it is the saga's only
        executable replay implementation, and the crash-recovery invariants it satisfies still have
        to be provable: a PREPARED row replays exactly once, drift quarantines without mutating, a
        missing precondition fails closed. Deleting it would have removed that evidence along with
        the unsafe entry point.
        """
        return self._reconcile_sweep(limit=limit, dry_run=False)

    def _reconcile_sweep(self, *, limit: int = 500, dry_run: bool = False) -> dict[str, Any]:
        """Replay every PREPARED METRIC_WRITE left by a crash (plan E4).

        Runs at startup, AFTER schema readiness and BEFORE scheduler jobs register, so no new
        writer competes with an in-flight operation. Replay is idempotent: record_metric is
        signature-idempotent and the request is frozen, so re-applying a completed mutation
        converges to the same node instead of creating a second version.

        Drift is reported, never repaired: a NEEDS_REVIEW row stays for an operator.

        With ``dry_run=True`` the same deterministic decision is made via ``_classify_replay``
        for every row but NOTHING is mutated -- no ``_apply``, no ``mark_needs_review``, no
        graph write. Each row is classified and reported in ``outcomes``; live mode reaches the
        same classification and then acts on it, so a dry-run summary and a live replay can
        never diverge.
        """
        replayed = 0
        drifted = 0
        failed = 0
        scanned = 0
        outcomes: list[dict[str, Any]] = []
        for row in self.journal.list_by_state("PREPARED", limit=limit):
            scanned += 1
            operation_kind = row.get("operation_kind")
            op_id = str(row["op_id"])
            if operation_kind != "METRIC_WRITE":
                if dry_run:
                    outcomes.append(
                        {"op_id": op_id, "operation_kind": operation_kind, "outcome": SKIP}
                    )
                continue
            try:
                request = json.loads(row["request_json"])
            except (TypeError, ValueError):
                if dry_run:
                    outcomes.append(
                        {"op_id": op_id, "operation_kind": operation_kind,
                         "outcome": WOULD_NEEDS_REVIEW,
                         "observed_error": "unparseable request_json"}
                    )
                else:
                    self.journal.mark_needs_review(op_id, observed_error="unparseable request_json")
                    drifted += 1
                continue
            if dry_run:
                # A malformed row must not abort the scan -- see the note in MergeCoordinator:
                # one unclassifiable old row must never hide the newer rows behind it. The
                # row-level guard lives in classify_prepared_row so the dispatcher and a direct
                # dry-run can never disagree.
                outcome, diag = self.classify_prepared_row(row)
                entry: dict[str, Any] = {
                    "op_id": op_id, "operation_kind": operation_kind, "outcome": outcome
                }
                if "observed_error" in diag:
                    entry["observed_error"] = diag["observed_error"]
                outcomes.append(entry)
                continue
            try:
                self._apply(request)
                replayed += 1
                logger.info("metric saga: replayed PREPARED op %s (%s)", op_id, request.get("view_key"))
            except MetricDrift:
                drifted += 1
                logger.warning("metric saga: op %s drifted; left NEEDS_REVIEW", op_id)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.warning("metric saga: replay of op %s failed: %s", op_id, exc)
        result: dict[str, Any] = {"replayed": replayed, "drifted": drifted, "failed": failed}
        if dry_run:
            # replayed/drifted/failed stay 0 in dry-run: they count actions PERFORMED, and a
            # dry-run performs none. The forecast lives in counts/outcomes instead.
            result["dry_run"] = True
            result["scanned"] = scanned
            result["counts"] = summarize_outcomes(outcomes)
            result["outcomes"] = outcomes
        return result
