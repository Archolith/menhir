from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from menhir.config.snapshot_mode import SnapshotReceiveMode
from menhir.snapshot.coordinator import SnapshotCoordinator
from menhir.snapshot.extraction_lease import ERR_LEASE_HELD, LeaseError, LeaseStore
from menhir.snapshot.extract_worker import WorkerOutcome
from menhir.snapshot.promotion import PromotionOutcome
from menhir.snapshot.protocol import FileRecord, SnapshotManifest, sha256_hex
from menhir.snapshot.receive import StagingReceiver, UploadState
from menhir.snapshot.shadow_scan import ShadowReport


def _sealed(receiver: StagingReceiver, body: bytes = b"archive"):
    record = receiver.begin(
        principal="operator-1",
        project_key="project-display",
        declared_bytes=len(body),
        chunk_bytes=len(body),
    )
    return receiver.put_chunk(
        upload_id=record.upload_id,
        principal=record.principal,
        index=0,
        data_b64=base64.b64encode(body).decode("ascii"),
        declared_len=len(body),
        digest=sha256_hex(body),
    )


def _materialized(root: Path, record) -> WorkerOutcome:
    content = b"print('snapshot')\n"
    file_record = FileRecord(path="src/app.py", size=len(content), sha256=sha256_hex(content))
    manifest = SnapshotManifest.build(
        display_name=record.project_key,
        files=[file_record],
    )
    (root / "content" / "src").mkdir(parents=True)
    (root / "content" / "src" / "app.py").write_bytes(content)
    manifest_bytes = manifest.to_canonical_bytes()
    (root / "snapshot.json").write_bytes(manifest_bytes)
    return WorkerOutcome(
        root=root,
        file_count=2,
        total_bytes=len(content) + len(manifest_bytes),
        digests={
            "snapshot.json": sha256_hex(manifest_bytes),
            "content/src/app.py": sha256_hex(content),
        },
    )


def _shadow_report() -> ShadowReport:
    return ShadowReport(
        fingerprint="sha256:" + "1" * 64,
        file_count=1,
        directory_count=1,
        symbol_count=0,
        import_count=0,
        endpoint_count=0,
        files_discovered=1,
        files_eligible=1,
        files_indexed=1,
    )


def test_receive_mode_commit_does_not_extract(tmp_path: Path) -> None:
    receiver = StagingReceiver(tmp_path / "staging")
    record = _sealed(receiver)

    with patch("menhir.snapshot.coordinator.run_extraction") as extract:
        result = SnapshotCoordinator(
            receiver, state_root=tmp_path, mode=SnapshotReceiveMode.RECEIVE
        ).commit(upload_id=record.upload_id, principal=record.principal)

    extract.assert_not_called()
    assert result.state is UploadState.SEALED
    assert result.result == {"stage": "received", "mode": "receive"}


def test_project_lease_contention_keeps_the_upload_sealed_for_retry(tmp_path: Path) -> None:
    receiver = StagingReceiver(tmp_path / "staging")
    record = _sealed(receiver)

    with patch(
        "menhir.snapshot.coordinator.LeaseStore.acquire",
        side_effect=LeaseError(ERR_LEASE_HELD, "busy"),
    ), pytest.raises(LeaseError) as excinfo:
        SnapshotCoordinator(
            receiver, state_root=tmp_path, mode=SnapshotReceiveMode.SHADOW
        ).commit(upload_id=record.upload_id, principal=record.principal)

    assert excinfo.value.code == ERR_LEASE_HELD

    current = receiver.status(upload_id=record.upload_id, principal=record.principal)
    assert current.state is UploadState.SEALED


def test_shadow_commit_extracts_scans_and_deletes_materials(tmp_path: Path) -> None:
    receiver = StagingReceiver(tmp_path / "staging")
    record = _sealed(receiver)
    extracted = tmp_path / "snapshot-extracted" / record.upload_id
    extracted.parent.mkdir(parents=True)
    outcome = _materialized(extracted, record)

    with patch(
        "menhir.snapshot.coordinator.run_extraction", return_value=outcome
    ), patch(
        "menhir.snapshot.coordinator.scan_snapshot",
        return_value=(MagicMock(), _shadow_report()),
    ):
        result = SnapshotCoordinator(
            receiver, state_root=tmp_path, mode=SnapshotReceiveMode.SHADOW
        ).commit(upload_id=record.upload_id, principal=record.principal)

    assert result.state is UploadState.READY
    assert result.result["stage"] == "scanned"
    assert result.result["shadow"]["file_count"] == 1
    assert not extracted.exists()
    assert not (tmp_path / "staging" / record.upload_id / "blob").exists()


def test_write_commit_supplies_authenticated_actor_and_reports_publication(tmp_path: Path) -> None:
    receiver = StagingReceiver(tmp_path / "staging")
    record = _sealed(receiver)
    extracted = tmp_path / "snapshot-extracted" / record.upload_id
    extracted.parent.mkdir(parents=True)
    outcome = _materialized(extracted, record)
    promoted = PromotionOutcome(
        root_id="root-1", generation=3, reused_root=False, already_current=False
    )
    neo4j = MagicMock()

    with patch(
        "menhir.snapshot.coordinator.run_extraction", return_value=outcome
    ), patch(
        "menhir.snapshot.coordinator.scan_snapshot",
        return_value=(MagicMock(), _shadow_report()),
    ), patch(
        "menhir.snapshot.coordinator.promote_snapshot",
        side_effect=lambda *_args, **kwargs: (
            kwargs["verify_before_flip"]("root-1") or promoted
        ),
    ) as promote:
        result = SnapshotCoordinator(
            receiver,
            state_root=tmp_path,
            mode=SnapshotReceiveMode.WRITE,
            neo4j=neo4j,
        ).commit(upload_id=record.upload_id, principal=record.principal)

    assert result.state is UploadState.READY
    assert result.result["stage"] == "published"
    assert result.result["generation"] == 3
    kwargs = promote.call_args.kwargs
    assert kwargs["actor"] == "operator-1"
    assert kwargs["project_id"] == record.project_id
    assert kwargs["snapshot_id"] == record.snapshot_id
    assert kwargs["display_name"] == record.project_key


def test_retry_recovers_ready_receipt_after_publication_crash(tmp_path: Path) -> None:
    receiver = StagingReceiver(tmp_path / "staging")
    record = _sealed(receiver)
    for expected, state in [
        ({UploadState.SEALED}, UploadState.EXTRACTING),
        ({UploadState.EXTRACTING}, UploadState.SCANNING),
        ({UploadState.SCANNING}, UploadState.PROMOTING),
    ]:
        record = receiver.transition(
            upload_id=record.upload_id,
            principal=record.principal,
            expected=expected,
            state=state,
        )
    attempt = SimpleNamespace(
        attempt_id="pa-1", state="SUCCEEDED", published_generation=4
    )
    receiver.sweep(now=record.updated_at + receiver.quotas.inactivity_ttl_s + 1)
    assert receiver.status(
        upload_id=record.upload_id, principal=record.principal
    ).state is UploadState.PROMOTING

    with patch(
        "menhir.snapshot.coordinator.read_latest_attempt", return_value=attempt
    ):
        recovered = SnapshotCoordinator(
            receiver,
            state_root=tmp_path,
            mode=SnapshotReceiveMode.WRITE,
            neo4j=MagicMock(),
        ).commit(upload_id=record.upload_id, principal=record.principal)

    assert recovered.state is UploadState.READY
    assert recovered.result == {
        "stage": "published",
        "mode": "write",
        "project_id": record.project_id,
        "snapshot_id": record.snapshot_id,
        "generation": 4,
        "recovered": True,
    }
    assert not (tmp_path / "staging" / record.upload_id / "blob").exists()


def test_retry_without_durable_attempt_replays_from_the_retained_blob(tmp_path: Path) -> None:
    receiver = StagingReceiver(tmp_path / "staging")
    record = _sealed(receiver)
    for expected, state in [
        ({UploadState.SEALED}, UploadState.EXTRACTING),
        ({UploadState.EXTRACTING}, UploadState.SCANNING),
        ({UploadState.SCANNING}, UploadState.PROMOTING),
    ]:
        record = receiver.transition(
            upload_id=record.upload_id,
            principal=record.principal,
            expected=expected,
            state=state,
        )
    extracted = tmp_path / "snapshot-extracted" / record.upload_id
    extracted.mkdir(parents=True)
    (extracted / "crash-remnant").write_text("partial", encoding="utf-8")
    promoted = PromotionOutcome(
        root_id="root-replayed", generation=2, reused_root=True, already_current=False
    )

    def extract(_blob, destination, **_kwargs):
        assert not (destination / "crash-remnant").exists()
        return _materialized(destination, record)

    with patch(
        "menhir.snapshot.coordinator.read_latest_attempt", return_value=None
    ), patch(
        "menhir.snapshot.coordinator.run_extraction", side_effect=extract
    ) as run, patch(
        "menhir.snapshot.coordinator.scan_snapshot",
        return_value=(MagicMock(), _shadow_report()),
    ), patch(
        "menhir.snapshot.coordinator.promote_snapshot", return_value=promoted
    ):
        replayed = SnapshotCoordinator(
            receiver,
            state_root=tmp_path,
            mode=SnapshotReceiveMode.WRITE,
            neo4j=MagicMock(),
        ).commit(upload_id=record.upload_id, principal=record.principal)

    assert run.call_count == 1
    assert replayed.state is UploadState.READY
    assert replayed.result["stage"] == "published"


def test_no_attempt_replay_refuses_while_the_original_publisher_holds_the_lease(
    tmp_path: Path,
) -> None:
    receiver = StagingReceiver(tmp_path / "staging")
    record = _sealed(receiver)
    for expected, state in [
        ({UploadState.SEALED}, UploadState.EXTRACTING),
        ({UploadState.EXTRACTING}, UploadState.SCANNING),
        ({UploadState.SCANNING}, UploadState.PROMOTING),
    ]:
        record = receiver.transition(
            upload_id=record.upload_id,
            principal=record.principal,
            expected=expected,
            state=state,
        )
    leases = LeaseStore(tmp_path / "snapshot-leases")
    live = leases.acquire(
        project_key=record.project_key,
        upload_id=record.upload_id,
        owner=record.principal,
    )

    with patch(
        "menhir.snapshot.coordinator.read_latest_attempt", return_value=None
    ), pytest.raises(LeaseError) as excinfo:
        SnapshotCoordinator(
            receiver,
            state_root=tmp_path,
            mode=SnapshotReceiveMode.WRITE,
            neo4j=MagicMock(),
        ).commit(upload_id=record.upload_id, principal=record.principal)

    assert excinfo.value.code == ERR_LEASE_HELD
    assert receiver.status(
        upload_id=record.upload_id, principal=record.principal
    ).state is UploadState.PROMOTING
    leases.release(live)


def test_ready_receipt_io_failure_does_not_overwrite_published_truth(
    tmp_path: Path,
) -> None:
    receiver = StagingReceiver(tmp_path / "staging")
    record = _sealed(receiver)
    extracted = tmp_path / "snapshot-extracted" / record.upload_id
    extracted.parent.mkdir(parents=True)
    outcome = _materialized(extracted, record)
    promoted = PromotionOutcome(
        root_id="root-1", generation=3, reused_root=False, already_current=False
    )
    attempt = SimpleNamespace(
        attempt_id="pa-1", state="SUCCEEDED", published_generation=3
    )
    original_transition = receiver.transition

    def transition(**kwargs):
        if kwargs["state"] is UploadState.READY:
            raise OSError("receipt disk unavailable")
        return original_transition(**kwargs)

    with patch(
        "menhir.snapshot.coordinator.run_extraction", return_value=outcome
    ), patch(
        "menhir.snapshot.coordinator.scan_snapshot",
        return_value=(MagicMock(), _shadow_report()),
    ), patch(
        "menhir.snapshot.coordinator.promote_snapshot", return_value=promoted
    ), patch(
        "menhir.snapshot.coordinator.read_latest_attempt", return_value=attempt
    ), patch.object(receiver, "transition", side_effect=transition), pytest.raises(
        OSError, match="receipt disk unavailable"
    ):
        SnapshotCoordinator(
            receiver,
            state_root=tmp_path,
            mode=SnapshotReceiveMode.WRITE,
            neo4j=MagicMock(),
        ).commit(upload_id=record.upload_id, principal=record.principal)

    assert receiver.status(
        upload_id=record.upload_id, principal=record.principal
    ).state is UploadState.PROMOTING
