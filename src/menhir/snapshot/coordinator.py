"""Coordinate an explicitly committed snapshot from staged bytes to its terminal mode result."""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any

from menhir.config.snapshot_mode import SnapshotReceiveMode
from menhir.snapshot.extraction_lease import (
    DEFAULT_LEASE_TTL_S,
    ERR_LEASE_HELD,
    ExtractionLease,
    LeaseError,
    LeaseStore,
)
from menhir.snapshot.extract_worker import run_extraction
from menhir.snapshot.promotion import promote_snapshot
from menhir.snapshot.promotion_attempt import (
    finish_attempt,
    mark_attempt_published,
    read_latest_attempt,
)
from menhir.snapshot.protocol import BundleFormatError, SnapshotManifest
from menhir.snapshot.receive import ReceiveError, StagingReceiver, UploadRecord, UploadState
from menhir.snapshot.shadow_scan import scan_snapshot
from menhir.snapshot.snapshot_structure import snapshot_structure_writer
from menhir.snapshot.canonical_view import read_view

__all__ = ["ERR_PIPELINE_INVALID_BUNDLE", "SnapshotCoordinator"]

ERR_PIPELINE_INVALID_BUNDLE = "snapshot.pipeline.invalid_bundle"
_VIEW_KEY = "canonical"


class SnapshotCoordinator:
    """Run one owned, sealed upload through the mode-authorized processing stages."""

    def __init__(
        self,
        receiver: StagingReceiver,
        *,
        state_root: Path,
        mode: SnapshotReceiveMode,
        neo4j: Any = None,
    ) -> None:
        self.receiver = receiver
        self.state_root = Path(state_root)
        self.mode = mode
        self.neo4j = neo4j

    def commit(self, *, upload_id: str, principal: str) -> UploadRecord:
        """Process a SEALED upload once; terminal retries return the durable receipt."""
        record = self.receiver.status(upload_id=upload_id, principal=principal)
        if record.state in {UploadState.READY, UploadState.FAILED}:
            return record
        if record.state is UploadState.PROMOTING:
            return self._recover_published_upload(record, principal=principal)
        if record.state is not UploadState.SEALED:
            raise ReceiveError(
                "snapshot.upload.wrong_state", f"upload is {record.state.value}"
            )

        if not self.mode.extracts_archives:
            self.receiver.ensure_project_identity(
                upload_id=upload_id, principal=principal
            )
            return self.receiver.transition(
                upload_id=upload_id,
                principal=principal,
                expected={UploadState.SEALED},
                state=UploadState.SEALED,
                result={"stage": "received", "mode": self.mode.value},
            )

        leases = LeaseStore(
            self.state_root / "snapshot-leases", ttl_s=DEFAULT_LEASE_TTL_S
        )
        lease: ExtractionLease | None = None
        extracted = self.state_root / "snapshot-extracted" / upload_id
        try:
            blob = self.receiver.blob_path(upload_id=upload_id, principal=principal)
            lease = leases.acquire(
                project_key=record.project_key, upload_id=upload_id, owner=principal
            )
            self.receiver.transition(
                upload_id=upload_id,
                principal=principal,
                expected={UploadState.SEALED},
                state=UploadState.EXTRACTING,
            )
            outcome = run_extraction(
                blob,
                extracted,
                lease=lease,
                lease_root=leases.root,
                lease_ttl_s=leases.ttl_s,
                limits=self.receiver.limits,
            )
            manifest = self._validate_materialized(record, outcome.root, outcome.digests)
            self.receiver.ensure_project_identity(upload_id=upload_id, principal=principal)

            self.receiver.transition(
                upload_id=upload_id,
                principal=principal,
                expected={UploadState.EXTRACTING},
                state=UploadState.SCANNING,
            )
            scan, shadow = scan_snapshot(outcome.root / "content")
            result: dict[str, Any] = {
                "stage": "scanned",
                "mode": self.mode.value,
                "project_id": record.project_id,
                "snapshot_id": record.snapshot_id,
                "tree_digest": manifest.tree_digest,
                "shadow": asdict(shadow),
            }

            if self.mode.writes_graph:
                if self.neo4j is None:
                    raise RuntimeError("snapshot WRITE mode has no graph connection")
                self.receiver.transition(
                    upload_id=upload_id,
                    principal=principal,
                    expected={UploadState.SCANNING},
                    state=UploadState.PROMOTING,
                )
                def renew_processing_lease(_root_id: str) -> None:
                    nonlocal lease
                    if lease is None:
                        raise LeaseError(ERR_LEASE_HELD, "snapshot processing lease is missing")
                    # Extend the same project fence immediately before the graph flip. A recovery
                    # caller can only reset PROMOTING after acquiring a newer generation; if that
                    # happened while this build ran, renewal refuses and publication is unreachable.
                    lease = leases.renew(lease)

                counts: dict[str, int] = {}
                promoted = promote_snapshot(
                    self.neo4j,
                    project_id=record.project_id,
                    view_key=_VIEW_KEY,
                    snapshot_id=record.snapshot_id,
                    actor=principal,
                    display_name=manifest.display_name,
                    verify_before_flip=renew_processing_lease,
                    write_structure=snapshot_structure_writer(
                        self.neo4j,
                        scan,
                        project_id=record.project_id,
                        report=counts,
                    ),
                )
                result.update(
                    {
                        "stage": "published",
                        "generation": promoted.generation,
                        "reused_root": promoted.reused_root,
                        "already_current": promoted.already_current,
                        "write": counts,
                    }
                )

            ready = self.receiver.transition(
                upload_id=upload_id,
                principal=principal,
                expected={
                    UploadState.PROMOTING
                    if self.mode.writes_graph
                    else UploadState.SCANNING
                },
                state=UploadState.READY,
                result=result,
            )
            self.receiver.drop_blob(upload_id=upload_id, principal=principal)
            return ready
        except BaseException as exc:
            code = str(getattr(exc, "code", "snapshot.pipeline.failed"))
            # Contention is retriable and belongs to the OTHER in-flight upload. Marking this
            # record FAILED would let one caller poison a valid sealed upload merely because a
            # peer is currently processing the same project.
            if isinstance(exc, LeaseError) and exc.code == ERR_LEASE_HELD:
                raise
            # Once durable graph intent exists, an exception cannot prove publication failed: the
            # pointer flip or attempt transition may have committed before the response/receipt
            # write failed. Preserve PROMOTING so explicit retry can reconcile graph truth.
            preserve_promoting = False
            if self.mode.writes_graph and self.neo4j is not None:
                try:
                    current = self.receiver.status(
                        upload_id=upload_id, principal=principal
                    )
                    attempt = read_latest_attempt(
                        self.neo4j,
                        project_id=current.project_id,
                        view_key=_VIEW_KEY,
                        snapshot_id=current.snapshot_id,
                    )
                    preserve_promoting = (
                        current.state is UploadState.PROMOTING
                        and attempt is not None
                        and attempt.state
                        in {"PREPARED", "PUBLISHED", "COMPENSATING", "SUCCEEDED"}
                    )
                except Exception:
                    # An unavailable graph is itself uncertainty after PROMOTING. Failing the disk
                    # receipt here would destroy the only retry handle for a possibly-live view.
                    try:
                        preserve_promoting = (
                            self.receiver.status(
                                upload_id=upload_id, principal=principal
                            ).state
                            is UploadState.PROMOTING
                        )
                    except Exception:
                        preserve_promoting = False
            if preserve_promoting:
                raise
            try:
                failed = self.receiver.transition(
                    upload_id=upload_id,
                    principal=principal,
                    expected={
                        UploadState.SEALED,
                        UploadState.EXTRACTING,
                        UploadState.SCANNING,
                        UploadState.PROMOTING,
                    },
                    state=UploadState.FAILED,
                    failure_code=code,
                )
                self.receiver.drop_blob(upload_id=failed.upload_id, principal=principal)
            except Exception:
                pass
            raise
        finally:
            shutil.rmtree(extracted, ignore_errors=True)
            if lease is not None:
                try:
                    leases.release(lease)
                except Exception:
                    pass

    def _recover_published_upload(
        self, record: UploadRecord, *, principal: str
    ) -> UploadRecord:
        """Finish the filesystem receipt after a graph publication survived a process crash."""
        if self.neo4j is None:
            raise ReceiveError(
                "snapshot.upload.wrong_state", "upload is still being promoted"
            )
        attempt = read_latest_attempt(
            self.neo4j,
            project_id=record.project_id,
            view_key=_VIEW_KEY,
            snapshot_id=record.snapshot_id,
        )
        if attempt is None:
            # No durable attempt means the pointer flip was unreachable: begin_attempt precedes
            # publish_root. First acquire the project fence: a live publisher still owns it and
            # must not have its state reset underneath it. If a stale holder is superseded, its
            # pre-flip renewal will fail, so it cannot publish after this replay starts.
            recovery_leases = LeaseStore(
                self.state_root / "snapshot-leases", ttl_s=DEFAULT_LEASE_TTL_S
            )
            recovery_lease = recovery_leases.acquire(
                project_key=record.project_key,
                upload_id=record.upload_id,
                owner=principal,
            )
            try:
                # Re-read after taking the fence in case the publisher created intent just before
                # losing/surrendering its lease.
                latest = read_latest_attempt(
                    self.neo4j,
                    project_id=record.project_id,
                    view_key=_VIEW_KEY,
                    snapshot_id=record.snapshot_id,
                )
                if latest is None:
                    shutil.rmtree(
                        self.state_root / "snapshot-extracted" / record.upload_id,
                        ignore_errors=True,
                    )
                    self.receiver.transition(
                        upload_id=record.upload_id,
                        principal=principal,
                        expected={UploadState.PROMOTING},
                        state=UploadState.SEALED,
                    )
            finally:
                try:
                    recovery_leases.release(recovery_lease)
                except LeaseError:
                    pass
            if latest is not None:
                return self._recover_published_upload(record, principal=principal)
            return self.commit(upload_id=record.upload_id, principal=principal)

        if attempt.state in {"PREPARED", "PUBLISHED"}:
            view = read_view(
                self.neo4j, project_id=record.project_id, view_key=_VIEW_KEY
            )
            if (
                view is None
                or view.current_root != attempt.root_id
                or view.generation
                != (attempt.published_generation or attempt.expected_generation + 1)
            ):
                raise ReceiveError(
                    "snapshot.upload.wrong_state", "upload is still being promoted"
                )
            if attempt.state == "PREPARED":
                attempt = mark_attempt_published(
                    self.neo4j,
                    attempt_id=attempt.attempt_id,
                    generation=view.generation,
                )
            attempt = finish_attempt(
                self.neo4j, attempt_id=attempt.attempt_id, state="SUCCEEDED"
            )

        if attempt.state == "SUCCEEDED":
            ready = self.receiver.transition(
                upload_id=record.upload_id,
                principal=principal,
                expected={UploadState.PROMOTING},
                state=UploadState.READY,
                result={
                    "stage": "published",
                    "mode": self.mode.value,
                    "project_id": record.project_id,
                    "snapshot_id": record.snapshot_id,
                    "generation": attempt.published_generation,
                    "recovered": True,
                },
            )
            self.receiver.drop_blob(upload_id=record.upload_id, principal=principal)
            return ready

        if attempt.state in {"ROLLED_BACK", "ABANDONED", "DEGRADED"}:
            failed = self.receiver.transition(
                upload_id=record.upload_id,
                principal=principal,
                expected={UploadState.PROMOTING},
                state=UploadState.FAILED,
                failure_code=f"snapshot.promotion.{attempt.state.lower()}",
            )
            self.receiver.drop_blob(upload_id=record.upload_id, principal=principal)
            return failed

        raise ReceiveError("snapshot.upload.wrong_state", "upload is still being promoted")

    def _validate_materialized(
        self, record: UploadRecord, root: Path, digests: dict[str, str]
    ) -> SnapshotManifest:
        manifest_path = root / "snapshot.json"
        try:
            if manifest_path.stat().st_size > self.receiver.limits.max_file_bytes:
                raise ValueError("manifest is too large")
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("manifest is not an object")
            manifest = SnapshotManifest.from_mapping(raw, self.receiver.limits)
        except (OSError, ValueError, TypeError, BundleFormatError) as exc:
            raise ReceiveError(
                ERR_PIPELINE_INVALID_BUNDLE, "snapshot manifest is invalid"
            ) from exc

        expected = {"snapshot.json"} | {f"content/{item.path}" for item in manifest.files}
        actual = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
        }
        hashes_match = all(
            digests.get(f"content/{item.path}") == item.sha256 for item in manifest.files
        )
        identity_matches = (
            manifest.display_name == record.project_key
            and (manifest.project_id is None or manifest.project_id == record.project_id)
        )
        if actual != expected or set(digests) != expected or not hashes_match or not identity_matches:
            raise ReceiveError(
                ERR_PIPELINE_INVALID_BUNDLE,
                "snapshot contents do not match the committed manifest",
            )
        return manifest
