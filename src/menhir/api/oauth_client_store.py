"""Menhir compatibility wrapper for the shared OAuth client store."""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from typing import TYPE_CHECKING

from archolith_oauth import OAuthClient, OAuthClientStore, hash_secret

if TYPE_CHECKING:
    from menhir.api.client_policy import ClientPolicyAuthority


def new_client_id() -> str:
    """Preserve Menhir's existing compact DCR client-id format."""
    return secrets.token_hex(8)


_client_store_singleton: OAuthClientStore | None = None
_client_store_singleton_lock = threading.Lock()

# Serializes CIMD snapshot upserts / freshness bookkeeping within this process.
# Cross-process safety comes from SQLite BEGIN IMMEDIATE transactions.
_cimd_write_lock = threading.Lock()


def _upsert_client_row(conn: sqlite3.Connection, client: OAuthClient) -> None:
    conn.execute(
        """INSERT INTO oauth_clients
           (client_id, client_name, redirect_uris, scopes, client_secret_hash,
            created_at, token_endpoint_auth_method, last_exchanged)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(client_id) DO UPDATE SET
             client_name=excluded.client_name,
             redirect_uris=excluded.redirect_uris,
             scopes=excluded.scopes,
             client_secret_hash=excluded.client_secret_hash,
             created_at=excluded.created_at,
             token_endpoint_auth_method=excluded.token_endpoint_auth_method,
             last_exchanged=COALESCE(oauth_clients.last_exchanged, excluded.last_exchanged)
        """,
        (
            client.client_id,
            client.client_name,
            json.dumps(list(client.redirect_uris)),
            json.dumps(list(client.scopes)),
            client.client_secret_hash,
            client.created_at,
            client.token_endpoint_auth_method,
            client.last_exchanged,
        ),
    )


def upsert_client(client: OAuthClient) -> None:
    """Durably insert-or-update a client row under its exact ``client_id``.

    Used for CIMD snapshots keyed by the metadata URL, which must be refreshable
    in place (the shared store's ``register`` is INSERT-only). An existing
    ``last_exchanged`` marker is preserved so reaping semantics for exchanged
    clients survive a snapshot refresh. Ordinary DCR clients are untouched.
    """
    with _cimd_write_lock:
        conn = sqlite3.connect(str(get_client_store().db_path))
        try:
            conn.execute("BEGIN IMMEDIATE")
            _upsert_client_row(conn, client)
            conn.commit()
        finally:
            conn.close()


def upsert_cimd_client(client: OAuthClient, *, fetched_at: float) -> None:
    """Atomically persist a validated CIMD snapshot and its freshness marker."""
    with _cimd_write_lock:
        conn = sqlite3.connect(str(get_client_store().db_path))
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS oauth_cimd_cache ("
                "client_id TEXT PRIMARY KEY, fetched_at REAL NOT NULL)"
            )
            _upsert_client_row(conn, client)
            conn.execute(
                "INSERT INTO oauth_cimd_cache (client_id, fetched_at) VALUES (?, ?)"
                " ON CONFLICT(client_id) DO UPDATE SET fetched_at=excluded.fetched_at",
                (client.client_id, float(fetched_at)),
            )
            conn.commit()
        finally:
            conn.close()


def _connect_cimd_cache() -> sqlite3.Connection:
    store = get_client_store()
    conn = sqlite3.connect(str(store.db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS oauth_cimd_cache ("
        "client_id TEXT PRIMARY KEY, fetched_at REAL NOT NULL)"
    )
    return conn


def record_cimd_fetch(client_id: str, *, now: float | None = None) -> None:
    """Record the fetch time of a CIMD document snapshot for bounded freshness."""
    ts = float(now) if now is not None else time.time()
    with _cimd_write_lock:
        conn = _connect_cimd_cache()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO oauth_cimd_cache (client_id, fetched_at) VALUES (?, ?)"
                " ON CONFLICT(client_id) DO UPDATE SET fetched_at=excluded.fetched_at",
                (client_id, ts),
            )
            conn.commit()
        finally:
            conn.close()


def cimd_fetched_at(client_id: str) -> float | None:
    """Return the recorded CIMD fetch time for *client_id*, or None."""
    try:
        conn = _connect_cimd_cache()
    except Exception:
        return None
    try:
        row = conn.execute(
            "SELECT fetched_at FROM oauth_cimd_cache WHERE client_id = ?",
            (client_id,),
        ).fetchone()
    except Exception:
        return None
    finally:
        conn.close()
    return float(row[0]) if row is not None else None


def configure_client_store(settings: object) -> OAuthClientStore:
    """Build the process store from the server's immutable settings snapshot."""
    global _client_store_singleton
    from menhir.infrastructure.paths import oauth_as_db_path

    _client_store_singleton = OAuthClientStore(
        oauth_as_db_path(str(getattr(settings, "oauth_as_dir", "")))
        / "menhir_oauth_as.db"
    )
    return _client_store_singleton


def reconcile_policy_clients(
    authority: "ClientPolicyAuthority",
    store: OAuthClientStore,
    *,
    now: float | None = None,
) -> tuple[str, ...]:
    """Seed policy-owned public clients atomically and reject stored drift.

    Registrations are optional because CIMD clients resolve from their metadata
    documents. A static web client, however, must be reproducible from the same
    digest-bound artifact that grants its authority. Existing rows are never
    overwritten; any mismatch aborts the whole transaction before a partial
    policy reconciliation can commit.
    """

    created_at = time.time() if now is None else float(now)
    registrations = tuple(
        policy
        for policy in authority.clients.values()
        if policy.registration is not None
    )
    if not registrations:
        return ()

    inserted: list[str] = []
    with _cimd_write_lock:
        conn = sqlite3.connect(str(store.db_path))
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            for policy in registrations:
                registration = policy.registration
                assert registration is not None
                row = conn.execute(
                    """SELECT client_name, redirect_uris, scopes,
                              client_secret_hash, token_endpoint_auth_method
                       FROM oauth_clients WHERE client_id = ?""",
                    (policy.client_id,),
                ).fetchone()
                if row is not None:
                    try:
                        stored_redirect_uris = tuple(json.loads(row["redirect_uris"]))
                        stored_scopes = frozenset(json.loads(row["scopes"]))
                    except Exception as exc:
                        raise ValueError(
                            "policy-owned OAuth client metadata is malformed"
                        ) from exc
                    if (
                        row["client_name"] != registration.client_name
                        or stored_redirect_uris != registration.redirect_uris
                        or stored_scopes != policy.scopes
                        or row["client_secret_hash"] != ""
                        or row["token_endpoint_auth_method"]
                        != registration.token_endpoint_auth_method
                    ):
                        raise ValueError(
                            "policy-owned OAuth client metadata does not match "
                            f"the configured authority: {policy.label}"
                        )
                    continue

                conn.execute(
                    """INSERT INTO oauth_clients
                       (client_id, client_name, redirect_uris, scopes,
                        client_secret_hash, created_at,
                        token_endpoint_auth_method, last_exchanged)
                       VALUES (?, ?, ?, ?, '', ?, ?, NULL)""",
                    (
                        policy.client_id,
                        registration.client_name,
                        json.dumps(list(registration.redirect_uris)),
                        json.dumps(sorted(policy.scopes)),
                        created_at,
                        registration.token_endpoint_auth_method,
                    ),
                )
                inserted.append(policy.client_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return tuple(inserted)


def get_client_store() -> OAuthClientStore:
    """Return the shared embedded-AS registered-client store."""
    global _client_store_singleton
    if _client_store_singleton is None:
        with _client_store_singleton_lock:
            if _client_store_singleton is None:
                from types import SimpleNamespace

                from menhir.config.oauth import _get_setting

                legacy = object()
                _client_store_singleton = configure_client_store(
                    SimpleNamespace(
                        oauth_as_dir=str(
                            _get_setting(
                                legacy,
                                "oauth_as_dir",
                                "MENHIR_OAUTH_AS_DIR",
                                "",
                            )
                        )
                    )
                )
    return _client_store_singleton


__all__ = [
    "OAuthClient",
    "OAuthClientStore",
    "cimd_fetched_at",
    "configure_client_store",
    "get_client_store",
    "hash_secret",
    "new_client_id",
    "record_cimd_fetch",
    "reconcile_policy_clients",
    "upsert_cimd_client",
    "upsert_client",
]
