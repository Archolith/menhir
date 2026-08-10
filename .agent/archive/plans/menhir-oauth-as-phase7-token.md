# Phase 7 — `/oauth/token` (code → signed JWT) (child plan)

**Parent:** `menhir-embedded-oauth-as-plan.md` (row 7). **Depends on:** P1 signing keys ✓,
P5 auth-code store ✓, P6 `/authorize` ✓ (issues the codes). **Status:** authored 2026-07-09.

Exchange an authorization code for a signed RS256 access token whose claims the **existing**
resource-server verifier (`OAuthTokenVerifier`) already accepts. Hand-written (security-
critical crypto path); test file written alongside. Flag stays OFF (`MENHIR_OAUTH_AS_ENABLED`).

## Deliverable

New module `src/menhir/api/oauth_token.py` exposing `router` with `POST /oauth/token`
(form-encoded, per OAuth 2.0). Wire `oauth_token_router` into `create_app` beside the other AS
routers. New `tests/test_oauth_token.py`. CHANGELOG entry. Master-plan row 7 → DONE.

## Request contract (`POST /oauth/token`, `application/x-www-form-urlencoded`)

| param | rule |
|---|---|
| `grant_type` | REQUIRED; must equal `authorization_code` (else `unsupported_grant_type`). |
| `code` | REQUIRED (else `invalid_request`). |
| `redirect_uri` | REQUIRED; must match the value bound to the code (enforced by `redeem`). |
| `client_id` | REQUIRED; must match the code's client (enforced by `redeem`). |
| `code_verifier` | REQUIRED (PKCE). |

## Flow

1. 404 when `_as_enabled(settings)` is false.
2. Parse form; `grant_type != "authorization_code"` → 400 `unsupported_grant_type`.
3. Any of `code` / `redirect_uri` / `client_id` / `code_verifier` missing → 400 `invalid_request`.
4. `record = get_auth_code_store().redeem(code=code, client_id=client_id, redirect_uri=redirect_uri)`.
   `None` → 400 `invalid_grant`. **`redeem` is atomic single-use — the code is burned even on a
   later PKCE failure** (defensive, matches Phase 5).
5. `auth_code_store.verify_pkce(code_verifier, record.code_challenge)` False → 400 `invalid_grant`.
6. Provenance: `client = get_client_store().get(record.client_id)`; `client_name =
   client.client_name if client else ""`.
7. Mint (token shape fixed by findings §5 / handoff §2):
   - header `{alg: RS256, kid, typ: JWT}` where `kid = public_jwks(get_signing_key())["keys"][0]["kid"]`
     (key handles are opaque — the kid is read through the JOSE seam's serialized JWK, not by
     touching the handle).
   - claims `{iss: base, sub: record.subject, aud: resource, client_id: record.client_id,
     client_name, scope: record.scope, iat: now, exp: now + ttl}`.
   - `base = build_oauth_config(settings).public_base_url` (500 if empty — `iss`/`aud` need it);
     `resource = build_oauth_config(settings).resource` (= `{base}/mcp-http`, the RS audience).
   - `ttl = MENHIR_OAUTH_AS_ACCESS_TTL_S` (default 3600).
   - sign via `jose_provider.sign_jwt(header, claims, get_signing_key())`.
8. Response `200 {access_token, token_type: "Bearer", expires_in, scope}` with
   `Cache-Control: no-store` and `Pragma: no-cache` headers. No refresh token (deferred; short
   access tokens per master-plan out-of-scope).

## Error bodies

OAuth 2.0 token error shape: JSON `{"error": ..., "error_description": ...}`, status 400,
`Cache-Control: no-store`. Codes used: `unsupported_grant_type`, `invalid_request`,
`invalid_grant`.

## Security musts (Phase 10 audits)

- single-use enforced by `redeem` (already DB-atomic); a burned/expired/wrong-binding code →
  `invalid_grant`, no token.
- PKCE verified server-side against the stored challenge; failure → `invalid_grant`.
- RS256 only; `kid` from the persisted signing key; private material never leaves the seam.
- claims exactly match the shape the RS verifier validates (iss/aud/exp/scope→tier).
- no token caching (`Cache-Control: no-store`).

## Files

- ADD `src/menhir/api/oauth_token.py` (hand-written).
- EDIT `src/menhir/api/server.py` — import + `app.include_router(oauth_token_router)`.
- ADD `tests/test_oauth_token.py`.
- EDIT `CHANGELOG.md`; EDIT master plan row 7 → DONE.

## Test matrix (tests/test_oauth_token.py)

Isolate the `oauth_client_store` + `auth_code_store` + `oauth_keys._SIGNING_KEY` singletons via
`MENHIR_OAUTH_AS_DIR`. Seed a code by registering a client and calling
`get_auth_code_store().issue(...)` directly (subject="menhir-admin", a known challenge).

1. disabled flag → 404.
2. `grant_type=password` → 400 `unsupported_grant_type`.
3. missing `code_verifier` → 400 `invalid_request`.
4. unknown/After-redeem `code` → 400 `invalid_grant`.
5. wrong `code_verifier` (fails PKCE) → 400 `invalid_grant`; and the code is now burned (a second
   correct attempt also `invalid_grant`).
6. happy path → 200; body has `access_token`, `token_type=Bearer`, `expires_in`, `scope`;
   response header `Cache-Control: no-store`.
7. minted JWT verifies through the SAME seam the RS uses: build the JWKS from
   `public_jwks(get_signing_key())`, `jose_provider.verify_jwt(token, keyset, ["RS256"], 60)`,
   assert `iss==base`, `aud=={base}/mcp-http`, `sub=="menhir-admin"`, `scope`, `client_id`,
   `client_name`, and `exp>iat`.
8. code is single-use: a second `/token` with the same code → 400 `invalid_grant`.
9. wrong `redirect_uri` (not the bound one) → 400 `invalid_grant`.

## Out of scope

- Refresh-token rotation (later). Consent session cookie (Phase 8). Full connector E2E (Phase 9).
