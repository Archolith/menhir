from __future__ import annotations

from unittest.mock import MagicMock, patch

from menhir.snapshot.canonical_view import (
    ERR_VIEW_SUPERSEDED,
    ViewError,
    ViewPointer,
)
from menhir.snapshot.promotion_attempt import AttemptRecord, reconcile_promotion_attempts
from menhir.snapshot.promotion import promote_snapshot


def test_reconcile_race_does_not_degrade_a_newer_valid_view() -> None:
    neo4j = MagicMock()
    neo4j.execute.return_value = [{"attempt_id": "pa-1"}]
    attempt = AttemptRecord(
        attempt_id="pa-1",
        project_id="project-1",
        view_key="canonical",
        snapshot_id="snapshot-1",
        root_id="root-stale",
        actor="operator",
        state="PUBLISHED",
        expected_generation=1,
        published_generation=2,
    )
    stale_view = ViewPointer(
        project_id="project-1",
        view_key="canonical",
        generation=2,
        current_root="root-stale",
        previous_root="root-old",
        degraded=False,
    )
    superseded = ViewError(ERR_VIEW_SUPERSEDED, "view moved")

    with patch(
        "menhir.snapshot.promotion_attempt.read_attempt", return_value=attempt
    ), patch(
        "menhir.snapshot.canonical_view.read_view", return_value=stale_view
    ), patch(
        "menhir.snapshot.canonical_view.restore_previous", side_effect=superseded
    ), patch(
        "menhir.snapshot.canonical_view.mark_degraded", side_effect=superseded
    ) as degrade, patch(
        "menhir.snapshot.promotion_attempt.finish_attempt"
    ) as finish:
        report = reconcile_promotion_attempts(neo4j, stale_after_ms=0)

    degrade.assert_called_once_with(
        neo4j,
        project_id="project-1",
        view_key="canonical",
        reason="interrupted snapshot promotion could not be restored",
        actor="system:snapshot-promotion-reconciler",
        expected_generation=2,
        expected_root="root-stale",
    )
    finish.assert_called_once_with(neo4j, attempt_id="pa-1", state="ABANDONED")
    assert report == {
        "examined": 1,
        "rolled_back": 0,
        "abandoned": 1,
        "degraded": 0,
    }


def test_processing_fence_is_verified_after_durable_intent_and_before_publish() -> None:
    events: list[str] = []
    attempt = AttemptRecord(
        attempt_id="pa-1",
        project_id="project-1",
        view_key="canonical",
        snapshot_id="snapshot-1",
        root_id="root-1",
        actor="operator",
        state="PREPARED",
        expected_generation=0,
        published_generation=None,
    )
    published = ViewPointer(
        project_id="project-1",
        view_key="canonical",
        generation=1,
        current_root="root-1",
        previous_root=None,
        degraded=False,
    )

    def begin(*_args, **_kwargs):
        events.append("attempt")
        return attempt

    def verify(_root: str) -> None:
        events.append("verify")

    def publish(*_args, **_kwargs):
        events.append("publish")
        return published

    with patch(
        "menhir.snapshot.promotion.find_publishable_root",
        return_value=MagicMock(root_id="root-1"),
    ), patch(
        "menhir.snapshot.promotion.read_view", return_value=None
    ), patch(
        "menhir.snapshot.promotion.begin_attempt", side_effect=begin
    ), patch(
        "menhir.snapshot.promotion.publish_root", side_effect=publish
    ), patch(
        "menhir.snapshot.promotion.mark_attempt_published"
    ), patch(
        "menhir.snapshot.promotion.finish_attempt"
    ):
        promote_snapshot(
            MagicMock(),
            project_id="project-1",
            view_key="canonical",
            snapshot_id="snapshot-1",
            actor="operator",
            write_structure=MagicMock(),
            verify_before_flip=verify,
        )

    assert events == ["attempt", "verify", "publish"]
