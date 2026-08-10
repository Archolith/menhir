"""Hashed token registry for per-client identity tokens.

Tokens are stored only as sha256 hashes; the raw token value is returned once
at mint time and never persisted.  Follows the same SQLite pattern as
:mod:`menhir.services.scheduler_lease`.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


def new_client_id() -> str:
    """Generate a stable, unique 16-hex-char client identifier."""
    return secrets.token_hex(8)


def hash_token(raw: str) -> str:
    """Return the sha256 hex digest of *raw*."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ClientTokenRecord:
    """Immutable record representing a registered client token."""

    client_id: str
    client_name: str
    tier: str
    created_at: float
    revoked: bool = False


class ClientTokenStore:
    """SQLite-backed store for client tokens (hashed)."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS client_tokens (
                    token_hash TEXT PRIMARY KEY,
                    client_id TEXT,
                    client_name TEXT,
                    tier TEXT,
                    created_at REAL,
                    revoked INTEGER DEFAULT 0
                )
                """
            )
            conn.commit()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ClientTokenRecord:
        return ClientTokenRecord(
            client_id=row["client_id"],
            client_name=row["client_name"],
            tier=row["tier"],
            created_at=row["created_at"],
            revoked=bool(row["revoked"]),
        )

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def mint(self, client_name: str, tier: str) -> tuple[str, ClientTokenRecord]:
        """Mint a new token.

        Returns the (raw_token, record) pair.  The raw token is shown **once**
        and is never persisted — only its sha256 hash is stored.
        """
        raw = secrets.token_urlsafe(32)
        cid = new_client_id()
        now = time.time()
        record = ClientTokenRecord(
            client_id=cid,
            client_name=client_name,
            tier=tier,
            created_at=now,
            revoked=False,
        )
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO client_tokens
                        (token_hash, client_id, client_name, tier, created_at, revoked)
                    VALUES (?, ?, ?, ?, ?, 0)
                    """,
                    (hash_token(raw), cid, client_name, tier, now),
                )
                conn.commit()
        return raw, record

    def mint_bootstrap(self, client_name: str, tier: str) -> tuple[str, ClientTokenRecord] | None:
        """Atomically mint a token ONLY while the store has no active token.

        Closes the bootstrap check-then-mint race (CT-003): two concurrent
        loopback bootstrap requests can both pass the middleware's
        ``has_active() is False`` gate, but the guarded ``INSERT ... WHERE NOT
        EXISTS`` is atomic, so exactly one wins. The loser gets ``None`` (the
        store was bootstrapped concurrently) and must be rejected by the caller.
        """
        raw = secrets.token_urlsafe(32)
        cid = new_client_id()
        now = time.time()
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO client_tokens
                        (token_hash, client_id, client_name, tier, created_at, revoked)
                    SELECT ?, ?, ?, ?, ?, 0
                    WHERE NOT EXISTS (
                        SELECT 1 FROM client_tokens WHERE revoked = 0
                    )
                    """,
                    (hash_token(raw), cid, client_name, tier, now),
                )
                conn.commit()
                if cursor.rowcount == 0:
                    return None
        record = ClientTokenRecord(
            client_id=cid,
            client_name=client_name,
            tier=tier,
            created_at=now,
            revoked=False,
        )
        return raw, record

    def resolve(self, raw_token: str) -> ClientTokenRecord | None:
        """Look up a raw bearer token.

        Returns the matching :class:`ClientTokenRecord` or *None* if the token
        is unknown or has been revoked.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT client_id, client_name, tier, created_at, revoked
                FROM client_tokens
                WHERE token_hash = ? AND revoked = 0
                """,
                (hash_token(raw_token),),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def revoke(self, client_id: str) -> bool:
        """Revoke all tokens belonging to *client_id*.

        Returns *True* if at least one row was updated.
        """
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    """
                    UPDATE client_tokens
                    SET revoked = 1
                    WHERE client_id = ?
                    """,
                    (client_id,),
                )
                conn.commit()
                return cursor.rowcount > 0

    def get(self, client_id: str) -> ClientTokenRecord | None:
        """Return the (first) non-revoked record for *client_id*, or *None*."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT client_id, client_name, tier, created_at, revoked
                FROM client_tokens
                WHERE client_id = ? AND revoked = 0
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (client_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def has_active(self) -> bool:
        """Return True if at least one non-revoked token exists.

        Used to gate loopback bootstrap minting: an unauthenticated loopback
        caller may only mint while the store has no active tokens (trust on
        first use). Revoking the last active token re-opens bootstrap.
        """
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM client_tokens WHERE revoked = 0 LIMIT 1"
            ).fetchone()
        return row is not None

    def all(self) -> list[ClientTokenRecord]:
        """Return every non-revoked token record."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT client_id, client_name, tier, created_at, revoked
                FROM client_tokens
                WHERE revoked = 0
                ORDER BY created_at ASC
                """,
            ).fetchall()
        return [self._row_to_record(r) for r in rows]


# ---------------------------------------------------------------------------
# Shared accessor
# ---------------------------------------------------------------------------

_store_singleton: ClientTokenStore | None = None
_store_singleton_lock = threading.Lock()


def configure_client_token_store(settings: object) -> ClientTokenStore | None:
    """Configure the process registry from the server's settings snapshot."""
    global _store_singleton
    if not bool(getattr(settings, "client_tokens_enabled", False)):
        _store_singleton = None
        return None
    from menhir.infrastructure.paths import oauth_as_db_path

    _store_singleton = ClientTokenStore(
        oauth_as_db_path(str(getattr(settings, "oauth_as_dir", ""))) / "client_tokens.db"
    )
    return _store_singleton


def client_tokens_enabled() -> bool:
    """Return True when the per-client token tier is enabled.

    Delegates to :class:`MemorySettings` so this reads the same
    ``MENHIR_CLIENT_TOKENS_ENABLED`` value through the same boolean parser as
    every other caller (previously this re-read the env var directly with its
    own ad hoc truthy set that included ``"on"``, while ``MemorySettings``'s
    parser did not -- the two could disagree on the same env value; see
    SSOT-07).
    """
    from menhir.config.settings import MemorySettings

    return MemorySettings.from_env().client_tokens_enabled


def get_client_token_store() -> ClientTokenStore | None:
    """Return the shared client-token store, or None when the tier is disabled.

    Backed by a single SQLite file (``oauth_as_db_path()/client_tokens.db``), so
    this instance and the HTTP server middleware's instance operate on the same
    data. Used by MCP tools, which cannot reach the FastAPI ``app.state`` store.
    """
    if not client_tokens_enabled():
        return None
    global _store_singleton
    if _store_singleton is None:
        with _store_singleton_lock:
            if _store_singleton is None:
                from menhir.infrastructure.paths import oauth_as_db_path

                _store_singleton = ClientTokenStore(oauth_as_db_path() / "client_tokens.db")
    return _store_singleton
