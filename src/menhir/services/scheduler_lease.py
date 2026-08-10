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

from menhir.infrastructure.telemetry import default_telemetry_db_path

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
            with sqlite3.connect(self.db_path) as conn:
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
        return socket.gethostname()

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """True if a process with this PID is currently running on the local host.

        Used to reclaim a lease whose owner was hard-killed before its TTL expired. On Windows a
        restart terminates the old server with TerminateProcess (not a graceful signal), so its
        shutdown hook never runs and the lease is never released; on a fast restart the TTL has not
        expired either, so the successor would otherwise be blocked until expiry (or a manual
        takeover). Conservative by design: any uncertainty (permission error, unexpected platform
        result) is treated as ALIVE, so a genuinely live owner is never displaced.
        """
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        if pid <= 0:
            return False
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            ERROR_INVALID_PARAMETER = 87  # PID does not exist
            # Declare signatures: a HANDLE is pointer-sized, so the default 32-bit c_int restype
            # would TRUNCATE it on Win64 and corrupt the handle. Set restype/argtypes explicitly.
            # GetExitCodeProcess (not WaitForSingleObject) is used because the query-limited access
            # right does not grant SYNCHRONIZE, which Wait* requires.
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                # No handle: invalid-parameter => the PID does not exist (dead). Any other error
                # (e.g. access-denied) => it exists but we cannot inspect it, so treat as alive.
                return ctypes.get_last_error() != ERROR_INVALID_PARAMETER
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True  # cannot read status — do not displace
                return exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists but owned by another user
        except OSError:
            return True  # unknown — do not displace
        return True

    def try_acquire(self, *, lease_name: str, owner_id: str, owner_pid: int, lease_duration_s: float) -> bool:
        self._ensure_ready()
        now = self._now_epoch()
        expires_at = now + max(1.0, lease_duration_s)
        heartbeat_at = _utc_now_iso()
        hostname = self._hostname()
        with sqlite3.connect(self.db_path) as conn:
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
            dead_local_owner = (
                existing_owner_id != owner_id
                and existing_expires_at > now
                and existing_host == hostname
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
        self._ensure_ready()
        now = self._now_epoch()
        heartbeat_at = _utc_now_iso()
        expires_at = now + max(1.0, lease_duration_s)
        with sqlite3.connect(self.db_path) as conn:
            updated = conn.execute(
                """
                UPDATE scheduler_leases
                SET owner_pid = ?,
                    heartbeat_at = ?,
                    lease_expires_at = ?
                WHERE lease_name = ?
                  AND owner_id = ?
                """,
                (owner_pid, heartbeat_at, expires_at, lease_name, owner_id),
            ).rowcount
            conn.commit()
            return updated > 0

    def release(self, *, lease_name: str, owner_id: str) -> None:
        self._ensure_ready()
        with sqlite3.connect(self.db_path) as conn:
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
        with sqlite3.connect(self.db_path) as conn:
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
        with sqlite3.connect(self.db_path) as conn:
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
