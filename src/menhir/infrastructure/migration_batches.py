"""Migration batch manifests for the one-time Metric migration (Part F of the Metric plan).

The dry run classifies exactly which :Entity instrumentation nodes become :Metric and freezes
that set as a canonical manifest with a SHA-256 over the target set. Approval and apply both
require the exact SHA, and apply reads the approved UUID list -- it never re-runs the
classifier to choose targets. Any target drift between snapshot and apply aborts the batch
before the first mutation.

State machine: DRY_RUN -> APPROVED -> APPLIED (or REVERSED). Approval and apply are guarded by
an exact manifest-SHA match so a stale or tampered manifest cannot be applied. See the
workspace-root artifact .agent/plans/menhir-metric-provenance-redesign.md (Part F).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import uuid as uuidlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from menhir.infrastructure.telemetry import default_telemetry_db_path

logger = logging.getLogger(__name__)

BATCH_STATES = frozenset({"DRY_RUN", "APPROVED", "APPLIED", "REVERSED"})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MigrationBatchError(RuntimeError):
    """Raised on a manifest-SHA mismatch or an illegal batch state transition."""


@dataclass
class MigrationBatchStore:
    """The ``migration_batches`` table: canonical, SHA-anchored migration manifests."""

    db_path: Path = field(default_factory=default_telemetry_db_path)
    _initialized: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def _ensure_ready(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS migration_batches (
                        batch_id            TEXT PRIMARY KEY,
                        classifier_version  TEXT NOT NULL,
                        manifest_sha256     TEXT NOT NULL,
                        manifest_json       TEXT NOT NULL,
                        target_count        INTEGER NOT NULL,
                        state               TEXT NOT NULL,
                        menhir_commit       TEXT,
                        created_at          TEXT NOT NULL,
                        approved_at         TEXT,
                        applied_at          TEXT
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_migration_batches_state "
                    "ON migration_batches (state)"
                )
                conn.commit()
            self._initialized = True

    def create_dry_run(
        self,
        *,
        classifier_version: str,
        manifest_sha256: str,
        manifest_json: str,
        target_count: int,
        menhir_commit: str | None = None,
        batch_id: str | None = None,
    ) -> str:
        """Store a DRY_RUN manifest and return its batch_id."""
        self._ensure_ready()
        batch_id = batch_id or uuidlib.uuid4().hex
        now = _utc_now_iso()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO migration_batches (
                    batch_id, classifier_version, manifest_sha256, manifest_json,
                    target_count, state, menhir_commit, created_at, approved_at, applied_at
                ) VALUES (?, ?, ?, ?, ?, 'DRY_RUN', ?, ?, NULL, NULL)
                """,
                (
                    batch_id, classifier_version, manifest_sha256, manifest_json,
                    target_count, menhir_commit, now,
                ),
            )
            conn.commit()
        return batch_id

    def approve(self, batch_id: str, *, manifest_sha256: str) -> None:
        """DRY_RUN -> APPROVED, only if the supplied SHA matches the stored manifest."""
        self._require_sha(batch_id, manifest_sha256)
        self._transition(batch_id, to_state="APPROVED", allowed_from={"DRY_RUN"},
                         stamp="approved_at")

    def mark_applied(self, batch_id: str, *, manifest_sha256: str) -> None:
        """APPROVED -> APPLIED, only if the supplied SHA matches the stored manifest."""
        self._require_sha(batch_id, manifest_sha256)
        self._transition(batch_id, to_state="APPLIED", allowed_from={"APPROVED"},
                         stamp="applied_at")

    def mark_reversed(self, batch_id: str, *, manifest_sha256: str) -> None:
        """APPLIED -> REVERSED, only if the supplied SHA matches the stored manifest."""
        self._require_sha(batch_id, manifest_sha256)
        self._transition(batch_id, to_state="REVERSED", allowed_from={"APPLIED"})

    def _require_sha(self, batch_id: str, manifest_sha256: str) -> None:
        batch = self.get(batch_id)
        if batch is None:
            raise MigrationBatchError(f"no migration batch {batch_id!r}")
        if batch["manifest_sha256"] != manifest_sha256:
            raise MigrationBatchError(
                f"manifest SHA mismatch for batch {batch_id!r}: supplied {manifest_sha256!r} "
                f"!= stored {batch['manifest_sha256']!r}. Re-run dry-run and re-approve."
            )

    def _transition(
        self, batch_id: str, *, to_state: str, allowed_from: set[str], stamp: str | None = None
    ) -> None:
        self._ensure_ready()
        now = _utc_now_iso()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT state FROM migration_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if row is None:
                raise MigrationBatchError(f"no migration batch {batch_id!r}")
            if row["state"] == to_state:
                return
            if row["state"] not in allowed_from:
                raise MigrationBatchError(
                    f"illegal batch transition {row['state']} -> {to_state} for {batch_id!r}"
                )
            if stamp is not None:
                conn.execute(
                    f"UPDATE migration_batches SET state = ?, {stamp} = ? WHERE batch_id = ?",
                    (to_state, now, batch_id),
                )
            else:
                conn.execute(
                    "UPDATE migration_batches SET state = ? WHERE batch_id = ?",
                    (to_state, batch_id),
                )
            conn.commit()

    def get(self, batch_id: str) -> dict[str, Any] | None:
        self._ensure_ready()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM migration_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_batches(self, *, state: str | None = None) -> list[dict[str, Any]]:
        self._ensure_ready()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if state is not None:
                if state not in BATCH_STATES:
                    raise MigrationBatchError(f"unknown state {state!r}")
                rows = conn.execute(
                    "SELECT * FROM migration_batches WHERE state = ? ORDER BY created_at DESC",
                    (state,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM migration_batches ORDER BY created_at DESC"
                ).fetchall()
            return [dict(r) for r in rows]
