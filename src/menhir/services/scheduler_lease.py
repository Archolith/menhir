"""Cross-process SQLite lease for ensuring only one scheduler loop runs."""

from __future__ import annotations

import logging
import os
import socket
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from menhir.infrastructure import operation_owner
from menhir.infrastructure import process_liveness
from menhir.infrastructure.telemetry import connect_telemetry_db, default_telemetry_db_path
from menhir.clock import utc_now_iso as _utc_now_iso

logger = logging.getLogger(__name__)




@dataclass
class SchedulerLeaseStore:
    """Cross-process SQLite lease for ensuring only one scheduler loop runs."""

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
            with connect_telemetry_db(self.db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS scheduler_leases (
                        lease_name TEXT PRIMARY KEY,
                        owner_id TEXT NOT NULL,
                        owner_pid INTEGER NOT NULL,
                        hostname TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        heartbeat_at TEXT NOT NULL,
                        lease_expires_at REAL NOT NULL
                    )
                    """
                )
                conn.commit()
            self._initialized = True

    @staticmethod
    def _now_epoch() -> float:
        return time.time()

    @staticmethod
    def _hostname() -> str:
        return process_liveness.hostname()

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Delegates to the shared predicate so lease and saga ownership cannot drift apart."""
        return process_liveness.pid_alive(pid)

    def try_acquire(self, *, lease_name: str, owner_id: str, owner_pid: int, lease_duration_s: float) -> bool:
        self._ensure_ready()
        now = self._now_epoch()
        expires_at = now + max(1.0, lease_duration_s)
        heartbeat_at = _utc_now_iso()
        hostname = self._hostname()
        with connect_telemetry_db(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT owner_id, lease_expires_at, owner_pid, hostname
                FROM scheduler_leases
                WHERE lease_name = ?
                """,
                (lease_name,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO scheduler_leases (
                        lease_name, owner_id, owner_pid, hostname, started_at, heartbeat_at, lease_expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (lease_name, owner_id, owner_pid, hostname, heartbeat_at, heartbeat_at, expires_at),
                )
                conn.commit()
                return True
            existing_owner_id = str(row[0])
            existing_expires_at = float(row[1])
            existing_pid = row[2]
            existing_host = str(row[3]) if row[3] is not None else ""
            # Reclaim a still-in-TTL lease whose owner was hard-killed: only when the prior owner
            # lived on THIS host (so we can actually check its PID) and that PID is no longer alive.
            # This makes restart self-healing without waiting for expiry or a manual takeover, while
            # never displacing a live owner (local live PID, or any owner on another host).
            #
            # Gated on the SAME deployment assertion that governs saga ownership, and for the same
            # reason: hostname equality does not establish that the recorded PID belongs to the
            # namespace this process can inspect. Where it does not -- containers on a shared
            # kernel, cloned images, a lease database mounted by several nodes -- "that PID is not
            # running here" is a statement about an unrelated process, and acting on it displaces a
            # LIVE lease holder before its TTL.
            #
            # Leaving the two layers on different policies was the hole: per-operation ownership
            # would correctly refuse to infer death while this fast path silently handed the global
            # reconciliation lease to a second reconciler. Unasserted, this simply waits for the
            # TTL, which costs restart latency and never costs correctness.
            dead_local_owner = (
                existing_owner_id != owner_id
                and existing_expires_at > now
                and existing_host == hostname
                and operation_owner.host_pid_namespace_is_verifiable()
                and not self._pid_alive(existing_pid)
            )
            if dead_local_owner:
                logger.warning(
                    "Reclaiming scheduler lease from dead local owner: lease=%s dead_pid=%s new_owner_id=%s",
                    lease_name, existing_pid, owner_id,
                )
            if existing_owner_id == owner_id or existing_expires_at <= now or dead_local_owner:
                conn.execute(
                    """
                    UPDATE scheduler_leases
                    SET owner_id = ?,
                        owner_pid = ?,
                        hostname = ?,
                        started_at = ?,
                        heartbeat_at = ?,
                        lease_expires_at = ?
                    WHERE lease_name = ?
                    """,
                    (owner_id, owner_pid, hostname, heartbeat_at, heartbeat_at, expires_at, lease_name),
                )
                conn.commit()
                return True
            conn.rollback()
        return False

    def renew(self, *, lease_name: str, owner_id: str, owner_pid: int, lease_duration_s: float) -> bool:
        """Extend a lease this owner still holds. False if it lapsed or moved.

        The expiry predicate is load-bearing: without it an owner whose lease had ALREADY lapsed
        could renew it back to life merely because nobody else had taken it yet. Any process that
        checked the gate during that window -- including the journal's PREPARE check, which reads
        expiry directly -- would legitimately have treated it as free, so resurrecting it
        retroactively invalidates their decision.
        """
        self._ensure_ready()
        now = self._now_epoch()
        heartbeat_at = _utc_now_iso()
        expires_at = now + max(1.0, lease_duration_s)
        with connect_telemetry_db(self.db_path) as conn:
            updated = conn.execute(
                """
                UPDATE scheduler_leases
                SET owner_pid = ?,
                    heartbeat_at = ?,
                    lease_expires_at = ?
                WHERE lease_name = ?
                  AND owner_id = ?
                  AND lease_expires_at > ?
                """,
                (owner_pid, heartbeat_at, expires_at, lease_name, owner_id, now),
            ).rowcount
            conn.commit()
            return updated > 0

    def release(self, *, lease_name: str, owner_id: str) -> None:
        self._ensure_ready()
        with connect_telemetry_db(self.db_path) as conn:
            conn.execute(
                """
                DELETE FROM scheduler_leases
                WHERE lease_name = ?
                  AND owner_id = ?
                """,
                (lease_name, owner_id),
            )
            conn.commit()

    def fetch(self, *, lease_name: str) -> dict[str, object] | None:
        self._ensure_ready()
        with connect_telemetry_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT lease_name, owner_id, owner_pid, hostname, started_at, heartbeat_at, lease_expires_at
                FROM scheduler_leases
                WHERE lease_name = ?
                """,
                (lease_name,),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["expired"] = float(result["lease_expires_at"]) <= self._now_epoch()
            return result

    def force_acquire(
        self,
        *,
        lease_name: str,
        owner_id: str,
        owner_pid: int,
        lease_duration_s: float,
    ) -> dict[str, object] | None:
        """Acquire the scheduler lease regardless of prior owner/expiry."""

        self._ensure_ready()
        now = self._now_epoch()
        expires_at = now + max(1.0, lease_duration_s)
        heartbeat_at = _utc_now_iso()
        hostname = self._hostname()
        with connect_telemetry_db(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            previous_row = conn.execute(
                """
                SELECT lease_name, owner_id, owner_pid, hostname, started_at, heartbeat_at, lease_expires_at
                FROM scheduler_leases
                WHERE lease_name = ?
                """,
                (lease_name,),
            ).fetchone()
            previous = dict(previous_row) if previous_row is not None else None
            if previous is not None:
                previous["expired"] = float(previous["lease_expires_at"]) <= now

            conn.execute(
                """
                INSERT INTO scheduler_leases (
                    lease_name, owner_id, owner_pid, hostname, started_at, heartbeat_at, lease_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(lease_name) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    owner_pid = excluded.owner_pid,
                    hostname = excluded.hostname,
                    started_at = excluded.started_at,
                    heartbeat_at = excluded.heartbeat_at,
                    lease_expires_at = excluded.lease_expires_at
                """,
                (lease_name, owner_id, owner_pid, hostname, heartbeat_at, heartbeat_at, expires_at),
            )
            conn.commit()
            return previous
