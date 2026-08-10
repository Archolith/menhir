# Phase 1 — Local Signing Key Bootstrap + JWKS Endpoint

Parent: `menhir-embedded-oauth-as-plan.md`. **Interop- and library-independent — may run in
parallel with Phase 0.** Bite-sized; one new module + one endpoint + tests.

**Project:** `projects/archolith/menhir/`.

## Objective

Give Menhir a persistent local RSA signing key so it can sign its own access tokens, and
serve the matching public key as a JWKS. This is the foundation the `/token` endpoint
(Phase 7) and the resource-server self-wiring (Phase 9) build on. Zero external setup: the
key is generated on first boot and persisted.

## Context / anchors

- Reuse the JOSE library the resource-server verifier already uses:
  `from authlib.jose import JsonWebKey` (`src/menhir/api/oauth.py:18`). If the S-009
  migration to `joserfc` has landed by execution time, use whatever `oauth.py` imports —
  stay consistent with the verifier, do not introduce a second JOSE lib.
- Data-dir convention: `src/menhir/infrastructure/paths.py` (`workspace_root() / ".agent" /
  <file>`, env override — see `telemetry_db_path`).
- Metadata routes live in `src/menhir/api/oauth_metadata.py` (an `APIRouter`), mounted in
  `src/menhir/api/server.py` via `app.include_router(oauth_metadata_router)`. The
  `.well-known` paths there are already exempt from auth (they are neither `/api/*` nor
  `/mcp*` — see `api/auth.py:_is_mcp_path` and the `/api/` prefix check).

## Tasks

1. **Add the data-dir helper.** In `src/menhir/infrastructure/paths.py`, add:
   ```python
   def oauth_as_db_path() -> Path:
       """Return the path for embedded OAuth AS state (signing key + stores)."""
       override = os.getenv("MENHIR_OAUTH_AS_DIR")
       base = Path(override) if override else (workspace_root() / ".agent")
       return base
   ```
   (Returns the directory; individual files live under it. Mirror the existing style.)

2. **Create `src/menhir/api/oauth_keys.py`** with:
   - `load_or_create_signing_key(key_path: Path) -> JsonWebKey` — if `key_path` exists, load
     the private JWK from it; else generate `JsonWebKey.generate_key("RSA", 2048,
     is_private=True)`, write it to `key_path` with **file mode 0o600**
     (`os.chmod`/`open(..., 0o600)`), and return it. Create parent dirs as needed.
   - The stored JWK MUST include a stable `kid` (use the key's RFC 7638 thumbprint; Authlib:
     `key.thumbprint()`), set before persisting so it is stable across restarts.
   - `public_jwks(signing_key: JsonWebKey) -> dict` — return `{"keys": [<public JWK>]}` with
     **no private material** (no `d`, `p`, `q`, `dp`, `dq`, `qi`). Use the library's
     public-export (e.g. `key.as_dict(is_private=False)`); assert `"d"` not in the output.
   - A module-level accessor `get_signing_key() -> JsonWebKey` that lazily loads/creates from
     `oauth_as_db_path() / "oauth_signing_key.json"` and caches the result (module singleton,
     mirror the caching style in `mcp/service_access.py`).

3. **Serve the JWKS.** In `src/menhir/api/oauth_metadata.py`, add a route:
   ```python
   @router.get("/.well-known/jwks.json", include_in_schema=False)
   async def jwks() -> JSONResponse:
       return JSONResponse(public_jwks(get_signing_key()))
   ```
   Confirm this path is unauthenticated (it is not under `/api/` or `/mcp` — verify against
   `api/auth.py` path checks; if the exempt logic needs the path, add it to `_EXEMPT_PATHS`).

## Tests (new file `tests/test_oauth_keys.py`)

- `test_key_persists_across_calls`: two `load_or_create_signing_key(tmp_path/...)` calls
  return a key with the **same `kid`** and same modulus; file created once.
- `test_kid_is_stable`: kid equals the thumbprint; unchanged after reload.
- `test_public_jwks_has_no_private_material`: `"d"` (and other private params) absent from
  every key in `public_jwks(...)`.
- `test_jwks_endpoint_shape` (async, via app/test client): `GET /.well-known/jwks.json`
  returns 200, `{"keys": [ {kty, kid, n, e, ...} ]}`, no private fields, and requires **no
  auth**.
- `test_signing_key_file_permissions` (skip on Windows if `0o600` is not enforceable):
  private key file is not world-readable.

## Acceptance criteria

- `python -c "import menhir.api.oauth_keys"` imports cleanly.
- `pytest -p no:cacheprovider -q tests/test_oauth_keys.py` passes.
- A token signed with `get_signing_key()` verifies against `public_jwks(...)` using the same
  JOSE library (add a round-trip assertion in the test: sign a trivial JWT, verify with the
  public set).
- No change to any existing test.

## Out of scope

- Key rotation / multiple active kids (single key is fine for v1; JWKS is a list so rotation
  is additive later).
- The `/token` endpoint that uses this (Phase 7).

## Verify

`pytest -p no:cacheprovider -q tests/test_oauth_keys.py`
Commit: `feat(oauth-as): local RSA signing key bootstrap + JWKS endpoint`
