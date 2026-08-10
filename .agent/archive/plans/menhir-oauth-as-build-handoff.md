# Embedded OAuth AS — Build Continuation Handoff (self-contained)

**Written 2026-07-09 for a fresh session with no prior context.** Read this top to bottom
before touching the embedded AS. Everything below is on `Archolith/menhir` branch `main`,
committed. Project root: `projects/archolith/menhir/`.

Goal of the whole effort: an **embedded OAuth 2.1 authorization server** inside Menhir
(Rung 2a) so MCP connectors (ChatGPT, claude.ai web, Claude Code) get one-click login with
no external IdP. Master plan: `.agent/plans/menhir-embedded-oauth-as-plan.md` (status table).
Decision record: `.agent/reviews/menhir-oauth-as-interop-findings.md` (Phase 0 — GO, Rung 2a,
library joserfc, DCR + CIMD, PKCE S256, public clients only, token shape in §5).

---

## 1. What is DONE (this session, 2026-07-09, all on `main`)

| Phase | Commit | Module(s) |
|---|---|---|
| 1 signing keys | `df8366b` | `api/oauth_keys.py` + `/.well-known/jwks.json` in `api/oauth_metadata.py` |
| 2 client store | `e5df4f9` | `api/oauth_client_store.py` |
| S-009 JOSE migrate | `9ee2b30` | `authlib.jose` → `joserfc`; `pyproject.toml` |
| JOSE seam | `d1a7f8c` | `api/jose_provider.py` (verifier + keys go through it) |
| 3 AS metadata | `d5864bb` | `api/oauth_as_metadata.py` (`/.well-known/oauth-authorization-server`) |
| 5 auth-code store | `fb6875d` | `api/auth_code_store.py` |
| 4 DCR register | `170b0e8` | `api/oauth_as_register.py` (`POST /oauth/register`) |
| 6 /authorize + consent | `c955212` | `api/oauth_authorize.py` (`GET`/`POST /oauth/authorize`) |
| 7 /token | (prior session) | `api/oauth_token.py` (`POST /oauth/token`) |
| 8 consent session | (this session) | `api/oauth_authorize.py` (`menhir_as_session` cookie + one-click GET) |

All non-interactive AS pieces (storage, discovery, keys, registration) plus the interactive
`/authorize` consent flow (now with a one-click consent-session cookie) and the `/token`
exchange are built and behind a flag. The full authorize→token→JWT path is proven (Phase 7
test decodes the minted token through the same `jose_provider` seam the RS verifier uses). Full
OAuth suite green: **267 passed, 1 skipped** (add `tests/test_oauth_authorize.py`,
`tests/test_oauth_token.py`, `tests/test_oauth_consent_session.py` to the §5 command).

**Phase 7 result / notes for Phase 9:** `/token` mints an RS256 JWT with `iss`=base,
`aud`={base}/mcp-http, `sub`=`menhir-admin`, `client_id`/`client_name`/`scope` from the code,
`kid` from the Phase 1 signing key. The RS verifier will accept it once Phase 9 defaults
`MENHIR_OAUTH_ISSUER`/`MENHIR_OAUTH_JWKS_URI` to self. No refresh token yet (deferred).

## 2. Key facts / conventions (follow these)

- **Two flags, keep distinct.** `MENHIR_OAUTH_ENABLED` = resource-server (validate external
  tokens). `MENHIR_OAUTH_AS_ENABLED` = the embedded AS (default false; stays OFF until Phase 9
  wiring). AS endpoints 404 when their flag is off. Read via `_as_enabled(settings)` in
  `api/oauth_as_metadata.py`, or the `_get_setting`/`_as_bool` helpers in `api/oauth.py`.
- **Shared AS database:** `oauth_as_db_path()/menhir_oauth_as.db` holds `oauth_clients`
  (Phase 2) and `oauth_codes` (Phase 5) as distinct tables. `oauth_as_db_path()` is a
  DIRECTORY (`workspace_root()/.agent`, override `MENHIR_OAUTH_AS_DIR`). Separate:
  `oauth_signing_key.json` (Phase 1), `client_tokens.db` (unrelated per-client-token tier).
- **JOSE goes through the seam ONLY.** `api/jose_provider.py` is the sole module importing a
  JOSE library (joserfc). Use `sign_jwt`, `verify_jwt`, `parse_jwks`, `generate_signing_key`,
  `serialize_key`, `load_key`, `jwks_has_kid`; it raises provider-neutral `JoseError`. Do NOT
  import joserfc anywhere else. (Trust hedge: swap to PyJWT = one new provider file. Open
  item: joserfc-vs-PyJWT supply-chain data pull, deferred, findings §4a.)
- **Token shape (Phase 7), fixed by findings §5:** `iss` = `MENHIR_PUBLIC_BASE_URL`, `aud` =
  `{base}/mcp-http`, `sub` = approving identity, `client_id`+`client_name` from the registered
  client (provenance), `scope` = space-joined menhir scopes, `exp` ~1h, refresh rotated,
  `kid` from Phase 1 key, **RS256**. The RS verifier is already proven against this exact
  shape (live mock-IdP pass).
- **Auth exemption:** `/.well-known/*` and `/oauth/*` are neither `/api/` nor `/mcp`, so they
  sit OUTSIDE `BearerAuthMiddleware` automatically (unauthenticated by spec). Verified for
  metadata + register; the same holds for `/oauth/authorize|token`.
- **Router wiring:** add `app.include_router(...)` in `api/server.py` beside the existing
  `oauth_as_metadata_router` / `oauth_as_register_router` includes.
- **Working pattern (kept quality high):** write **security-critical logic yourself**
  (validation, single-use redeem, crypto, consent/CSRF); **delegate mechanical modules + all
  tests** to the `delegate` MCP with exact anchors, then review. Commit per phase
  (`feat(oauth-as): ...`), explicit paths, CHANGELOG entry, flip the master-plan status row.
- **Test isolation:** point stores at a tmp dir by setting `MENHIR_OAUTH_AS_DIR` and resetting
  the module singleton (`monkeypatch.setattr(oauth_client_store, "_client_store_singleton",
  None)`, likewise `auth_code_store._auth_code_store_singleton`, `oauth_keys._SIGNING_KEY`).
- **Windows:** run pytest with `-p no:cacheprovider`. Do NOT modify existing tests to pass;
  library-migration helper updates are the only sanctioned exception.

## 3. Remaining phases (author child plan, then build) — IN ORDER

Dependencies (all satisfied prereqs marked ✓): P1✓ P2✓ P5✓ exist.

- **Phase 6 — `/oauth/authorize` + admin-gated consent + PKCE** (needs P2✓, P5✓). The hardest
  and most security-critical. Scope: validate `client_id` (exists in store), `redirect_uri`
  (EXACT match against the registered set), `response_type=code`, `code_challenge` +
  `code_challenge_method=S256` (reject others), `scope` (⊆ client's granted), `state`,
  `resource`. Render a **consent page** that the **admin approves** (single-admin model:
  "user" = holder of the admin secret / operator key — see `api/auth.py` admin gate). On
  approval, `AuthCodeStore.issue(...)` and 302 back to `redirect_uri?code=...&state=...`.
  **Security musts:** exact redirect_uri match (no prefix/substring), CSRF token on the consent
  POST, no open redirect, error responses per OAuth 2.1 (`error=...&state=...` redirect for
  client errors vs. direct 400 for invalid client/redirect). Recommend running this one
  through `plannerific` first. Subject value for the code = the approving admin identity.
- **Phase 7 — `/oauth/token`** (needs P1✓, P5✓). Exchange code→signed JWT. `grant_type=
  authorization_code`: `AuthCodeStore.redeem(code, client_id, redirect_uri)` (atomic
  single-use; None→`invalid_grant`), then **PKCE verify** with `auth_code_store.verify_pkce(
  verifier, record.code_challenge)` (None→`invalid_grant`), then mint via
  `jose_provider.sign_jwt(header{alg RS256, kid}, claims per findings §5, get_signing_key())`.
  Return `{access_token, token_type:"Bearer", expires_in, scope, refresh_token?}`. Refresh-
  token rotation may be a follow-on; start with short access tokens (out-of-scope note in
  master plan). Standard OAuth error bodies (`invalid_grant`, `invalid_request`,
  `unsupported_grant_type`).
- **Phase 8 — consent session cookie** (needs P6). Remember a prior admin approval so repeat
  authorizes are true one-click. Signed/HTTP-only cookie; short TTL; scoped to the admin.
- **Phase 9 — resource self-wiring + E2E** (needs P1✓, P7). When `MENHIR_OAUTH_AS_ENABLED`,
  default `MENHIR_OAUTH_ISSUER`/`MENHIR_OAUTH_JWKS_URI` to self so the RS verifier consumes
  the AS's own JWKS with zero config. Full flow test: register → authorize → token → call a
  protected `/mcp` route with the minted JWT. This is where the flag finally goes on.
- **Phase 10 — security audit** (needs P3–P9). Use the workspace audit library
  (`.agent/audit/`), write results to `.agent/reviews/`. Focus: redirect_uri exact-match,
  PKCE required + correct, single-use codes, consent CSRF, admin-secret handling, no open
  redirect, token claim correctness, the jose_provider crypto path.

**Also open (not blocking the flow):** Phase 4b CIMD accept-path (SSRF-guarded outbound
client-metadata fetch — https-only, block loopback/private/link-local, size+timeout+cache).

## 4. File map (embedded-AS modules)

- `api/oauth_keys.py` — signing key (`get_signing_key`, `public_jwks`) via seam.
- `api/oauth_client_store.py` — `OAuthClientStore` (`register/get/all/verify_secret`),
  `new_client_id`, `hash_secret`, `get_client_store()`.
- `api/auth_code_store.py` — `AuthCodeStore` (`issue/redeem/purge_expired`), `verify_pkce`,
  `hash_code`, `get_auth_code_store()`. Single-use is DB-enforced (atomic UPDATE).
- `api/jose_provider.py` — JOSE seam (see §2).
- `api/oauth_as_metadata.py` — `/.well-known/oauth-authorization-server`, `_as_enabled`.
- `api/oauth_as_register.py` — `POST /oauth/register` (DCR), redirect-uri validator.
- `api/oauth.py` — RS verifier (`OAuthTokenVerifier`), `build_oauth_config`, `_get_setting`,
  `_as_bool`, scope→tier, `OAuthConfig.scopes_supported`.
- `api/server.py` — `create_app` router wiring.
- Existing plans: `menhir-oauth-as-phase3-as-metadata.md`, `-phase4-dcr.md`,
  `-phase5-authcode-store.md`, `-phase1-signing-keys.md`, `-phase2-client-store.md`.

## 5. Verify current state

From `projects/archolith/menhir/`:
```
python -m pytest -p no:cacheprovider -q \
  tests/test_jose_provider.py tests/test_oauth_keys.py tests/test_oauth_client_store.py \
  tests/test_auth_code_store.py tests/test_oauth_metadata.py tests/test_oauth_as_metadata.py \
  tests/test_oauth_as_register.py tests/test_oauth_jwt_verifier.py tests/test_api_auth.py \
  tests/test_loopback_auth_safety.py tests/test_operator_diagnostics.py \
  tests/test_oauth_operator_preflight.py tests/test_oauth_local_smoke.py
```
Expected: 235 passed, 1 skipped (the skip is a Windows 0o600 file-mode test).

## 6. Next action

Phases 6 (`c955212`), 7 (`803eaf1`), and 8 are DONE. Next: author
`menhir-oauth-as-phase9-resource-wiring.md` and build Phase 9 — when `MENHIR_OAUTH_AS_ENABLED`,
default `MENHIR_OAUTH_ISSUER`/`MENHIR_OAUTH_JWKS_URI` to self so the existing RS verifier
consumes the AS's own JWKS with zero config, then a full E2E test: register → authorize →
token → call a protected `/mcp` route with the minted JWT. **This is where the flag finally
goes ON.** Finish with the Phase 10 security audit (`.agent/audit/` library →
`.agent/reviews/`). Broader auth context: `.agent/plans/menhir-oauth-handoff.md`.
