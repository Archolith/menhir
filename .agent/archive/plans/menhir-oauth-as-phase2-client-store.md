# Phase 2 — Persistent Registered-Client Store

Parent: `menhir-embedded-oauth-as-plan.md`. **Interop- and library-independent — may run in
parallel with Phase 0.** Bite-sized; one new module + tests. No HTTP in this phase.

**Project:** `projects/archolith/menhir/`.

## Objective

A durable store of OAuth clients that register with Menhir (via Dynamic Client Registration,
Phase 4). Each record carries the client's identity so tokens minted later (Phase 7) can
embed `client_id`/`client_name` — this is the **provenance** requirement. This phase is the
storage layer only; the `/register` endpoint that writes to it is Phase 4.

## Context / anchors

- Follow the existing SQLite pattern in the codebase, not JSON:
  `src/menhir/services/scheduler_lease.py` and `src/menhir/infrastructure/pending_actions.py`
  both use `sqlite3.connect(self.db_path)` with `db_path.parent.mkdir(parents=True,
  exist_ok=True)` and a table created on init. Mirror that structure.
- Data-dir: `oauth_as_db_path()` from `src/menhir/infrastructure/paths.py` (added in
  Phase 1; if Phase 1 has not landed yet, add that helper here instead — coordinate so it is
  added exactly once).
- Secret hashing: reuse `hashlib`/`hmac` already used in `src/menhir/api/auth.py` (constant-
  time compare via `hmac.compare_digest`). Do not add a new crypto dependency.

## Data model

```python
@dataclass(frozen=True)
class OAuthClient:
    client_id: str          # server-generated, stable
    client_name: str        # from DCR client metadata — the provenance label
    redirect_uris: tuple[str, ...]
    scopes: tuple[str, ...]         # scopes this client may request
    client_secret_hash: str = ""    # sha256 hash; empty for public PKCE-only clients
    created_at: float = 0.0         # unix ts
    token_endpoint_auth_method: str = "none"  # "none" (public+PKCE) or "client_secret_post"
```

## Tasks

1. **Create `src/menhir/api/oauth_client_store.py`** with an `OAuthClientStore` class:
   - `__init__(self, db_path: Path)` — mkdir parents, `CREATE TABLE IF NOT EXISTS
     oauth_clients (client_id TEXT PRIMARY KEY, client_name TEXT, redirect_uris TEXT,
     scopes TEXT, client_secret_hash TEXT, created_at REAL, token_endpoint_auth_method TEXT)`.
     Store `redirect_uris`/`scopes` as JSON-encoded text.
   - `register(self, client: OAuthClient) -> None` — INSERT; raise on duplicate `client_id`.
   - `get(self, client_id: str) -> OAuthClient | None` — SELECT one, decode JSON fields.
   - `all(self) -> list[OAuthClient]`.
   - `verify_secret(self, client_id: str, presented_secret: str) -> bool` — look up, compare
     `sha256(presented_secret)` against stored hash with `hmac.compare_digest`; return False
     for unknown client or public client with empty hash + non-empty presented secret.
   - Use a `threading.Lock` around writes (mirror `service_access.py` / `scheduler_lease.py`).
2. **Helper `new_client_id() -> str`** and **`hash_secret(secret: str) -> str`** (sha256
   hexdigest) as module functions, so Phase 4 reuses them.
3. **Module accessor** `get_client_store() -> OAuthClientStore` — lazy singleton bound to
   `oauth_as_db_path() / "oauth_clients.db"` (a **separate** db file from the signing key;
   or a shared `menhir_oauth_as.db` — pick one and be consistent with Phase 5's code store).

## Tests (new file `tests/test_oauth_client_store.py`)

- `test_register_and_get_roundtrip`: register a client, `get` returns equal fields
  (redirect_uris/scopes decode back to tuples).
- `test_persistence_across_instances`: register with one `OAuthClientStore(db)`, read with a
  fresh instance on the same path — record survives.
- `test_duplicate_client_id_raises`.
- `test_secret_never_stored_plaintext`: after registering with a secret, the raw secret does
  not appear in the DB file bytes; `verify_secret` returns True for the right secret, False
  for a wrong one.
- `test_public_client_has_empty_hash`: a `token_endpoint_auth_method="none"` client stores an
  empty `client_secret_hash`.
- `test_all_lists_registered`.

## Acceptance criteria

- `python -c "import menhir.api.oauth_client_store"` imports cleanly.
- `pytest -p no:cacheprovider -q tests/test_oauth_client_store.py` passes.
- Secrets are only ever persisted as sha256 hashes; verification is constant-time.
- No change to any existing test.

## Out of scope

- The `/register` HTTP endpoint + DCR request/response shaping (Phase 4).
- Client expiry / revocation (v2).

## Verify

`pytest -p no:cacheprovider -q tests/test_oauth_client_store.py`
Commit: `feat(oauth-as): persistent registered-client store`
