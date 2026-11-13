"""How a saga writer settles its journal row when the mutation fails or its commit is refused.

Two decisions every coordinator used to make locally, and got wrong in the same way:

* **A mutation raised (#427).** The row used to stay PREPARED for a reconciler, keeping its
  participant locks; in ``live`` startup mode it then blocked boot as OWNER_UNKNOWN. A row may move
  to FAILED ("no graph mutation") only on proof: the server itself reported the statement rolled
  back, AND a re-read shows the graph at the op's before-state. A connection-class error is never
  proof -- the server may have committed after the client lost the socket -- so it stays PREPARED.

* **A commit was refused after this writer's graph work (#404).** Whether the journal's claim is
  still true depends on who did graph work: see :func:`settle_refused_commit`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from menhir.infrastructure.graph_operations_transitions import StaleTransitionError

try:  # pragma: no cover - import guard mirrors infrastructure/neo4j.py
    from neo4j.exceptions import ClientError, TransientError
except ImportError:  # pragma: no cover
    ClientError = TransientError = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

#: :func:`commit_or_settle` results.
COMMITTED_BY_THIS_WRITER = "committed"
COMPLETED_BY_OTHER_OWNER = "completed_by_other_owner"


def server_rolled_back(exc: BaseException) -> bool:
    """Whether ``exc`` proves the server rolled the statement back.

    True only for errors the SERVER reported for the statement: ``ClientError`` (which includes
    ``ConstraintError``, e.g. a plain DELETE of a node that gained a relationship) and
    ``TransientError`` (retried by ``Neo4jRepository.execute``, each attempt rolled back). False for
    every ``DriverError`` (``ServiceUnavailable``, ``SessionExpired``: the commit may have landed),
    ``DatabaseError`` (an internal server failure, not asserted to be a clean rollback), and every
    non-Neo4j exception (which may have been raised after the statement committed).
    """
    if ClientError is None:
        return False
    return isinstance(exc, (ClientError, TransientError))


def close_after_failed_mutation(
    journal: Any,
    op_id: str,
    *,
    owner_token: str,
    exc: BaseException,
    prove_before: Callable[[], bool],
) -> bool:
    """Record the failed attempt, then close the row FAILED if -- and only if -- it is provable.

    Returns whether the row was closed. Never swallows ``exc``: the caller re-raises it either way.
    ``prove_before`` re-reads the graph and returns True only when it is at the op's before-state;
    if it raises, nothing is proven and the row stays PREPARED for a reconciler.
    """
    error = f"{type(exc).__name__}: {exc}"
    try:
        if not journal.record_attempt(op_id, owner_token=owner_token, error=error):
            return False  # the claim is gone; whoever holds the row now decides it
    except Exception:  # the caller re-raises the mutation's error; this must not replace it
        logger.warning("op %s: could not record the failed attempt", op_id, exc_info=True)
        return False
    if not server_rolled_back(exc):
        return False
    try:
        proven = bool(prove_before())
    except Exception:  # an unreadable graph proves nothing
        logger.warning(
            "op %s: server rolled back, but the before-state re-read failed; leaving PREPARED",
            op_id, exc_info=True,
        )
        return False
    if not proven:
        logger.warning(
            "op %s: server rolled back, but the graph is not at the before-state; leaving PREPARED",
            op_id,
        )
        return False
    try:
        journal.mark_failed(
            op_id,
            owner_token=owner_token,
            reason=f"rolled back by the server; graph re-read at the before-state: {error}",
        )
    except StaleTransitionError as stale:
        logger.warning("op %s: claim lost before FAILED could be written: %s", op_id, stale)
        return False
    except Exception:  # same: never replace the mutation's own error
        logger.warning("op %s: could not write FAILED; leaving PREPARED", op_id, exc_info=True)
        return False
    logger.info("op %s closed FAILED after a server rollback (#427): %s", op_id, error)
    return True


def settle_refused_commit(
    journal: Any,
    op_id: str,
    *,
    owner_token: str,
    did_graph_work: bool,
    observed: str,
) -> bool:
    """Decide what a refused transition means for the op. True = report success; False = re-raise.

    Called when this writer's ``mark_committed`` (or, after an abstention, ``mark_needs_review``)
    raised :class:`StaleTransitionError` because another owner had already moved the row.

    ===========================  ==================  =====================  ======================
    row                          this writer's work  committer's work       action
    ===========================  ==================  =====================  ======================
    FAILED                       yes                 --                     escalate; re-raise
    FAILED                       no                  --                     re-raise (FAILED true)
    COMMITTED by another owner   yes                 yes / unknown (NULL)   escalate; re-raise
    COMMITTED by another owner   yes                 no                     success (one apply)
    COMMITTED by another owner   no                  any                    success (by recovery)
    anything else                --                  --                     re-raise
    ===========================  ==================  =====================  ======================

    "Success" means the operation's outcome is one verified application, so the forward caller can
    report it (FR-6) -- the committer verified the after-state before committing. Escalation
    (``escalate_after_refused_commit``) re-fences the participants and quarantines the row.
    """
    row = journal.get(op_id) or {}
    state = row.get("state")
    if state == "FAILED":
        if did_graph_work:
            journal.escalate_after_refused_commit(
                op_id,
                owner_token=owner_token,
                observed_error=(
                    f"writer graph work landed after the row was FAILED (#404): {observed}"
                ),
            )
        return False
    if state == "COMMITTED" and row.get("resolved_by_token") != owner_token:
        if not did_graph_work:
            logger.warning(
                "op %s was completed by another owner (%s); this writer did no graph work",
                op_id, row.get("resolved_by_token"),
            )
            return True
        if row.get("commit_graph_work") == 0:
            logger.warning(
                "op %s was committed by another owner that only observed this writer's applied "
                "state; one application", op_id,
            )
            return True
        journal.escalate_after_refused_commit(
            op_id,
            owner_token=owner_token,
            observed_error=(
                "possible double apply: this writer and the committing owner both did graph "
                f"work (#404): {observed}"
            ),
        )
        return False
    return False


def commit_or_settle(
    journal: Any,
    op_id: str,
    *,
    owner_token: str,
    after_graph_work: bool,
    observed: str,
) -> str:
    """``mark_committed``, settling a refusal through :func:`settle_refused_commit`.

    Returns ``COMMITTED_BY_THIS_WRITER`` or ``COMPLETED_BY_OTHER_OWNER``; re-raises the
    ``StaleTransitionError`` when the refusal is not a single verified application.
    """
    try:
        journal.mark_committed(op_id, owner_token=owner_token, after_graph_work=after_graph_work)
    except StaleTransitionError:
        if settle_refused_commit(
            journal, op_id, owner_token=owner_token,
            did_graph_work=after_graph_work, observed=observed,
        ):
            return COMPLETED_BY_OTHER_OWNER
        raise
    return COMMITTED_BY_THIS_WRITER


def quarantine_after_graph_work(
    journal: Any, op_id: str, *, owner_token: str, observed_error: str
) -> None:
    """``mark_needs_review`` after this writer's own graph work, escalating if it is refused.

    A refusal here means another owner already closed the row while this writer's mutation landed
    and then failed its own after-state check. FAILED would now claim "no graph mutation" over a
    graph this writer changed, and COMMITTED would vouch for a state this writer just saw drift, so
    either way the row is re-fenced and quarantined (#404). The refusal is re-raised regardless.
    """
    try:
        journal.mark_needs_review(op_id, owner_token=owner_token, observed_error=observed_error)
    except StaleTransitionError:
        row = journal.get(op_id) or {}
        if row.get("state") in ("FAILED", "COMMITTED") and (
            row.get("resolved_by_token") != owner_token
        ):
            journal.escalate_after_refused_commit(
                op_id,
                owner_token=owner_token,
                observed_error=(
                    f"writer's after-state check failed after its graph work while the row was "
                    f"{row.get('state')} by another owner (#404): {observed_error}"
                ),
            )
        raise


__all__ = [
    "COMMITTED_BY_THIS_WRITER",
    "COMPLETED_BY_OTHER_OWNER",
    "close_after_failed_mutation",
    "commit_or_settle",
    "quarantine_after_graph_work",
    "server_rolled_back",
    "settle_refused_commit",
]
