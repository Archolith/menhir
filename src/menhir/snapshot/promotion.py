"""Drive a snapshot into a project's canonical view, and undo it when it is wrong.

Plan: `.agent/plans/menhir-mcp-snapshot-ingest-2026-09-16.md` (P4).
Design: `.agent/plans/menhir-snapshot-p4-graph-write-design-2026-09-17.md`.

`view_root` owns what a root is. `canonical_view` owns the flip. This owns the ORDER, which is
where the gate's failure cases live: kill at every state, a failed write, a failed compensation,
and a restore from `previous`.

    begin -> write -> complete -> VERIFY -> flip -> (verify again) -> compensate on failure

**Verification happens before the flip, and that placement is the design.** Everything up to the
flip is invisible: the root is unreferenced, so a failure there is discarded rather than undone,
and P3's "a failure leaves nothing" property is recovered for the part of the work that can have
it. Only after the flip does undo become a real problem, and by then the only thing that moved is
one pointer.

**What each state looks like to a process that dies in it.** The point of the lifecycle is that
none of these need a recovery routine that reasons about what the dead process was doing:

| Killed... | What survives | Who cleans it, and on what evidence |
| --- | --- | --- |
| before `begin_root` | nothing | nobody; a retry starts clean |
| mid-write | a BUILDING root, unreferenced | the sweeper, once the LEASE expires |
| after `complete_root` | a COMPLETE root, unreferenced | the sweeper, on the same evidence |
| after the flip | the promotion, which succeeded | nobody; a retry sees it is already current |
| mid-compensation | a view pointing at the bad root plus durable attempt | scheduled reconciler |

Nothing in that table is cleaned up because a PID vanished, because a directory looked stale, or
because enough time passed on some other machine's clock. Every row that reclaims anything does it
on a lease expiring against the SERVER's clock.

The flip is also bracketed by a durable promotion-attempt record. A process killed after the flip
therefore leaves evidence that the reconciler can use to restore the previous root (or mark the
view degraded when restoration is impossible). The attempt is written before publication, never
reconstructed from process identity or timing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from menhir.snapshot.canonical_view import (
    ERR_VIEW_ALREADY_CURRENT,
    ViewError,
    mark_degraded,
    publish_root,
    read_view,
    restore_previous,
)
from menhir.snapshot.promotion_attempt import (
    begin_attempt,
    finish_attempt,
    mark_attempt_compensating,
    mark_attempt_published,
)
from menhir.snapshot.view_root import (
    DEFAULT_ROOT_LEASE_SECONDS,
    RootError,
    begin_root,
    complete_root,
    find_publishable_root,
    renew_root,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ERR_PROMOTION_DEGRADED",
    "ERR_PROMOTION_LEASE_LOST",
    "ERR_PROMOTION_VERIFY_FAILED",
    "ERR_PROMOTION_WRITE_FAILED",
    "PromotionError",
    "PromotionOutcome",
    "promote_snapshot",
]

ERR_PROMOTION_WRITE_FAILED = "snapshot.promotion.write_failed"
ERR_PROMOTION_LEASE_LOST = "snapshot.promotion.lease_lost"
ERR_PROMOTION_VERIFY_FAILED = "snapshot.promotion.verify_failed"
ERR_PROMOTION_DEGRADED = "snapshot.promotion.degraded"


class PromotionError(RuntimeError):
    """A promotion that did not publish, or published and was undone.

    Carries a code and whether the view was left degraded, never a path or file content.
    """

    def __init__(self, code: str, message: str, *, degraded: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.degraded = degraded


@dataclass(frozen=True)
class PromotionOutcome:
    """What a promotion did. `reused_root` and `already_current` are the two retry shapes."""

    root_id: str
    generation: int
    #: True when a COMPLETE root built by an earlier attempt was adopted instead of rebuilt.
    reused_root: bool
    #: True when the flip had ALREADY happened -- the first attempt succeeded and the caller never
    #: found out. The promotion is complete; nothing was written this time.
    already_current: bool


def promote_snapshot(
    neo4j: Any,
    *,
    project_id: str,
    view_key: str,
    snapshot_id: str,
    actor: str,
    display_name: str = "",
    write_structure: Callable[[str, Callable[[], None]], None],
    verify_before_flip: Callable[[str], None] | None = None,
    verify_after_flip: Callable[[str], None] | None = None,
    lease_seconds: int = DEFAULT_ROOT_LEASE_SECONDS,
) -> PromotionOutcome:
    """Build a root for `snapshot_id` and make it the view's current root.

    `write_structure` is handed the root id and a `renew` callable, and must tag everything it
    writes with that root. It is an argument rather than an import because the state machine here
    is the part worth proving, and a test that can fail the write at a chosen point proves more
    about it than any real writer would.

    **`renew` is not optional politeness.** A lease checked once before a long write is a lease
    that expires in the middle of it, and an expired lease means `complete_root` refuses and the
    work is discarded. A writer that batches should renew between batches.

    A failure before the flip raises and leaves an unreferenced root for the sweeper. A failure
    after it attempts a restore, and marks the view degraded if that restore also fails.
    """
    reused = find_publishable_root(
        neo4j, project_id=project_id, view_key=view_key, snapshot_id=snapshot_id
    )
    if reused is not None:
        root_id = reused.root_id
    else:
        root_id = begin_root(
            neo4j,
            project_id=project_id,
            view_key=view_key,
            snapshot_id=snapshot_id,
            lease_seconds=lease_seconds,
        ).root_id

        def _renew() -> None:
            renew_root(neo4j, root_id=root_id, lease_seconds=lease_seconds)

        try:
            write_structure(root_id, _renew)
        except BaseException as exc:
            # Deliberately BaseException. A KeyboardInterrupt here must not leave a root that looks
            # finished -- and it cannot, because nothing marks it COMPLETE. The root stays BUILDING,
            # unreferenced and unpublishable, and the sweeper reclaims it on lease expiry. There is
            # nothing to roll back, which is the entire reason the write happens off to the side.
            logger.warning(
                "Promotion write failed before the flip; the root stays unreferenced and will be "
                "reclaimed when its lease expires",
                exc_info=True,
            )
            raise PromotionError(
                ERR_PROMOTION_WRITE_FAILED,
                "the snapshot could not be written; nothing was published",
            ) from exc

        try:
            complete_root(neo4j, root_id=root_id)
        except RootError as exc:
            # The lease lapsed while the write ran, so the sweeper was entitled to reclaim this
            # root and the builder can no longer prove what it wrote is still there. Losing the
            # work is the correct outcome; publishing it would not be. Surfaced as a PromotionError
            # like every other pre-flip failure, so a caller needs one except clause rather than
            # two for outcomes that are identical from the outside: nothing was published.
            raise PromotionError(
                ERR_PROMOTION_LEASE_LOST,
                "the build outlived its lease and was not published; renew during long writes",
            ) from exc

    view = read_view(neo4j, project_id=project_id, view_key=view_key)
    expected = view.generation if view is not None else 0
    attempt = begin_attempt(
        neo4j,
        project_id=project_id,
        view_key=view_key,
        snapshot_id=snapshot_id,
        root_id=root_id,
        actor=actor,
        expected_generation=expected,
    )

    # This check is intentionally after root completion and durable intent creation, immediately
    # before the atomic pointer move. The coordinator uses it to renew its filesystem processing
    # fence; running it earlier would let the lease expire (or be superseded) while complete_root,
    # view resolution, and attempt creation ran, after which a stale publisher could still flip.
    if verify_before_flip is not None:
        try:
            verify_before_flip(root_id)
        except BaseException as exc:
            finish_attempt(neo4j, attempt_id=attempt.attempt_id, state="ABANDONED")
            raise PromotionError(
                ERR_PROMOTION_WRITE_FAILED,
                "the snapshot could not be verified; nothing was published",
            ) from exc

    try:
        published = publish_root(
            neo4j,
            project_id=project_id,
            view_key=view_key,
            root_id=root_id,
            expected_generation=expected,
            actor=actor,
            display_name=display_name,
        )
    except ViewError as exc:
        if exc.code == ERR_VIEW_ALREADY_CURRENT:
            # The previous attempt published and the caller never learned it. Reporting this as a
            # failure would invite a rebuild-and-republish of a snapshot that is already live.
            current = read_view(neo4j, project_id=project_id, view_key=view_key)
            finish_attempt(neo4j, attempt_id=attempt.attempt_id, state="SUCCEEDED")
            return PromotionOutcome(
                root_id=root_id,
                generation=current.generation if current else expected,
                reused_root=reused is not None,
                already_current=True,
            )
        finish_attempt(neo4j, attempt_id=attempt.attempt_id, state="ABANDONED")
        raise

    mark_attempt_published(
        neo4j, attempt_id=attempt.attempt_id, generation=published.generation
    )

    if verify_after_flip is None:
        finish_attempt(neo4j, attempt_id=attempt.attempt_id, state="SUCCEEDED")
        return PromotionOutcome(
            root_id=root_id,
            generation=published.generation,
            reused_root=reused is not None,
            already_current=False,
        )

    try:
        verify_after_flip(root_id)
    except BaseException as exc:
        mark_attempt_compensating(
            neo4j,
            attempt_id=attempt.attempt_id,
            reason="post-publish verification failed",
        )
        _compensate(
            neo4j,
            project_id=project_id,
            view_key=view_key,
            generation=published.generation,
            reason="post-publish verification failed",
            actor=actor,
            attempt_id=attempt.attempt_id,
        )
        finish_attempt(neo4j, attempt_id=attempt.attempt_id, state="ROLLED_BACK")
        raise PromotionError(
            ERR_PROMOTION_VERIFY_FAILED,
            "the promotion was published and then rolled back",
        ) from exc

    finish_attempt(neo4j, attempt_id=attempt.attempt_id, state="SUCCEEDED")
    return PromotionOutcome(
        root_id=root_id,
        generation=published.generation,
        reused_root=reused is not None,
        already_current=False,
    )


def _compensate(
    neo4j: Any,
    *,
    project_id: str,
    view_key: str,
    generation: int,
    reason: str,
    actor: str,
    attempt_id: str,
) -> None:
    """Flip back, and make the failure loud if flipping back does not work.

    The case most rollback designs assume away is the one the gate names, so it is handled
    explicitly: if the restore fails -- because nothing is retained, because the view moved again,
    or because the database is unreachable -- the view is marked degraded. That blocks further
    promotions and makes every read carry the status, which is the honest answer for a state no
    code path intended.

    `mark_degraded` is deliberately unconditional on the generation, so a view that moved under us
    still gets marked. If marking ALSO fails there is nothing left to try, and this raises rather
    than returning quietly: a silent failure here is the one that leaves a bad root live with
    nothing anywhere recording it.
    """
    try:
        restore_previous(
            neo4j,
            project_id=project_id,
            view_key=view_key,
            expected_generation=generation,
            actor=actor,
        )
        return
    except Exception:
        logger.error(
            "Compensation failed for a published promotion; marking the view degraded",
            exc_info=True,
        )

    try:
        mark_degraded(
            neo4j,
            project_id=project_id,
            view_key=view_key,
            reason=reason,
            actor=actor,
        )
    except Exception as exc:
        raise PromotionError(
            ERR_PROMOTION_DEGRADED,
            "the promotion could not be rolled back and the view could not be marked degraded; "
            "this view is serving a snapshot that failed verification and nothing records it",
            degraded=False,
        ) from exc

    finish_attempt(neo4j, attempt_id=attempt_id, state="DEGRADED")

    raise PromotionError(
        ERR_PROMOTION_DEGRADED,
        "the promotion could not be rolled back; the view is marked degraded and needs an "
        "operator",
        degraded=True,
    )
